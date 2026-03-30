"""
Guarded memory adapter — enforces agent-level isolation on memory access.

Wraps any :class:`~cortiva.adapters.protocols.MemoryAdapter` and blocks
cross-agent queries when isolation is active.  Transparent proxy for
:class:`~cortiva.adapters.protocols.GraphMemoryAdapter` extensions.
"""

from __future__ import annotations

import logging
from typing import Any

from cortiva.adapters.protocols import MemoryRecord
from cortiva.core.isolation import NoIsolation

logger = logging.getLogger("cortiva.memory_guard")

# Sentinel agent_id for org-wide shared memory
SHARED_AGENT_ID = "__org_shared__"


class GuardedMemoryAdapter:
    """Wraps a memory adapter to enforce agent-level isolation.

    All ``MemoryAdapter`` and ``GraphMemoryAdapter`` methods are proxied.
    When the isolation enforcer blocks a cross-agent access, the method
    returns an empty/default result instead of delegating.

    Parameters
    ----------
    inner:
        The real memory adapter to delegate to.
    enforcer:
        The isolation enforcer that decides access policy.
    """

    def __init__(self, inner: Any, enforcer: NoIsolation) -> None:
        self._inner = inner
        self._enforcer = enforcer

    # ------------------------------------------------------------------
    # MemoryAdapter methods
    # ------------------------------------------------------------------

    async def store(
        self,
        agent_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 5.0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        """Store a memory — always allowed (agents write to their own store)."""
        return await self._inner.store(
            agent_id, content, tags=tags, importance=importance, metadata=metadata,
        )

    async def search(
        self,
        agent_id: str,
        query: str,
        *,
        limit: int = 10,
        min_importance: float = 0.0,
        tags: list[str] | None = None,
        _caller_id: str | None = None,
    ) -> list[MemoryRecord]:
        """Search memories — blocked if caller differs from target agent.

        Shared memory (``__org_shared__``) is always readable.
        """
        caller = _caller_id or agent_id
        if agent_id != SHARED_AGENT_ID and not self._enforcer.validate_memory_access(caller, agent_id):
            return []
        return await self._inner.search(
            agent_id, query, limit=limit, min_importance=min_importance, tags=tags,
        )

    async def recall(
        self,
        agent_id: str,
        *,
        limit: int = 20,
        min_importance: float = 0.0,
        _caller_id: str | None = None,
    ) -> list[MemoryRecord]:
        """Recall memories — blocked if caller differs from target agent.

        Shared memory (``__org_shared__``) is always readable.
        """
        caller = _caller_id or agent_id
        if agent_id != SHARED_AGENT_ID and not self._enforcer.validate_memory_access(caller, agent_id):
            return []
        return await self._inner.recall(
            agent_id, limit=limit, min_importance=min_importance,
        )

    async def delete(
        self,
        agent_id: str,
        memory_id: str,
        _caller_id: str | None = None,
    ) -> bool:
        """Delete a memory — blocked if caller differs from target agent."""
        caller = _caller_id or agent_id
        if not self._enforcer.validate_memory_access(caller, agent_id):
            return False
        return await self._inner.delete(agent_id, memory_id)

    # ------------------------------------------------------------------
    # Shared memory tier (org-wide knowledge)
    # ------------------------------------------------------------------

    async def store_shared(
        self,
        caller_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 5.0,
    ) -> MemoryRecord:
        """Store a memory in the org-wide shared tier.

        Any agent can write.  The memory is stored under
        :const:`SHARED_AGENT_ID`.
        """
        all_tags = (tags or []) + ["shared", f"author:{caller_id}"]
        return await self._inner.store(
            SHARED_AGENT_ID, content, tags=all_tags, importance=importance,
            metadata={"author": caller_id},
        )

    async def search_shared(
        self,
        query: str,
        *,
        limit: int = 10,
        min_importance: float = 0.0,
    ) -> list[MemoryRecord]:
        """Search org-wide shared memories.  Always allowed."""
        return await self._inner.search(
            SHARED_AGENT_ID, query, limit=limit, min_importance=min_importance,
        )

    async def recall_shared(
        self,
        *,
        limit: int = 20,
        min_importance: float = 0.0,
    ) -> list[MemoryRecord]:
        """Recall high-importance shared memories."""
        return await self._inner.recall(
            SHARED_AGENT_ID, limit=limit, min_importance=min_importance,
        )

    # ------------------------------------------------------------------
    # GraphMemoryAdapter methods (proxied if inner supports them)
    # ------------------------------------------------------------------

    async def create_edge(
        self,
        agent_id: str,
        from_id: str,
        to_id: str,
        relationship: str,
        weight: float = 1.0,
    ) -> None:
        if hasattr(self._inner, "create_edge"):
            await self._inner.create_edge(agent_id, from_id, to_id, relationship, weight)

    async def find_clusters(
        self,
        agent_id: str,
        *,
        tag: str | None = None,
        min_importance: float = 0.0,
        threshold: float = 0.5,
    ) -> list[list[MemoryRecord]]:
        if hasattr(self._inner, "find_clusters"):
            return await self._inner.find_clusters(
                agent_id, tag=tag, min_importance=min_importance, threshold=threshold,
            )
        return []

    async def traverse(
        self,
        agent_id: str,
        start_id: str,
        *,
        depth: int = 2,
        min_weight: float = 0.0,
    ) -> list[MemoryRecord]:
        if hasattr(self._inner, "traverse"):
            return await self._inner.traverse(
                agent_id, start_id, depth=depth, min_weight=min_weight,
            )
        return []

    async def get_edges(
        self,
        agent_id: str,
        memory_id: str,
    ) -> list[dict[str, Any]]:
        if hasattr(self._inner, "get_edges"):
            return await self._inner.get_edges(agent_id, memory_id)
        return []

    # ------------------------------------------------------------------
    # Transparent proxy for any other attributes
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
