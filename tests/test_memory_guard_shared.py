"""Tests for GuardedMemoryAdapter shared memory and graph proxy methods."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cortiva.core.isolation import NoIsolation, SoftIsolation
from cortiva.core.memory_guard import SHARED_AGENT_ID, GuardedMemoryAdapter


class TestSharedMemory:
    @pytest.mark.asyncio
    async def test_store_shared(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.store.return_value = "shared-record"
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.store_shared(
            "agent-1", "org knowledge", tags=["policy"], importance=7.0
        )

        inner.store.assert_called_once_with(
            SHARED_AGENT_ID,
            "org knowledge",
            tags=["policy", "shared", "author:agent-1"],
            importance=7.0,
            metadata={"author": "agent-1"},
        )
        assert result == "shared-record"

    @pytest.mark.asyncio
    async def test_store_shared_default_tags(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.store.return_value = "record"
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        await guard.store_shared("agent-2", "content")

        call_kwargs = inner.store.call_args
        assert call_kwargs.kwargs["tags"] == ["shared", "author:agent-2"]

    @pytest.mark.asyncio
    async def test_search_shared(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.search.return_value = ["shared-result"]
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.search_shared("query", limit=5, min_importance=3.0)

        inner.search.assert_called_once_with(
            SHARED_AGENT_ID, "query", limit=5, min_importance=3.0,
        )
        assert result == ["shared-result"]

    @pytest.mark.asyncio
    async def test_recall_shared(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.recall.return_value = ["recalled"]
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.recall_shared(limit=10, min_importance=2.0)

        inner.recall.assert_called_once_with(
            SHARED_AGENT_ID, limit=10, min_importance=2.0,
        )
        assert result == ["recalled"]


class TestSharedAgentIdBypass:
    """SHARED_AGENT_ID bypasses isolation in search and recall."""

    @pytest.mark.asyncio
    async def test_search_shared_agent_id_bypasses_isolation(
        self, tmp_path: Path
    ) -> None:
        inner = AsyncMock()
        inner.search.return_value = ["result"]
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        # Cross-agent search with SHARED_AGENT_ID should succeed even under
        # SoftIsolation because of the bypass
        result = await guard.search(
            SHARED_AGENT_ID, "query", _caller_id="agent-1"
        )
        assert result == ["result"]
        inner.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_recall_shared_agent_id_bypasses_isolation(
        self, tmp_path: Path
    ) -> None:
        inner = AsyncMock()
        inner.recall.return_value = ["result"]
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.recall(
            SHARED_AGENT_ID, _caller_id="agent-1"
        )
        assert result == ["result"]
        inner.recall.assert_called_once()

    @pytest.mark.asyncio
    async def test_regular_cross_agent_search_still_blocked(
        self, tmp_path: Path
    ) -> None:
        inner = AsyncMock()
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.search("agent-2", "query", _caller_id="agent-1")
        assert result == []
        inner.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_regular_cross_agent_recall_still_blocked(
        self, tmp_path: Path
    ) -> None:
        inner = AsyncMock()
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.recall("agent-2", _caller_id="agent-1")
        assert result == []
        inner.recall.assert_not_called()


class TestGraphMemoryAdapterProxy:
    @pytest.mark.asyncio
    async def test_create_edge_proxied(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.create_edge = AsyncMock()
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        await guard.create_edge("agent-1", "from-id", "to-id", "similar", 0.9)
        inner.create_edge.assert_called_once_with(
            "agent-1", "from-id", "to-id", "similar", 0.9
        )

    @pytest.mark.asyncio
    async def test_create_edge_skipped_when_not_supported(
        self, tmp_path: Path
    ) -> None:
        inner = AsyncMock(spec=[])  # No create_edge attribute
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        # Should not raise
        await guard.create_edge("agent-1", "from", "to", "rel")

    @pytest.mark.asyncio
    async def test_find_clusters_proxied(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.find_clusters = AsyncMock(return_value=[["cluster"]])
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.find_clusters("agent-1", tag="test")
        assert result == [["cluster"]]

    @pytest.mark.asyncio
    async def test_find_clusters_returns_empty_when_not_supported(
        self, tmp_path: Path
    ) -> None:
        inner = AsyncMock(spec=[])
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.find_clusters("agent-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_traverse_proxied(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.traverse = AsyncMock(return_value=["node1", "node2"])
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.traverse("agent-1", "start-id", depth=3)
        assert result == ["node1", "node2"]

    @pytest.mark.asyncio
    async def test_traverse_returns_empty_when_not_supported(
        self, tmp_path: Path
    ) -> None:
        inner = AsyncMock(spec=[])
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.traverse("agent-1", "start-id")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_edges_proxied(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.get_edges = AsyncMock(return_value=[{"to": "node2"}])
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.get_edges("agent-1", "mem-1")
        assert result == [{"to": "node2"}]

    @pytest.mark.asyncio
    async def test_get_edges_returns_empty_when_not_supported(
        self, tmp_path: Path
    ) -> None:
        inner = AsyncMock(spec=[])
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.get_edges("agent-1", "mem-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_delete_allowed_same_agent(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.delete.return_value = True
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.delete("agent-1", "mem-1", _caller_id="agent-1")
        assert result is True
        inner.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_blocked_cross_agent(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)

        result = await guard.delete("agent-2", "mem-1", _caller_id="agent-1")
        assert result is False
        inner.delete.assert_not_called()
