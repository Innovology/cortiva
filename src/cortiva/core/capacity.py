"""
Node capacity and contention tracking.

Tracks resource utilisation across the node and measures how much time
agents spend waiting for shared resources (LLM API, heartbeat slot).

Key metrics:

- **Capacity**: CPU, RAM, active agent count vs. max concurrent.
- **Queue wait time**: How long a task sits in "pending" before
  execution starts.
- **Heartbeat contention**: How long each agent waits for its turn
  in the serial heartbeat loop.
- **Consciousness contention**: Time spent waiting for LLM API
  responses (blocks other agents in the serial loop).
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("cortiva.capacity")


@dataclass
class TaskTiming:
    """Timing data for a single task execution."""

    agent_id: str
    task_id: str
    queued_at: float = 0.0
    """Timestamp when task entered pending state."""

    started_at: float = 0.0
    """Timestamp when task execution began."""

    finished_at: float = 0.0
    """Timestamp when task execution completed."""

    consciousness_wait: float = 0.0
    """Seconds spent waiting for the LLM API response."""

    @property
    def queue_wait(self) -> float:
        """Seconds the task waited in the queue before execution."""
        if self.queued_at and self.started_at:
            return self.started_at - self.queued_at
        return 0.0

    @property
    def execution_time(self) -> float:
        """Total execution time in seconds."""
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "queue_wait_s": round(self.queue_wait, 2),
            "execution_s": round(self.execution_time, 2),
            "consciousness_wait_s": round(self.consciousness_wait, 2),
        }


@dataclass
class HeartbeatTiming:
    """Timing data for a single heartbeat cycle."""

    started_at: float = 0.0
    finished_at: float = 0.0
    agent_timings: dict[str, float] = field(default_factory=dict)
    """Per-agent wall-clock time within this heartbeat."""

    @property
    def total_time(self) -> float:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return 0.0

    @property
    def idle_time(self) -> float:
        """Time not spent on any agent (scheduling overhead)."""
        agent_total = sum(self.agent_timings.values())
        return max(0.0, self.total_time - agent_total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_s": round(self.total_time, 2),
            "idle_s": round(self.idle_time, 2),
            "agents": {
                aid: round(t, 2) for aid, t in self.agent_timings.items()
            },
        }


class CapacityTracker:
    """Tracks node capacity and contention metrics.

    Instantiated once per Fabric.  Call the ``start_*`` / ``finish_*``
    methods from the heartbeat loop and task execution path to
    accumulate timing data.
    """

    def __init__(self, max_history: int = 100) -> None:
        self._task_timings: list[TaskTiming] = []
        self._heartbeat_timings: list[HeartbeatTiming] = []
        self._max_history = max_history
        self._current_heartbeat: HeartbeatTiming | None = None
        self._active_tasks: dict[str, TaskTiming] = {}

    # ----- Task timing -----

    def task_queued(self, agent_id: str, task_id: str) -> None:
        """Record that a task entered the pending queue."""
        key = f"{agent_id}:{task_id}"
        self._active_tasks[key] = TaskTiming(
            agent_id=agent_id,
            task_id=task_id,
            queued_at=time.monotonic(),
        )

    def task_started(self, agent_id: str, task_id: str) -> None:
        """Record that task execution has begun."""
        key = f"{agent_id}:{task_id}"
        timing = self._active_tasks.get(key)
        if timing:
            timing.started_at = time.monotonic()
        else:
            # Task wasn't tracked from queue — record start only
            self._active_tasks[key] = TaskTiming(
                agent_id=agent_id,
                task_id=task_id,
                started_at=time.monotonic(),
            )

    def task_finished(
        self, agent_id: str, task_id: str, consciousness_wait: float = 0.0,
    ) -> TaskTiming | None:
        """Record task completion and return the timing data."""
        key = f"{agent_id}:{task_id}"
        timing = self._active_tasks.pop(key, None)
        if timing is None:
            return None
        timing.finished_at = time.monotonic()
        timing.consciousness_wait = consciousness_wait
        self._task_timings.append(timing)
        if len(self._task_timings) > self._max_history:
            self._task_timings.pop(0)
        return timing

    # ----- Heartbeat timing -----

    def heartbeat_start(self) -> None:
        """Record the start of a heartbeat cycle."""
        self._current_heartbeat = HeartbeatTiming(started_at=time.monotonic())

    def agent_cycle_start(self, agent_id: str) -> float:
        """Record when an agent's cycle begins within a heartbeat.

        Returns the start timestamp for use with :meth:`agent_cycle_end`.
        """
        return time.monotonic()

    def agent_cycle_end(self, agent_id: str, start: float) -> None:
        """Record when an agent's cycle ends within a heartbeat."""
        if self._current_heartbeat is not None:
            self._current_heartbeat.agent_timings[agent_id] = time.monotonic() - start

    def heartbeat_end(self) -> HeartbeatTiming | None:
        """Record the end of a heartbeat cycle."""
        hb = self._current_heartbeat
        if hb is None:
            return None
        hb.finished_at = time.monotonic()
        self._current_heartbeat = None
        self._heartbeat_timings.append(hb)
        if len(self._heartbeat_timings) > self._max_history:
            self._heartbeat_timings.pop(0)
        return hb

    # ----- Capacity snapshot -----

    def snapshot(
        self,
        active_agents: int,
        total_agents: int,
        heartbeat_interval: float = 30.0,
    ) -> dict[str, Any]:
        """Build a capacity and contention report.

        Parameters
        ----------
        active_agents:
            Number of agents currently in EXECUTING/REPLANNING state.
        total_agents:
            Total number of registered agents.
        heartbeat_interval:
            Fabric heartbeat interval in seconds.
        """
        cpu_cores = os.cpu_count() or 1

        # RAM (try psutil, fallback gracefully)
        ram_total_gb = 0.0
        ram_available_gb = 0.0
        ram_percent = 0.0
        try:
            import psutil
            mem = psutil.virtual_memory()
            ram_total_gb = mem.total / (1024 ** 3)
            ram_available_gb = mem.available / (1024 ** 3)
            ram_percent = mem.percent
        except ImportError:
            pass

        # Disk
        try:
            usage = shutil.disk_usage(".")
            disk_free_gb = usage.free / (1024 ** 3)
        except OSError:
            disk_free_gb = 0.0

        # Task contention
        recent_tasks = self._task_timings[-20:] if self._task_timings else []
        avg_queue_wait = 0.0
        avg_execution = 0.0
        avg_consciousness_wait = 0.0
        if recent_tasks:
            avg_queue_wait = sum(t.queue_wait for t in recent_tasks) / len(recent_tasks)
            avg_execution = sum(t.execution_time for t in recent_tasks) / len(recent_tasks)
            avg_consciousness_wait = sum(
                t.consciousness_wait for t in recent_tasks
            ) / len(recent_tasks)

        # Heartbeat contention
        recent_hb = self._heartbeat_timings[-10:] if self._heartbeat_timings else []
        avg_heartbeat = 0.0
        avg_idle = 0.0
        if recent_hb:
            avg_heartbeat = sum(h.total_time for h in recent_hb) / len(recent_hb)
            avg_idle = sum(h.idle_time for h in recent_hb) / len(recent_hb)

        # Per-agent contention (who's hogging the heartbeat)
        agent_hb_totals: dict[str, float] = {}
        for hb in recent_hb:
            for aid, t in hb.agent_timings.items():
                agent_hb_totals[aid] = agent_hb_totals.get(aid, 0.0) + t
        total_agent_time = sum(agent_hb_totals.values()) or 1.0
        agent_share = {
            aid: round(t / total_agent_time * 100, 1)
            for aid, t in agent_hb_totals.items()
        }

        # Estimate max concurrent agents.
        #
        # The bottleneck is almost never CPU — agents spend most of
        # their cycle blocked on LLM API calls.  The real limit is
        # how many cycles fit in one heartbeat interval.
        #
        # With measured data: heartbeat_interval / avg_per_agent_cycle.
        # Without data: conservative estimate based on RAM (each agent
        # + terminal subprocess uses ~200-500MB).
        max_concurrent, max_basis = self._estimate_max_concurrent(
            heartbeat_interval, avg_heartbeat, active_agents,
            ram_available_gb,
        )

        return {
            "node": {
                "cpu_cores": cpu_cores,
                "ram_total_gb": round(ram_total_gb, 1),
                "ram_available_gb": round(ram_available_gb, 1),
                "ram_percent": round(ram_percent, 1),
                "disk_free_gb": round(disk_free_gb, 1),
            },
            "agents": {
                "active": active_agents,
                "total": total_agents,
                "max_concurrent": max_concurrent,
                "max_concurrent_basis": max_basis,
            },
            "contention": {
                "avg_queue_wait_s": round(avg_queue_wait, 2),
                "avg_execution_s": round(avg_execution, 2),
                "avg_consciousness_wait_s": round(avg_consciousness_wait, 2),
                "avg_heartbeat_s": round(avg_heartbeat, 2),
                "avg_heartbeat_idle_s": round(avg_idle, 2),
                "heartbeat_utilisation_pct": round(
                    (1.0 - avg_idle / avg_heartbeat) * 100, 1
                ) if avg_heartbeat > 0 else 0.0,
            },
            "agent_share_pct": agent_share,
            "recent_tasks": [t.to_dict() for t in recent_tasks[-5:]],
        }

    @staticmethod
    def _estimate_max_concurrent(
        heartbeat_interval: float,
        avg_heartbeat_time: float,
        active_agents: int,
        ram_available_gb: float,
    ) -> tuple[int, str]:
        """Estimate how many agents this node can run concurrently.

        Returns ``(count, basis)`` where *basis* explains the estimate.
        """
        # Method 1: Measured — if we have heartbeat data with active
        # agents, extrapolate how many would fill the interval.
        if avg_heartbeat_time > 0 and active_agents > 0:
            per_agent = avg_heartbeat_time / active_agents
            if per_agent > 0:
                measured = int(heartbeat_interval / per_agent)
                measured = max(measured, 1)
                return (measured, f"measured: {per_agent:.1f}s/agent, {heartbeat_interval:.0f}s interval")

        # Method 2: RAM-based — each agent with a terminal subprocess
        # uses roughly 300MB.  This is conservative.
        if ram_available_gb > 0:
            ram_estimate = int(ram_available_gb / 0.3)
            ram_estimate = max(ram_estimate, 1)
            return (ram_estimate, "estimated from available RAM (~300MB/agent)")

        # Fallback
        return (10, "default estimate (no measured data)")
