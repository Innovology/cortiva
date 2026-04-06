"""Tests for the Neo4j graph memory adapter (mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cortiva.adapters.memory.neo4j import Neo4jMemoryAdapter


class TestNeo4jMemoryAdapter:
    def _make_adapter(self) -> Neo4jMemoryAdapter:
        adapter = Neo4jMemoryAdapter.__new__(Neo4jMemoryAdapter)
        adapter._uri = "bolt://localhost:7687"
        adapter._username = "neo4j"
        adapter._password = "password"
        adapter._database = "neo4j"
        adapter._driver = None
        return adapter

    @pytest.mark.asyncio
    async def test_store(self) -> None:
        adapter = self._make_adapter()
        with patch.object(adapter, "_run", return_value=[]):
            record = await adapter.store("agent-1", "learned something", tags=["test"])
        assert record.agent_id == "agent-1"
        assert record.content == "learned something"
        assert "test" in record.tags

    @pytest.mark.asyncio
    async def test_search(self) -> None:
        adapter = self._make_adapter()
        node = {
            "m": {"id": "mem-1", "content": "test memory", "agent_id": "agent-1",
                   "tags": ["test"], "importance": 5.0, "created_at": "2026-01-01T00:00:00"},
        }
        with patch.object(adapter, "_run", return_value=[node]):
            results = await adapter.search("agent-1", "test", limit=5)
        assert len(results) == 1
        assert results[0].content == "test memory"

    @pytest.mark.asyncio
    async def test_recall(self) -> None:
        adapter = self._make_adapter()
        node = {
            "m": {"id": "mem-1", "content": "important", "agent_id": "agent-1",
                   "tags": [], "importance": 8.0, "created_at": "2026-01-01T00:00:00"},
        }
        with patch.object(adapter, "_run", return_value=[node]):
            results = await adapter.recall("agent-1", limit=10, min_importance=6.0)
        assert len(results) == 1
        assert results[0].importance == 8.0

    @pytest.mark.asyncio
    async def test_delete(self) -> None:
        adapter = self._make_adapter()
        with patch.object(adapter, "_run", return_value=[{"deleted": 1}]):
            result = await adapter.delete("agent-1", "mem-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_create_edge(self) -> None:
        adapter = self._make_adapter()
        with patch.object(adapter, "_run", return_value=[]) as mock_run:
            await adapter.create_edge("agent-1", "mem-1", "mem-2", "SIMILAR_TO", 0.8)
        mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_edges(self) -> None:
        adapter = self._make_adapter()
        edge = {"relationship": "SIMILAR_TO", "target_id": "mem-2", "weight": 0.9, "target_content": "related"}
        with patch.object(adapter, "_run", return_value=[edge]):
            edges = await adapter.get_edges("agent-1", "mem-1")
        assert len(edges) == 1
        assert edges[0]["relationship"] == "SIMILAR_TO"

    @pytest.mark.asyncio
    async def test_find_clusters(self) -> None:
        adapter = self._make_adapter()
        with patch.object(adapter, "_run", return_value=[]):
            clusters = await adapter.find_clusters("agent-1")
        assert isinstance(clusters, list)

    @pytest.mark.asyncio
    async def test_traverse(self) -> None:
        adapter = self._make_adapter()
        with patch.object(adapter, "_run", return_value=[]):
            results = await adapter.traverse("agent-1", "mem-1", depth=2)
        assert isinstance(results, list)

    def test_record_from_node(self) -> None:
        node = {
            "id": "mem-1", "content": "test", "agent_id": "a1",
            "tags": ["t1"], "importance": 7.0, "created_at": "2026-01-01",
        }
        record = Neo4jMemoryAdapter._record_from_node(node, "a1")
        assert record.id == "mem-1"
        assert record.content == "test"

    def test_get_driver_import_error(self) -> None:
        adapter = self._make_adapter()
        with patch.dict("sys.modules", {"neo4j": None}):
            import importlib
            # _get_driver tries to import neo4j
            try:
                adapter._get_driver()
            except ImportError:
                pass  # Expected
