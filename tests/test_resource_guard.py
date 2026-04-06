"""Tests for agent resource guards."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cortiva.core.resource_guard import ResourceGuard, ResourceLimits


class TestResourceLimits:
    def test_defaults(self) -> None:
        lim = ResourceLimits()
        assert lim.cycle_timeout_s == 120.0
        assert lim.max_consciousness_calls_per_cycle == 5
        assert lim.max_disk_mb == 500.0
        assert lim.max_hours_per_day == 12.0

    def test_from_dict(self) -> None:
        lim = ResourceLimits.from_dict({
            "cycle_timeout_s": 60,
            "max_disk_mb": 100,
        })
        assert lim.cycle_timeout_s == 60
        assert lim.max_disk_mb == 100
        # Defaults for unspecified
        assert lim.max_consciousness_calls_per_cycle == 5


class TestResourceGuard:
    def test_pre_cycle_allows(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        (tmp_path / "agent-1").mkdir()
        result = guard.pre_cycle_check("agent-1")
        assert result is None  # allowed

    def test_pre_cycle_blocks_max_hours(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        guard.load({"defaults": {"max_hours_per_day": 8.0}})
        result = guard.pre_cycle_check("agent-1", hours_today=9.0)
        assert result is not None
        assert "hours" in result.lower()

    def test_pre_cycle_blocks_disk_quota(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        guard.load({"defaults": {"max_disk_mb": 0.001}})  # tiny limit
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        # Write some data
        (agent_dir / "big.txt").write_text("x" * 10000)
        result = guard.pre_cycle_check("agent-1")
        assert result is not None
        assert "disk" in result.lower()

    def test_pre_cycle_blocks_cycles_per_heartbeat(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        guard.load({"defaults": {"max_cycles_per_heartbeat": 1}})
        state = guard._state_for("agent-1")
        state.cycles_this_heartbeat = 1
        result = guard.pre_cycle_check("agent-1")
        assert result is not None
        assert "cycles" in result.lower()

    def test_pre_cycle_blocks_suspended(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        guard.suspend("agent-1")
        result = guard.pre_cycle_check("agent-1")
        assert result is not None
        assert "suspended" in result.lower()

    @pytest.mark.asyncio
    async def test_wrap_cycle_success(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)

        async def fake_cycle() -> dict:
            return {"action": "executed"}

        result = await guard.wrap_cycle("agent-1", fake_cycle())
        assert result == {"action": "executed"}

    @pytest.mark.asyncio
    async def test_wrap_cycle_timeout(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        guard.load({"defaults": {"cycle_timeout_s": 0.1}})

        async def slow_cycle() -> dict:
            await asyncio.sleep(5)
            return {"action": "executed"}

        result = await guard.wrap_cycle("agent-1", slow_cycle())
        assert result is None  # timed out

    def test_consciousness_call_gate(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        guard.load({"defaults": {"max_consciousness_calls_per_cycle": 2}})

        assert guard.allow_consciousness_call("agent-1") is True
        assert guard.allow_consciousness_call("agent-1") is True
        assert guard.allow_consciousness_call("agent-1") is False  # limit hit

    def test_suspend_unsuspend(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        assert guard.is_suspended("agent-1") is False
        guard.suspend("agent-1")
        assert guard.is_suspended("agent-1") is True
        guard.unsuspend("agent-1")
        assert guard.is_suspended("agent-1") is False

    def test_reset_heartbeat(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        state = guard._state_for("agent-1")
        state.cycles_this_heartbeat = 5
        guard.reset_heartbeat()
        assert state.cycles_this_heartbeat == 0

    def test_per_agent_overrides(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        guard.load({
            "defaults": {"cycle_timeout_s": 120},
            "dev-cortiva": {"cycle_timeout_s": 300},
        })
        assert guard.limits_for("dev-cortiva").cycle_timeout_s == 300
        assert guard.limits_for("qa-cortiva").cycle_timeout_s == 120

    def test_status(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        (tmp_path / "agent-1").mkdir()
        guard._state_for("agent-1")  # init state
        s = guard.status("agent-1")
        assert s["agent_id"] == "agent-1"
        assert "limits" in s
        assert "usage" in s
        assert s["suspended"] is False

    def test_post_cycle_check(self, tmp_path: Path) -> None:
        guard = ResourceGuard(tmp_path)
        state = guard._state_for("agent-1")
        state.violations.append("disk:600MB>500MB")
        violations = guard.post_cycle_check("agent-1")
        assert len(violations) == 1
        # Violations are cleared after check
        assert len(guard.post_cycle_check("agent-1")) == 0
