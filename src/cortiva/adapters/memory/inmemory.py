"""
In-memory adapter for testing and development.

No external dependencies. Memories live in a dict and die with the process.
Useful for unit tests and local development before wiring up real storage.
"""

from __future__ import annotations

import uuid
from typing import Any

from cortiva.adapters.protocols import MemoryRecord


class InMemoryAdapter:
    """Simple dict-backed memory. No persistence. Great for tests."""

    def __init__(self) -> None:
        self._store: dict[str, list[MemoryRecord]] = {}

    async def store(
        self,
        agent_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 5.0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        record = MemoryRecord(
            id=str(uuid.uuid4()),
            content=content,
            agent_id=agent_id,
            tags=tags or [],
            importance=importance,
            metadata=metadata or {},
        )
        self._store.setdefault(agent_id, []).append(record)
        return record

    async def search(
        self,
        agent_id: str,
        query: str,
        *,
        limit: int = 10,
        min_importance: float = 0.0,
        tags: list[str] | None = None,
    ) -> list[MemoryRecord]:
        records = self._store.get(agent_id, [])
        query_lower = query.lower()

        matches = [
            r for r in records
            if query_lower in r.content.lower()
            and r.importance >= min_importance
            and (not tags or any(t in r.tags for t in tags))
        ]
        matches.sort(key=lambda r: r.importance, reverse=True)
        return matches[:limit]

    async def recall(
        self,
        agent_id: str,
        *,
        limit: int = 20,
        min_importance: float = 0.0,
    ) -> list[MemoryRecord]:
        records = self._store.get(agent_id, [])
        filtered = [r for r in records if r.importance >= min_importance]
        filtered.sort(key=lambda r: r.importance, reverse=True)
        return filtered[:limit]

    async def delete(self, agent_id: str, memory_id: str) -> bool:
        records = self._store.get(agent_id, [])
        before = len(records)
        self._store[agent_id] = [r for r in records if r.id != memory_id]
        return len(self._store[agent_id]) < before

    # --- GraphMemoryAdapter extensions (in-memory graph) ---

    def __init_edges(self) -> None:
        if not hasattr(self, "_edges"):
            self._edges: dict[str, list[dict[str, Any]]] = {}

    async def create_edge(
        self,
        agent_id: str,
        from_id: str,
        to_id: str,
        relationship: str,
        weight: float = 1.0,
    ) -> None:
        self.__init_edges()
        self._edges.setdefault(agent_id, []).append({
            "from_id": from_id,
            "to_id": to_id,
            "relationship": relationship,
            "weight": weight,
        })

    async def get_edges(
        self,
        agent_id: str,
        memory_id: str,
    ) -> list[dict[str, Any]]:
        self.__init_edges()
        return [
            e for e in self._edges.get(agent_id, [])
            if e["from_id"] == memory_id or e["to_id"] == memory_id
        ]

    async def traverse(
        self,
        agent_id: str,
        start_id: str,
        *,
        depth: int = 2,
        min_weight: float = 0.0,
    ) -> list[MemoryRecord]:
        self.__init_edges()
        records_by_id = {r.id: r for r in self._store.get(agent_id, [])}
        visited: set[str] = {start_id}
        frontier = {start_id}

        for _ in range(depth):
            next_frontier: set[str] = set()
            for node_id in frontier:
                for edge in self._edges.get(agent_id, []):
                    if edge["weight"] < min_weight:
                        continue
                    neighbor = None
                    if edge["from_id"] == node_id:
                        neighbor = edge["to_id"]
                    elif edge["to_id"] == node_id:
                        neighbor = edge["from_id"]
                    if neighbor and neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.add(neighbor)
            frontier = next_frontier
            if not frontier:
                break

        visited.discard(start_id)
        return [records_by_id[rid] for rid in visited if rid in records_by_id]

    async def find_clusters(
        self,
        agent_id: str,
        *,
        tag: str | None = None,
        min_importance: float = 0.0,
        threshold: float = 0.5,
    ) -> list[list[MemoryRecord]]:
        self.__init_edges()
        records = self._store.get(agent_id, [])
        filtered = [
            r for r in records
            if r.importance >= min_importance
            and (not tag or tag in r.tags)
        ]
        if not filtered:
            return []

        # Simple union-find clustering via edges above threshold
        parent: dict[str, str] = {r.id: r.id for r in filtered}
        id_set = set(parent.keys())

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for edge in self._edges.get(agent_id, []):
            if edge["weight"] < threshold:
                continue
            a, b = edge["from_id"], edge["to_id"]
            if a in id_set and b in id_set:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

        groups: dict[str, list[MemoryRecord]] = {}
        records_by_id = {r.id: r for r in filtered}
        for rid in id_set:
            root = find(rid)
            groups.setdefault(root, []).append(records_by_id[rid])

        return [cluster for cluster in groups.values() if len(cluster) > 1]
