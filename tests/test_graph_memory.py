"""Tests for the graph memory protocol extension and InMemoryAdapter graph ops."""

from __future__ import annotations

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import GraphMemoryAdapter, MemoryRecord


# ---------------------------------------------------------------------------
# Extended MemoryRecord fields
# ---------------------------------------------------------------------------


class TestMemoryRecordExtended:
    def test_new_fields_default(self) -> None:
        record = MemoryRecord(id="1", content="test", agent_id="a")
        assert record.outcome == ""
        assert record.emotion_dimensions == {}
        assert record.prediction_error == 0.0
        assert record.edges == []

    def test_new_fields_populated(self) -> None:
        record = MemoryRecord(
            id="1", content="test", agent_id="a",
            outcome="success",
            emotion_dimensions={"confidence": 0.8, "frustration": 0.1},
            prediction_error=0.3,
            edges=[{"to": "2", "rel": "SIMILAR_TO", "weight": 0.9}],
        )
        assert record.outcome == "success"
        assert record.emotion_dimensions["confidence"] == 0.8
        assert record.prediction_error == 0.3
        assert len(record.edges) == 1


# ---------------------------------------------------------------------------
# InMemoryAdapter — graph extensions
# ---------------------------------------------------------------------------


class TestInMemoryGraphOps:
    @pytest.mark.asyncio
    async def test_create_and_get_edges(self) -> None:
        adapter = InMemoryAdapter()
        m1 = await adapter.store("a", "experience 1")
        m2 = await adapter.store("a", "experience 2")

        await adapter.create_edge("a", m1.id, m2.id, "SIMILAR_TO", weight=0.85)

        edges = await adapter.get_edges("a", m1.id)
        assert len(edges) == 1
        assert edges[0]["relationship"] == "SIMILAR_TO"
        assert edges[0]["weight"] == 0.85

    @pytest.mark.asyncio
    async def test_get_edges_bidirectional(self) -> None:
        adapter = InMemoryAdapter()
        m1 = await adapter.store("a", "exp 1")
        m2 = await adapter.store("a", "exp 2")

        await adapter.create_edge("a", m1.id, m2.id, "FOLLOWED_BY")

        # Edge visible from both sides
        edges_from_m1 = await adapter.get_edges("a", m1.id)
        edges_from_m2 = await adapter.get_edges("a", m2.id)
        assert len(edges_from_m1) == 1
        assert len(edges_from_m2) == 1

    @pytest.mark.asyncio
    async def test_traverse_depth_1(self) -> None:
        adapter = InMemoryAdapter()
        m1 = await adapter.store("a", "start")
        m2 = await adapter.store("a", "neighbor")
        m3 = await adapter.store("a", "far")

        await adapter.create_edge("a", m1.id, m2.id, "SIMILAR_TO", weight=0.9)
        await adapter.create_edge("a", m2.id, m3.id, "SIMILAR_TO", weight=0.9)

        result = await adapter.traverse("a", m1.id, depth=1)
        result_ids = {r.id for r in result}
        assert m2.id in result_ids
        assert m3.id not in result_ids  # depth=1 only reaches m2

    @pytest.mark.asyncio
    async def test_traverse_depth_2(self) -> None:
        adapter = InMemoryAdapter()
        m1 = await adapter.store("a", "start")
        m2 = await adapter.store("a", "neighbor")
        m3 = await adapter.store("a", "far")

        await adapter.create_edge("a", m1.id, m2.id, "SIMILAR_TO", weight=0.9)
        await adapter.create_edge("a", m2.id, m3.id, "SIMILAR_TO", weight=0.9)

        result = await adapter.traverse("a", m1.id, depth=2)
        result_ids = {r.id for r in result}
        assert m2.id in result_ids
        assert m3.id in result_ids

    @pytest.mark.asyncio
    async def test_traverse_respects_min_weight(self) -> None:
        adapter = InMemoryAdapter()
        m1 = await adapter.store("a", "start")
        m2 = await adapter.store("a", "weak link")

        await adapter.create_edge("a", m1.id, m2.id, "SIMILAR_TO", weight=0.2)

        result = await adapter.traverse("a", m1.id, depth=1, min_weight=0.5)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_find_clusters(self) -> None:
        adapter = InMemoryAdapter()
        m1 = await adapter.store("a", "cluster A node 1", importance=7.0)
        m2 = await adapter.store("a", "cluster A node 2", importance=7.0)
        m3 = await adapter.store("a", "isolated node", importance=7.0)

        await adapter.create_edge("a", m1.id, m2.id, "SIMILAR_TO", weight=0.9)

        clusters = await adapter.find_clusters("a", min_importance=6.0, threshold=0.5)
        assert len(clusters) == 1
        cluster_ids = {r.id for r in clusters[0]}
        assert m1.id in cluster_ids
        assert m2.id in cluster_ids
        assert m3.id not in cluster_ids

    @pytest.mark.asyncio
    async def test_find_clusters_with_tag_filter(self) -> None:
        adapter = InMemoryAdapter()
        m1 = await adapter.store("a", "tagged 1", importance=7.0, tags=["invoice"])
        m2 = await adapter.store("a", "tagged 2", importance=7.0, tags=["invoice"])
        m3 = await adapter.store("a", "untagged", importance=7.0, tags=["other"])

        await adapter.create_edge("a", m1.id, m2.id, "SIMILAR_TO", weight=0.9)
        await adapter.create_edge("a", m1.id, m3.id, "SIMILAR_TO", weight=0.9)

        clusters = await adapter.find_clusters("a", tag="invoice", threshold=0.5)
        assert len(clusters) == 1
        cluster_ids = {r.id for r in clusters[0]}
        assert m1.id in cluster_ids
        assert m2.id in cluster_ids

    @pytest.mark.asyncio
    async def test_find_clusters_below_threshold(self) -> None:
        adapter = InMemoryAdapter()
        m1 = await adapter.store("a", "node 1", importance=7.0)
        m2 = await adapter.store("a", "node 2", importance=7.0)

        await adapter.create_edge("a", m1.id, m2.id, "SIMILAR_TO", weight=0.3)

        clusters = await adapter.find_clusters("a", threshold=0.5)
        assert len(clusters) == 0

    @pytest.mark.asyncio
    async def test_edges_isolated_per_agent(self) -> None:
        adapter = InMemoryAdapter()
        m1 = await adapter.store("a", "agent a")
        m2 = await adapter.store("b", "agent b")

        await adapter.create_edge("a", m1.id, "fake", "LINK")

        edges_b = await adapter.get_edges("b", m2.id)
        assert len(edges_b) == 0


# ---------------------------------------------------------------------------
# Neo4j adapter import (no live connection needed)
# ---------------------------------------------------------------------------


class TestNeo4jAdapterImport:
    def test_import_and_init(self) -> None:
        from cortiva.adapters.memory.neo4j import Neo4jMemoryAdapter
        adapter = Neo4jMemoryAdapter(
            uri="bolt://localhost:7687",
            username="neo4j",
            password="test",
        )
        assert adapter._uri == "bolt://localhost:7687"

    def test_config_registry(self) -> None:
        from cortiva.core.config import _MEMORY_ADAPTERS
        assert "neo4j" in _MEMORY_ADAPTERS


# ---------------------------------------------------------------------------
# GraphMemoryAdapter protocol check
# ---------------------------------------------------------------------------


class TestGraphProtocol:
    def test_inmemory_has_graph_methods(self) -> None:
        adapter = InMemoryAdapter()
        assert hasattr(adapter, "create_edge")
        assert hasattr(adapter, "get_edges")
        assert hasattr(adapter, "traverse")
        assert hasattr(adapter, "find_clusters")
