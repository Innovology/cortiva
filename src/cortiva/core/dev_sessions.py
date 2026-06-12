"""Detached, capped, steerable dev sessions — the agent-as-driver runtime.

An agent's hands-on work runs as a *detached* Claude Code session, not a
blocking call inside the heartbeat cycle. That matters for two reasons:

1. **No fleet freeze.** The single global heartbeat awaits every agent's cycle
   before the next tick; a blocking 20-minute session would stall every other
   agent's wakes/sleeps/reassess. A detached session lets the launching cycle
   return immediately — the long work happens off the heartbeat and is *reaped*
   at a later cycle the agent controls (so no concurrent mutation of its state).
2. **Parallelism with a cap.** An agent may run up to ``max_per_agent`` sessions
   at once (founder cap = 2): Slot A (the main deep task) and Slot B (fast/meta —
   voice, deep_think, and questioning Slot A's output). This manager is the
   governor; Myelin decides what goes in each slot.

This module only *schedules and tracks* sessions. The actual session logic (what
prompt, which model, the steering, the Slot-B critic) lives in the fabric, which
holds the agent context — it hands us a coroutine and we run it detached.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SessionResult:
    """The outcome of a detached dev session, reaped by the agent's cycle."""

    agent_id: str
    task_id: str
    ok: bool
    outcome: str = ""
    error: str = ""
    session_id: str = ""
    tools_used: int = 0
    critique: str = ""  # Slot-B's verdict, if it was run
    meta: dict[str, Any] = field(default_factory=dict)


class DevSessionManager:
    """Tracks each agent's in-flight dev sessions and enforces the per-agent cap.

    Concurrency-safe by construction: detached tasks never touch agent state;
    they only deposit a :class:`SessionResult` here, which the agent drains and
    applies inside its own (single-threaded) cycle.
    """

    def __init__(self, *, max_per_agent: int = 2) -> None:
        self._max = max_per_agent
        self._active: dict[str, set[asyncio.Task]] = defaultdict(set)
        self._completed: dict[str, list[SessionResult]] = defaultdict(list)
        # Tasks currently owned by a live session, so the cycle never
        # double-launches the same work.
        self._in_flight_tasks: dict[str, set[str]] = defaultdict(set)

    def active_count(self, agent_id: str) -> int:
        return len(self._active.get(agent_id, ()))

    def total_active(self) -> int:
        return sum(len(s) for s in self._active.values())

    def can_launch(self, agent_id: str) -> bool:
        return self.active_count(agent_id) < self._max

    def is_in_flight(self, agent_id: str, task_id: str) -> bool:
        return task_id in self._in_flight_tasks.get(agent_id, ())

    def launch(
        self,
        agent_id: str,
        task_id: str,
        runner: Callable[[], Awaitable[SessionResult]],
    ) -> bool:
        """Start ``runner`` detached for ``agent_id``. Returns False if the
        agent is already at its concurrency cap (caller should leave the task
        pending and retry on a later cycle)."""
        if not self.can_launch(agent_id):
            return False
        self._in_flight_tasks[agent_id].add(task_id)

        async def _wrapped() -> None:
            try:
                result = await runner()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let a session crash the fabric
                logger.exception("Dev session crashed for %s/%s", agent_id, task_id)
                result = SessionResult(
                    agent_id=agent_id,
                    task_id=task_id,
                    ok=False,
                    error=f"session crashed: {exc}",
                )
            self._completed[agent_id].append(result)
            self._in_flight_tasks[agent_id].discard(task_id)

        task = asyncio.ensure_future(_wrapped())
        self._active[agent_id].add(task)
        task.add_done_callback(lambda t: self._active[agent_id].discard(t))
        logger.info(
            "Launched dev session for %s/%s (%d/%d active)",
            agent_id,
            task_id,
            self.active_count(agent_id),
            self._max,
        )
        return True

    def drain_completed(self, agent_id: str) -> list[SessionResult]:
        """Pop all finished session results for an agent, for it to reap."""
        out = self._completed.get(agent_id, [])
        self._completed[agent_id] = []
        return out

    async def shutdown(self) -> None:
        """Cancel all in-flight sessions (graceful reload / stop)."""
        tasks = [t for s in self._active.values() for t in s]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        self._in_flight_tasks.clear()
