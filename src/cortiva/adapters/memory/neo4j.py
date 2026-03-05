"""
Neo4j graph memory adapter.

Stores agent memories as nodes in a Neo4j graph with edges for
similarity, temporal proximity, and prediction error relationships.

Install: pip install neo4j
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from cortiva.adapters.protocols import MemoryRecord


class Neo4jMemoryAdapter:
    """
    Graph-backed memory adapter using Neo4j.

    Each memory is a node with properties. Edges represent relationships:
    - SIMILAR_TO: cosine similarity between experiences
    - FOLLOWED_BY: temporal sequence
    - PREDICTION_ERROR: unexpected outcomes linked to the original expectation
    """

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        username: str = "neo4j",
        password: str = "password",
        database: str = "neo4j",
    ) -> None:
        self._uri = uri
        self._username = username
        self._password = password
        self._database = database
        self._driver: Any = None

    def _get_driver(self) -> Any:
        if self._driver is None:
            try:
                from neo4j import GraphDatabase
            except ImportError:
                raise ImportError(
                    "neo4j is not installed. Install it with: pip install neo4j"
                )
            self._driver = GraphDatabase.driver(
                self._uri, auth=(self._username, self._password),
            )
        return self._driver

    def _run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        driver = self._get_driver()
        with driver.session(database=self._database) as session:
            result = session.run(query, **params)
            return [dict(record) for record in result]

    @staticmethod
    def _record_from_node(node: dict[str, Any], agent_id: str) -> MemoryRecord:
        return MemoryRecord(
            id=node.get("id", ""),
            content=node.get("content", ""),
            agent_id=agent_id,
            tags=node.get("tags", []),
            importance=float(node.get("importance", 5.0)),
            metadata=node.get("metadata", {}),
            outcome=node.get("outcome", ""),
            emotion_dimensions=node.get("emotion_dimensions", {}),
            prediction_error=float(node.get("prediction_error", 0.0)),
        )

    # --- MemoryAdapter interface ---

    async def store(
        self,
        agent_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 5.0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        record_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._run(
            """
            CREATE (m:Memory {
                id: $id, agent_id: $agent_id, content: $content,
                tags: $tags, importance: $importance, metadata: $metadata,
                outcome: '', emotion_dimensions: '{}',
                prediction_error: 0.0, created_at: $created_at
            })
            """,
            id=record_id, agent_id=agent_id, content=content,
            tags=tags or [], importance=importance,
            metadata=str(metadata or {}), created_at=now,
        )
        return MemoryRecord(
            id=record_id, content=content, agent_id=agent_id,
            tags=tags or [], importance=importance,
            metadata=metadata or {},
        )

    async def search(
        self,
        agent_id: str,
        query: str,
        *,
        limit: int = 10,
        min_importance: float = 0.0,
        tags: list[str] | None = None,
    ) -> list[MemoryRecord]:
        tag_clause = ""
        if tags:
            tag_clause = "AND any(t IN m.tags WHERE t IN $tags)"
        results = self._run(
            f"""
            MATCH (m:Memory)
            WHERE m.agent_id = $agent_id
              AND m.importance >= $min_importance
              AND toLower(m.content) CONTAINS toLower($query)
              {tag_clause}
            RETURN m ORDER BY m.importance DESC LIMIT $limit
            """,
            agent_id=agent_id, query=query,
            min_importance=min_importance, limit=limit,
            tags=tags or [],
        )
        return [
            self._record_from_node(dict(r["m"]), agent_id)
            for r in results
        ]

    async def recall(
        self,
        agent_id: str,
        *,
        limit: int = 20,
        min_importance: float = 0.0,
    ) -> list[MemoryRecord]:
        results = self._run(
            """
            MATCH (m:Memory)
            WHERE m.agent_id = $agent_id AND m.importance >= $min_importance
            RETURN m ORDER BY m.importance DESC LIMIT $limit
            """,
            agent_id=agent_id, min_importance=min_importance, limit=limit,
        )
        return [
            self._record_from_node(dict(r["m"]), agent_id)
            for r in results
        ]

    async def delete(self, agent_id: str, memory_id: str) -> bool:
        result = self._run(
            """
            MATCH (m:Memory {id: $id, agent_id: $agent_id})
            DETACH DELETE m
            RETURN count(m) AS deleted
            """,
            id=memory_id, agent_id=agent_id,
        )
        return result[0].get("deleted", 0) > 0 if result else False

    # --- GraphMemoryAdapter extensions ---

    async def create_edge(
        self,
        agent_id: str,
        from_id: str,
        to_id: str,
        relationship: str,
        weight: float = 1.0,
    ) -> None:
        rel_type = relationship.upper().replace(" ", "_")
        self._run(
            f"""
            MATCH (a:Memory {{id: $from_id, agent_id: $agent_id}})
            MATCH (b:Memory {{id: $to_id, agent_id: $agent_id}})
            CREATE (a)-[r:{rel_type} {{weight: $weight}}]->(b)
            """,
            from_id=from_id, to_id=to_id,
            agent_id=agent_id, weight=weight,
        )

    async def find_clusters(
        self,
        agent_id: str,
        *,
        tag: str | None = None,
        min_importance: float = 0.0,
        threshold: float = 0.5,
    ) -> list[list[MemoryRecord]]:
        tag_clause = "AND $tag IN m.tags" if tag else ""
        results = self._run(
            f"""
            MATCH (m:Memory)-[r]-(n:Memory)
            WHERE m.agent_id = $agent_id
              AND m.importance >= $min_importance
              AND r.weight >= $threshold
              {tag_clause}
            RETURN m, collect(DISTINCT n) AS cluster
            ORDER BY m.importance DESC
            """,
            agent_id=agent_id, min_importance=min_importance,
            threshold=threshold, tag=tag or "",
        )
        clusters: list[list[MemoryRecord]] = []
        seen: set[str] = set()
        for row in results:
            center = dict(row["m"])
            if center.get("id") in seen:
                continue
            cluster = [self._record_from_node(center, agent_id)]
            seen.add(center.get("id", ""))
            for node in row.get("cluster", []):
                node_dict = dict(node)
                if node_dict.get("id") not in seen:
                    cluster.append(self._record_from_node(node_dict, agent_id))
                    seen.add(node_dict.get("id", ""))
            clusters.append(cluster)
        return clusters

    async def traverse(
        self,
        agent_id: str,
        start_id: str,
        *,
        depth: int = 2,
        min_weight: float = 0.0,
    ) -> list[MemoryRecord]:
        results = self._run(
            """
            MATCH (start:Memory {id: $start_id, agent_id: $agent_id})
            MATCH path = (start)-[r*1..$depth]-(connected:Memory)
            WHERE ALL(rel IN relationships(path) WHERE rel.weight >= $min_weight)
            RETURN DISTINCT connected
            ORDER BY connected.importance DESC
            """,
            start_id=start_id, agent_id=agent_id,
            depth=depth, min_weight=min_weight,
        )
        return [
            self._record_from_node(dict(r["connected"]), agent_id)
            for r in results
        ]

    async def get_edges(
        self,
        agent_id: str,
        memory_id: str,
    ) -> list[dict[str, Any]]:
        results = self._run(
            """
            MATCH (m:Memory {id: $id, agent_id: $agent_id})-[r]-(n:Memory)
            RETURN type(r) AS relationship, r.weight AS weight,
                   n.id AS target_id, n.content AS target_content
            """,
            id=memory_id, agent_id=agent_id,
        )
        return [
            {
                "relationship": r["relationship"],
                "weight": r.get("weight", 1.0),
                "target_id": r["target_id"],
                "target_content": r.get("target_content", ""),
            }
            for r in results
        ]

    def close(self) -> None:
        if self._driver:
            self._driver.close()
            self._driver = None
