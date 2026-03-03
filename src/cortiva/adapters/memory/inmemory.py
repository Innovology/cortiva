"""
In-memory adapter for testing and development.

No external dependencies. Memories live in a dict and die with the process.
Useful for unit tests and local development before wiring up real storage.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from cortiva.adapters.protocols import MemoryAdapter, MemoryRecord


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
