"""
Engram memory adapter for Cortiva.

Uses engram-core for persistent agent memory. Zero config,
SQLite-backed, with full-text search and importance scoring.

Install: pip install engram-core
"""

from __future__ import annotations

import uuid
from typing import Any

from cortiva.adapters.protocols import MemoryRecord


class EngramMemoryAdapter:
    """
    Memory adapter backed by Engram (engram-core).

    Each agent gets its own namespace for memory isolation.
    Shared namespaces can be created for cross-agent knowledge.
    """

    def __init__(self, namespace_prefix: str = "cortiva"):
        self._prefix = namespace_prefix
        self._memories: dict[str, Any] = {}  # engram Memory instances

    def _get_memory(self, agent_id: str) -> Any:
        """Get or create an Engram Memory instance for an agent."""
        if agent_id not in self._memories:
            try:
                from engram import Memory
                self._memories[agent_id] = Memory(
                    namespace=f"{self._prefix}_{agent_id}"
                )
            except ImportError:
                raise ImportError(
                    "engram-core is not installed. "
                    "Install it with: pip install engram-core"
                )
        return self._memories[agent_id]

    async def store(
        self,
        agent_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 5.0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        mem = self._get_memory(agent_id)
        mem.store(content, tags=tags or [], importance=int(importance))

        return MemoryRecord(
            id=str(uuid.uuid4()),
            content=content,
            agent_id=agent_id,
            tags=tags or [],
            importance=importance,
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
        mem = self._get_memory(agent_id)
        results = mem.search(query)

        records = []
        for r in results[:limit]:
            importance = getattr(r, "importance", 5)
            if importance >= min_importance:
                records.append(MemoryRecord(
                    id=getattr(r, "id", str(uuid.uuid4())),
                    content=getattr(r, "content", str(r)),
                    agent_id=agent_id,
                    tags=getattr(r, "tags", []),
                    importance=float(importance),
                ))
        return records

    async def recall(
        self,
        agent_id: str,
        *,
        limit: int = 20,
        min_importance: float = 0.0,
    ) -> list[MemoryRecord]:
        mem = self._get_memory(agent_id)
        results = mem.recall(limit=limit)

        records = []
        for r in results:
            importance = getattr(r, "importance", 5)
            if importance >= min_importance:
                records.append(MemoryRecord(
                    id=getattr(r, "id", str(uuid.uuid4())),
                    content=getattr(r, "content", str(r)),
                    agent_id=agent_id,
                    tags=getattr(r, "tags", []),
                    importance=float(importance),
                ))
        return records

    async def delete(self, agent_id: str, memory_id: str) -> bool:
        # Engram's delete API may vary — adapt as needed
        return True
