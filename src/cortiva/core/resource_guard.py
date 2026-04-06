"""
Resource guard — prevents agents from starving each other or the host.

Enforces per-agent limits on execution time, consciousness calls,
disk usage, and memory.  Works at **all isolation tiers**, not just
container mode.

Limits are configured per-agent or via defaults in ``cortiva.yaml``:

.. code-block:: yaml

    resource_limits:
      defaults:
        cycle_timeout_s: 120
        max_consciousness_calls_per_cycle: 5
        max_disk_mb: 500
        max_cycles_per_heartbeat: 1
      dev-cortiva:
        cycle_timeout_s: 300
        max_disk_mb: 2000

The guard is checked at three points:

1. **Before cycle** — ``pre_cycle_check()`` rejects agents that have
   exhausted their disk quota or are over budget.
2. **During cycle** — ``wrap_cycle()`` enforces a timeout on each
   cycle so one slow LLM call can't block the heartbeat forever.
3. **After cycle** — ``post_cycle_check()`` records usage and flags
   agents approaching limits.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortiva.resource_guard")


@dataclass
class ResourceLimits:
    """Per-agent resource limits."""

    cycle_timeout_s: float = 120.0
    """Maximum wall-clock seconds for a single cycle.  The cycle is
    cancelled if it exceeds this.  Prevents one slow LLM call from
    blocking the entire heartbeat."""

    max_consciousness_calls_per_cycle: int = 5
    """Maximum consciousness (LLM) calls allowed in a single cycle.
    Prevents runaway loops."""

    max_disk_mb: float = 500.0
    """Maximum disk space (MB) the agent's directory may consume.
    Checked before each cycle."""

    max_cycles_per_heartbeat: int = 1
    """Maximum cycles an agent may run per heartbeat tick.  Normally 1.
    Set higher only for agents with very short cycles."""

    max_hours_per_day: float = 12.0
    """Maximum working hours per day before the agent is forced to sleep.
    Prevents indefinite operation if the sleep schedule is missed."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourceLimits:
        fields = cls.__dataclass_fields__
        kwargs = {k: data[k] for k in data if k in fields}
        return cls(**kwargs)


@dataclass
class AgentResourceState:
    """Tracks resource usage for one agent within the current heartbeat."""

    cycles_this_heartbeat: int = 0
    consciousness_calls_this_cycle: int = 0
    violations: list[str] = field(default_factory=list)
    suspended: bool = False
    """True if the agent has been suspended due to resource violations."""


class ResourceGuard:
    """Enforces per-agent resource limits across all isolation tiers.

    Instantiated once per Fabric.  The Fabric calls the guard's
    methods at each stage of the heartbeat/cycle flow.
    """

    def __init__(self, agents_dir: Path) -> None:
        self._agents_dir = agents_dir
        self._defaults = ResourceLimits()
        self._overrides: dict[str, ResourceLimits] = {}
        self._state: dict[str, AgentResourceState] = {}

    def load(self, config: dict[str, Any]) -> None:
        """Load limits from the ``resource_limits`` config section."""
        defaults_data = config.get("defaults", {})
        if defaults_data:
            self._defaults = ResourceLimits.from_dict(defaults_data)

        for agent_id, agent_data in config.items():
            if agent_id == "defaults":
                continue
            if isinstance(agent_data, dict):
                # Merge with defaults
                merged = {**self._defaults.__dict__, **agent_data}
                self._overrides[agent_id] = ResourceLimits.from_dict(merged)

    def limits_for(self, agent_id: str) -> ResourceLimits:
        """Get the effective limits for an agent."""
        return self._overrides.get(agent_id, self._defaults)

    def _state_for(self, agent_id: str) -> AgentResourceState:
        if agent_id not in self._state:
            self._state[agent_id] = AgentResourceState()
        return self._state[agent_id]

    # ------------------------------------------------------------------
    # Pre-cycle checks
    # ------------------------------------------------------------------

    def pre_cycle_check(self, agent_id: str, hours_today: float = 0.0) -> str | None:
        """Check whether an agent should be allowed to run a cycle.

        Returns ``None`` if allowed, or a reason string if blocked.
        """
        limits = self.limits_for(agent_id)
        state = self._state_for(agent_id)

        if state.suspended:
            return f"Agent {agent_id} is suspended due to resource violations"

        # Max cycles per heartbeat
        if state.cycles_this_heartbeat >= limits.max_cycles_per_heartbeat:
            return (
                f"Agent {agent_id} exceeded max cycles per heartbeat "
                f"({limits.max_cycles_per_heartbeat})"
            )

        # Max hours per day
        if hours_today >= limits.max_hours_per_day:
            return (
                f"Agent {agent_id} exceeded max hours per day "
                f"({hours_today:.1f}h >= {limits.max_hours_per_day}h)"
            )

        # Disk quota
        disk_mb = self._agent_disk_mb(agent_id)
        if disk_mb > limits.max_disk_mb:
            state.violations.append(f"disk:{disk_mb:.0f}MB>{limits.max_disk_mb:.0f}MB")
            return (
                f"Agent {agent_id} exceeded disk quota "
                f"({disk_mb:.0f}MB > {limits.max_disk_mb:.0f}MB)"
            )

        return None

    # ------------------------------------------------------------------
    # Cycle wrapper
    # ------------------------------------------------------------------

    async def wrap_cycle(
        self,
        agent_id: str,
        cycle_coro: Any,
    ) -> dict[str, Any] | None:
        """Run a cycle coroutine with a timeout.

        Returns the cycle result, or ``None`` if the cycle was
        cancelled due to timeout.
        """
        limits = self.limits_for(agent_id)
        state = self._state_for(agent_id)
        state.consciousness_calls_this_cycle = 0

        try:
            result = await asyncio.wait_for(
                cycle_coro,
                timeout=limits.cycle_timeout_s,
            )
            state.cycles_this_heartbeat += 1
            return result
        except TimeoutError:
            state.violations.append(f"timeout:{limits.cycle_timeout_s}s")
            logger.warning(
                "Agent %s cycle timed out after %.0fs",
                agent_id, limits.cycle_timeout_s,
            )
            return None

    # ------------------------------------------------------------------
    # Consciousness call gate
    # ------------------------------------------------------------------

    def allow_consciousness_call(self, agent_id: str) -> bool:
        """Check whether the agent may make another consciousness call
        within the current cycle.

        Called by the Fabric before each ``consciousness.think()`` invocation.
        """
        limits = self.limits_for(agent_id)
        state = self._state_for(agent_id)

        if state.consciousness_calls_this_cycle >= limits.max_consciousness_calls_per_cycle:
            logger.warning(
                "Agent %s hit consciousness call limit (%d/%d) for this cycle",
                agent_id,
                state.consciousness_calls_this_cycle,
                limits.max_consciousness_calls_per_cycle,
            )
            return False

        state.consciousness_calls_this_cycle += 1
        return True

    # ------------------------------------------------------------------
    # Post-cycle and heartbeat reset
    # ------------------------------------------------------------------

    def post_cycle_check(self, agent_id: str) -> list[str]:
        """Return any violations accumulated during the cycle."""
        state = self._state_for(agent_id)
        violations = list(state.violations)
        state.violations.clear()
        return violations

    def reset_heartbeat(self) -> None:
        """Reset per-heartbeat counters for all agents."""
        for state in self._state.values():
            state.cycles_this_heartbeat = 0

    def suspend(self, agent_id: str) -> None:
        """Suspend an agent from further execution."""
        state = self._state_for(agent_id)
        state.suspended = True
        logger.warning("Agent %s suspended due to resource violations", agent_id)

    def unsuspend(self, agent_id: str) -> None:
        """Lift suspension for an agent."""
        state = self._state_for(agent_id)
        state.suspended = False
        state.violations.clear()

    def is_suspended(self, agent_id: str) -> bool:
        return self._state_for(agent_id).suspended

    # ------------------------------------------------------------------
    # Disk measurement
    # ------------------------------------------------------------------

    def _agent_disk_mb(self, agent_id: str) -> float:
        """Measure an agent's disk usage in MB."""
        agent_dir = self._agents_dir / agent_id
        if not agent_dir.exists():
            return 0.0
        try:
            total = sum(
                f.stat().st_size
                for f in agent_dir.rglob("*")
                if f.is_file()
            )
            return total / (1024 * 1024)
        except OSError:
            return 0.0

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self, agent_id: str) -> dict[str, Any]:
        """Get resource status for an agent."""
        limits = self.limits_for(agent_id)
        state = self._state_for(agent_id)
        disk_mb = self._agent_disk_mb(agent_id)
        return {
            "agent_id": agent_id,
            "limits": {
                "cycle_timeout_s": limits.cycle_timeout_s,
                "max_consciousness_calls_per_cycle": limits.max_consciousness_calls_per_cycle,
                "max_disk_mb": limits.max_disk_mb,
                "max_cycles_per_heartbeat": limits.max_cycles_per_heartbeat,
                "max_hours_per_day": limits.max_hours_per_day,
            },
            "usage": {
                "disk_mb": round(disk_mb, 1),
                "disk_pct": round(disk_mb / limits.max_disk_mb * 100, 1) if limits.max_disk_mb > 0 else 0,
                "cycles_this_heartbeat": state.cycles_this_heartbeat,
                "consciousness_calls_this_cycle": state.consciousness_calls_this_cycle,
            },
            "suspended": state.suspended,
        }

    def all_status(self) -> dict[str, dict[str, Any]]:
        """Get resource status for all tracked agents."""
        return {aid: self.status(aid) for aid in self._state}
