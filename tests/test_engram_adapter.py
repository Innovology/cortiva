"""Tests for the Engram memory adapter."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from cortiva.adapters.protocols import MemoryRecord


@pytest.fixture
def mock_engram():
    """Provide a mock engram module so tests don't need engram-core installed."""
    mock_module = ModuleType("engram")
    mock_memory_cls = MagicMock()
    mock_module.Memory = mock_memory_cls
    with patch.dict(sys.modules, {"engram": mock_module}):
        yield mock_memory_cls


class TestEngramMemoryAdapter:
    def test_init_defaults(self) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        adapter = EngramMemoryAdapter()
        assert adapter._prefix == "cortiva"
        assert adapter._memories == {}

    def test_init_custom_prefix(self) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        adapter = EngramMemoryAdapter(namespace_prefix="custom")
        assert adapter._prefix == "custom"

    def test_get_memory_creates_instance(self, mock_engram: MagicMock) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        adapter = EngramMemoryAdapter()
        mem = adapter._get_memory("agent-1")
        mock_engram.assert_called_once_with(namespace="cortiva_agent-1")
        assert "agent-1" in adapter._memories

    def test_get_memory_caches_instance(self, mock_engram: MagicMock) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        adapter = EngramMemoryAdapter()
        mem1 = adapter._get_memory("agent-1")
        mem2 = adapter._get_memory("agent-1")
        # Should only create once
        mock_engram.assert_called_once()
        assert mem1 is mem2

    def test_get_memory_raises_without_engram(self) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        adapter = EngramMemoryAdapter()
        with patch.dict(sys.modules, {"engram": None}):
            # Force re-import attempt
            adapter._memories.clear()
            with pytest.raises(ImportError, match="engram-core"):
                adapter._get_memory("agent-1")

    @pytest.mark.asyncio
    async def test_store(self, mock_engram: MagicMock) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        mock_mem_instance = MagicMock()
        mock_engram.return_value = mock_mem_instance

        adapter = EngramMemoryAdapter()
        record = await adapter.store(
            "agent-1",
            "important fact",
            tags=["knowledge"],
            importance=8.0,
            metadata={"source": "test"},
        )

        mock_mem_instance.store.assert_called_once_with(
            "important fact", tags=["knowledge"], importance=8
        )
        assert isinstance(record, MemoryRecord)
        assert record.content == "important fact"
        assert record.agent_id == "agent-1"
        assert record.tags == ["knowledge"]
        assert record.importance == 8.0
        assert record.metadata == {"source": "test"}

    @pytest.mark.asyncio
    async def test_store_defaults(self, mock_engram: MagicMock) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        mock_mem_instance = MagicMock()
        mock_engram.return_value = mock_mem_instance

        adapter = EngramMemoryAdapter()
        record = await adapter.store("agent-1", "data")

        mock_mem_instance.store.assert_called_once_with(
            "data", tags=[], importance=5
        )
        assert record.tags == []
        assert record.importance == 5.0
        assert record.metadata == {}

    @pytest.mark.asyncio
    async def test_search(self, mock_engram: MagicMock) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        mock_result = MagicMock()
        mock_result.id = "mem-123"
        mock_result.content = "found memory"
        mock_result.tags = ["tag1"]
        mock_result.importance = 7

        mock_mem_instance = MagicMock()
        mock_mem_instance.search.return_value = [mock_result]
        mock_engram.return_value = mock_mem_instance

        adapter = EngramMemoryAdapter()
        records = await adapter.search("agent-1", "query", limit=5)

        mock_mem_instance.search.assert_called_once_with("query")
        assert len(records) == 1
        assert records[0].content == "found memory"
        assert records[0].agent_id == "agent-1"
        assert records[0].importance == 7.0

    @pytest.mark.asyncio
    async def test_search_filters_by_importance(self, mock_engram: MagicMock) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        low = MagicMock()
        low.importance = 2
        low.content = "low"
        low.id = "1"
        low.tags = []

        high = MagicMock()
        high.importance = 9
        high.content = "high"
        high.id = "2"
        high.tags = []

        mock_mem_instance = MagicMock()
        mock_mem_instance.search.return_value = [low, high]
        mock_engram.return_value = mock_mem_instance

        adapter = EngramMemoryAdapter()
        records = await adapter.search("agent-1", "query", min_importance=5.0)

        assert len(records) == 1
        assert records[0].content == "high"

    @pytest.mark.asyncio
    async def test_search_respects_limit(self, mock_engram: MagicMock) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        results = []
        for i in range(10):
            r = MagicMock()
            r.importance = 5
            r.content = f"mem-{i}"
            r.id = str(i)
            r.tags = []
            results.append(r)

        mock_mem_instance = MagicMock()
        mock_mem_instance.search.return_value = results
        mock_engram.return_value = mock_mem_instance

        adapter = EngramMemoryAdapter()
        records = await adapter.search("agent-1", "query", limit=3)

        assert len(records) == 3

    @pytest.mark.asyncio
    async def test_recall(self, mock_engram: MagicMock) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        mock_result = MagicMock()
        mock_result.id = "mem-456"
        mock_result.content = "recalled memory"
        mock_result.tags = ["recall"]
        mock_result.importance = 6

        mock_mem_instance = MagicMock()
        mock_mem_instance.recall.return_value = [mock_result]
        mock_engram.return_value = mock_mem_instance

        adapter = EngramMemoryAdapter()
        records = await adapter.recall("agent-1", limit=10)

        mock_mem_instance.recall.assert_called_once_with(limit=10)
        assert len(records) == 1
        assert records[0].content == "recalled memory"
        assert records[0].importance == 6.0

    @pytest.mark.asyncio
    async def test_recall_filters_by_importance(self, mock_engram: MagicMock) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        low = MagicMock()
        low.importance = 1
        low.content = "low"
        low.id = "1"
        low.tags = []

        mock_mem_instance = MagicMock()
        mock_mem_instance.recall.return_value = [low]
        mock_engram.return_value = mock_mem_instance

        adapter = EngramMemoryAdapter()
        records = await adapter.recall("agent-1", min_importance=5.0)

        assert len(records) == 0

    @pytest.mark.asyncio
    async def test_delete(self, mock_engram: MagicMock) -> None:
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        adapter = EngramMemoryAdapter()
        result = await adapter.delete("agent-1", "mem-123")

        # Current implementation always returns True
        assert result is True

    @pytest.mark.asyncio
    async def test_search_result_without_attributes(self, mock_engram: MagicMock) -> None:
        """Results missing attributes fall back to defaults via getattr."""
        from cortiva.adapters.memory.engram import EngramMemoryAdapter

        # A result object that has no id, content, tags, importance attributes
        bare_result = "just a string"

        mock_mem_instance = MagicMock()
        mock_mem_instance.search.return_value = [bare_result]
        mock_engram.return_value = mock_mem_instance

        adapter = EngramMemoryAdapter()
        records = await adapter.search("agent-1", "query")

        assert len(records) == 1
        assert records[0].content == "just a string"
        assert records[0].importance == 5.0
        assert records[0].tags == []
