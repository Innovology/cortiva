"""
Custom MemoryAdapter example — LoggingMemoryAdapter.

Demonstrates how to write a custom adapter that satisfies the MemoryAdapter
protocol by wrapping an existing implementation and adding logging.

Run with:
    PYTHONPATH=src python3 examples/custom_adapter.py
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import (
    ConsciousResponse,
    MemoryRecord,
    Priority,
)
from cortiva.core.fabric import Fabric

# ---------------------------------------------------------------------------
# Custom adapter: wraps InMemoryAdapter and logs every call
# ---------------------------------------------------------------------------


class LoggingMemoryAdapter:
    """A MemoryAdapter that delegates to an inner adapter and logs operations."""

    def __init__(self, inner: InMemoryAdapter | None = None) -> None:
        self._inner = inner or InMemoryAdapter()

    # -- helpers --

    @staticmethod
    def _log(operation: str, agent_id: str, detail: str = "") -> None:
        suffix = f" | {detail}" if detail else ""
        print(f"[memory] {operation:<8} agent={agent_id}{suffix}")

    # -- MemoryAdapter protocol --

    async def store(
        self,
        agent_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 5.0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        self._log("store", agent_id, f"content={content!r}")
        return await self._inner.store(
            agent_id, content, tags=tags, importance=importance, metadata=metadata
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
        self._log("search", agent_id, f"query={query!r} limit={limit}")
        results = await self._inner.search(
            agent_id, query, limit=limit, min_importance=min_importance, tags=tags
        )
        self._log("search", agent_id, f"found {len(results)} result(s)")
        return results

    async def recall(
        self,
        agent_id: str,
        *,
        limit: int = 20,
        min_importance: float = 0.0,
    ) -> list[MemoryRecord]:
        self._log("recall", agent_id, f"limit={limit}")
        results = await self._inner.recall(
            agent_id, limit=limit, min_importance=min_importance
        )
        self._log("recall", agent_id, f"returned {len(results)} record(s)")
        return results

    async def delete(self, agent_id: str, memory_id: str) -> bool:
        self._log("delete", agent_id, f"memory_id={memory_id}")
        removed = await self._inner.delete(agent_id, memory_id)
        self._log("delete", agent_id, f"removed={removed}")
        return removed


# ---------------------------------------------------------------------------
# Minimal mock ConsciousnessAdapter (just enough to satisfy Fabric)
# ---------------------------------------------------------------------------


class MockConsciousnessAdapter:
    """Returns canned responses so the example can run without an LLM."""

    async def think(
        self,
        agent_id: str,
        context: str,
        prompt: str,
        *,
        priority: Priority = Priority.NORMAL,
        max_tokens: int = 4096,
        metadata: dict[str, Any] | None = None,
    ) -> ConsciousResponse:
        return ConsciousResponse(content="(mock thought)")

    async def reflect(
        self,
        agent_id: str,
        context: str,
        day_summary: str,
    ) -> ConsciousResponse:
        return ConsciousResponse(content="(mock reflection)")


# ---------------------------------------------------------------------------
# Main: wire the custom adapter into a Fabric and exercise the memory API
# ---------------------------------------------------------------------------


async def main() -> None:
    # 1. Create the logging adapter
    memory = LoggingMemoryAdapter()

    # 2. Plug it into a Fabric alongside a mock consciousness adapter
    fabric = Fabric(
        agents_dir="./tmp_agents",
        memory=memory,
        consciousness=MockConsciousnessAdapter(),
    )

    agent_id = "demo-agent"

    # 3. Store some memories
    print("--- storing memories ---")
    rec1 = await fabric.memory.store(
        agent_id,
        "The deploy pipeline is flaky on Mondays.",
        tags=["ops", "deploy"],
        importance=7.0,
    )
    await fabric.memory.store(
        agent_id,
        "User prefers concise status updates.",
        tags=["preferences"],
        importance=9.0,
    )

    # 4. Search for a memory
    print("\n--- searching memories ---")
    await fabric.memory.search(agent_id, "deploy")

    # 5. Recall top memories
    print("\n--- recalling memories ---")
    await fabric.memory.recall(agent_id, limit=5)

    # 6. Delete a memory
    print("\n--- deleting a memory ---")
    await fabric.memory.delete(agent_id, rec1.id)

    # 7. Confirm deletion
    print("\n--- recall after deletion ---")
    remaining = await fabric.memory.recall(agent_id)
    print(f"\nRemaining memories: {[r.content for r in remaining]}")

    shutil.rmtree(Path("./tmp_agents"), ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
