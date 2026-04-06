"""Tests for the agent scheduler and its integration with Fabric."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cortiva.core.scheduler import (
    AgentSchedule,
    ScheduleEntry,
    Scheduler,
    _parse_days,
    _parse_times,
    parse_schedule,
)


# ---------------------------------------------------------------------------
# _parse_days
# ---------------------------------------------------------------------------


class TestParseDays:
    def test_weekdays_keyword(self) -> None:
        assert _parse_days("weekdays") == {0, 1, 2, 3, 4}

    def test_weekends_keyword(self) -> None:
        assert _parse_days("weekends") == {5, 6}

    def test_daily_keyword(self) -> None:
        assert _parse_days("daily") == {0, 1, 2, 3, 4, 5, 6}

    def test_range_mon_fri(self) -> None:
        assert _parse_days("mon-fri") == {0, 1, 2, 3, 4}

    def test_range_wrap_around(self) -> None:
        # fri-mon wraps: {4, 5, 6, 0}
        assert _parse_days("fri-mon") == {4, 5, 6, 0}

    def test_comma_list(self) -> None:
        assert _parse_days("mon,wed,fri") == {0, 2, 4}

    def test_single_day(self) -> None:
        assert _parse_days("tue") == {1}

    def test_unknown_falls_back_to_daily(self) -> None:
        assert _parse_days("xyzzy") == {0, 1, 2, 3, 4, 5, 6}

    def test_case_insensitive(self) -> None:
        assert _parse_days("MON-FRI") == {0, 1, 2, 3, 4}


# ---------------------------------------------------------------------------
# _parse_times
# ---------------------------------------------------------------------------


class TestParseTimes:
    def test_single_time(self) -> None:
        assert _parse_times("09:00") == [(9, 0)]

    def test_multiple_times(self) -> None:
        assert _parse_times("12:00,15:30") == [(12, 0), (15, 30)]

    def test_single_digit_hour(self) -> None:
        assert _parse_times("9:00") == [(9, 0)]

    def test_empty_string(self) -> None:
        assert _parse_times("") == []

    def test_invalid_format(self) -> None:
        assert _parse_times("noon") == []


# ---------------------------------------------------------------------------
# ScheduleEntry
# ---------------------------------------------------------------------------


class TestScheduleEntry:
    def test_is_due_exact_time(self) -> None:
        entry = ScheduleEntry(action="wake", times=[(9, 0)], days={0, 1, 2, 3, 4})
        # Monday 09:00
        now = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)  # Monday
        assert entry.is_due(now)

    def test_is_due_within_tolerance(self) -> None:
        entry = ScheduleEntry(action="wake", times=[(9, 0)], days={0, 1, 2, 3, 4})
        now = datetime(2026, 3, 2, 9, 3, tzinfo=timezone.utc)  # 3 min after
        assert entry.is_due(now, tolerance_minutes=5)

    def test_not_due_outside_tolerance(self) -> None:
        entry = ScheduleEntry(action="wake", times=[(9, 0)], days={0, 1, 2, 3, 4})
        now = datetime(2026, 3, 2, 9, 6, tzinfo=timezone.utc)  # 6 min after
        assert not entry.is_due(now, tolerance_minutes=5)

    def test_not_due_wrong_day(self) -> None:
        entry = ScheduleEntry(action="wake", times=[(9, 0)], days={0, 1, 2, 3, 4})
        # Saturday
        now = datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc)
        assert not entry.is_due(now)

    def test_not_due_before_time(self) -> None:
        entry = ScheduleEntry(action="wake", times=[(9, 0)], days={0, 1, 2, 3, 4})
        now = datetime(2026, 3, 2, 8, 59, tzinfo=timezone.utc)
        assert not entry.is_due(now)


# ---------------------------------------------------------------------------
# AgentSchedule
# ---------------------------------------------------------------------------


class TestAgentSchedule:
    def test_due_actions_returns_matching(self) -> None:
        schedule = AgentSchedule(
            agent_id="test-01",
            entries=[
                ScheduleEntry(action="wake", times=[(9, 0)], days={0, 1, 2, 3, 4}),
                ScheduleEntry(action="sleep", times=[(17, 0)], days={0, 1, 2, 3, 4}),
            ],
        )
        now = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        actions = schedule.due_actions(now)
        assert "wake" in actions
        assert "sleep" not in actions

    def test_deduplication(self) -> None:
        schedule = AgentSchedule(
            agent_id="test-01",
            entries=[
                ScheduleEntry(action="wake", times=[(9, 0)], days={0, 1, 2, 3, 4}),
            ],
        )
        now = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        actions1 = schedule.due_actions(now)
        actions2 = schedule.due_actions(now)
        assert actions1 == ["wake"]
        assert actions2 == []  # Already triggered

    def test_retrigger_next_day(self) -> None:
        schedule = AgentSchedule(
            agent_id="test-01",
            entries=[
                ScheduleEntry(action="wake", times=[(9, 0)], days={0, 1, 2, 3, 4}),
            ],
        )
        mon = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        tue = datetime(2026, 3, 3, 9, 0, tzinfo=timezone.utc)
        schedule.due_actions(mon)
        actions = schedule.due_actions(tue)
        assert "wake" in actions

    def test_multiple_times(self) -> None:
        schedule = AgentSchedule(
            agent_id="test-01",
            entries=[
                ScheduleEntry(action="replan", times=[(12, 0), (15, 0)], days={0, 1, 2, 3, 4}),
            ],
        )
        noon = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
        afternoon = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)
        a1 = schedule.due_actions(noon)
        a2 = schedule.due_actions(afternoon)
        assert "replan" in a1
        assert "replan" in a2


# ---------------------------------------------------------------------------
# parse_schedule
# ---------------------------------------------------------------------------


class TestParseSchedule:
    def test_basic_schedule(self) -> None:
        config = {"wake": "09:00 mon-fri", "sleep": "17:00"}
        schedule = parse_schedule("agent-01", config)
        assert schedule.agent_id == "agent-01"
        assert len(schedule.entries) == 2

    def test_all_actions(self) -> None:
        config = {
            "wake": "09:00",
            "replan": "12:00,15:00",
            "sleep": "17:00",
        }
        schedule = parse_schedule("agent-01", config)
        assert len(schedule.entries) == 3
        actions = {e.action for e in schedule.entries}
        assert actions == {"wake", "replan", "sleep"}

    def test_replan_multiple_times(self) -> None:
        config = {"replan": "12:00,15:00 weekdays"}
        schedule = parse_schedule("agent-01", config)
        assert len(schedule.entries) == 1
        assert schedule.entries[0].times == [(12, 0), (15, 0)]
        assert schedule.entries[0].days == {0, 1, 2, 3, 4}

    def test_empty_config(self) -> None:
        schedule = parse_schedule("agent-01", {})
        assert schedule.entries == []

    def test_ignores_unknown_actions(self) -> None:
        config = {"wake": "09:00", "party": "23:00"}
        schedule = parse_schedule("agent-01", config)
        assert len(schedule.entries) == 1
        assert schedule.entries[0].action == "wake"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class TestScheduler:
    def test_register_and_tick(self) -> None:
        scheduler = Scheduler()
        scheduler.register("agent-01", {"wake": "09:00 mon-fri"})
        now = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)  # Monday
        result = scheduler.tick(now)
        assert "agent-01" in result
        assert "wake" in result["agent-01"]

    def test_tick_no_due(self) -> None:
        scheduler = Scheduler()
        scheduler.register("agent-01", {"wake": "09:00 mon-fri"})
        now = datetime(2026, 3, 2, 20, 0, tzinfo=timezone.utc)
        result = scheduler.tick(now)
        assert result == {}

    def test_unregister(self) -> None:
        scheduler = Scheduler()
        scheduler.register("agent-01", {"wake": "09:00"})
        scheduler.unregister("agent-01")
        assert scheduler.agent_ids == []

    def test_multiple_agents(self) -> None:
        scheduler = Scheduler()
        scheduler.register("a", {"wake": "09:00"})
        scheduler.register("b", {"wake": "10:00"})
        now = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        result = scheduler.tick(now)
        assert "a" in result
        assert "b" not in result

    def test_get_schedule(self) -> None:
        scheduler = Scheduler()
        scheduler.register("a", {"wake": "09:00"})
        sched = scheduler.get_schedule("a")
        assert sched is not None
        assert sched.agent_id == "a"

    def test_get_schedule_unknown(self) -> None:
        scheduler = Scheduler()
        assert scheduler.get_schedule("nonexistent") is None

    def test_agent_ids(self) -> None:
        scheduler = Scheduler()
        scheduler.register("a", {"wake": "09:00"})
        scheduler.register("b", {"sleep": "17:00"})
        assert set(scheduler.agent_ids) == {"a", "b"}

    def test_tick_defaults_to_utc_now(self) -> None:
        scheduler = Scheduler()
        scheduler.register("a", {"wake": "99:00"})  # Never due
        result = scheduler.tick()
        assert result == {}


# ---------------------------------------------------------------------------
# Fabric.load_schedules
# ---------------------------------------------------------------------------


class TestFabricScheduleIntegration:
    def _make_fabric(self, tmp_path):
        from cortiva.adapters.memory.inmemory import InMemoryAdapter
        from cortiva.core.fabric import Fabric

        class StubConsciousness:
            async def think(self, **kw):
                from cortiva.adapters.protocols import ConsciousResponse
                return ConsciousResponse(content="- [ ] Do stuff", model="stub")
            async def reflect(self, **kw):
                from cortiva.adapters.protocols import ConsciousResponse
                return ConsciousResponse(content="Reflected.", model="stub")

        return Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=StubConsciousness(),
        )

    def test_load_schedules(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        fabric.load_schedules({
            "agent-01": {"wake": "09:00 mon-fri", "sleep": "17:00"},
        })
        sched = fabric.scheduler.get_schedule("agent-01")
        assert sched is not None
        assert len(sched.entries) == 2

    @pytest.mark.asyncio
    async def test_heartbeat_triggers_wake(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        agent = fabric.register_agent("agent-01")
        fabric.load_schedules({"agent-01": {"wake": "09:00 mon-fri"}})

        now = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        fabric.scheduler.tick(now)  # Consume the first trigger

        # Re-register to reset dedup
        fabric.scheduler.register("agent-01", {"wake": "09:00 mon-fri"})

        with patch.object(fabric, "wake", new_callable=AsyncMock) as mock_wake:
            mock_wake.return_value = agent
            # Patch scheduler.tick to return our controlled result
            with patch.object(fabric.scheduler, "tick", return_value={"agent-01": ["wake"]}):
                await fabric.heartbeat()
            mock_wake.assert_called_once_with("agent-01")

    @pytest.mark.asyncio
    async def test_heartbeat_triggers_sleep(self, tmp_path) -> None:
        from cortiva.core.agent import AgentState

        fabric = self._make_fabric(tmp_path)
        agent = fabric.register_agent("agent-01")
        # Force agent to EXECUTING state
        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        with patch.object(fabric, "sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.return_value = agent
            with patch.object(fabric.scheduler, "tick", return_value={"agent-01": ["sleep"]}):
                await fabric.heartbeat()
            mock_sleep.assert_called_once_with("agent-01")

    @pytest.mark.asyncio
    async def test_heartbeat_triggers_replan(self, tmp_path) -> None:
        from cortiva.core.agent import AgentState, TaskQueue

        fabric = self._make_fabric(tmp_path)
        agent = fabric.register_agent("agent-01")
        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)
        agent.task_queue = TaskQueue()

        with patch.object(fabric, "_replan", new_callable=AsyncMock) as mock_replan:
            with patch.object(fabric.scheduler, "tick", return_value={"agent-01": ["replan"]}):
                await fabric.heartbeat()
            mock_replan.assert_called_once_with(agent, [])

    @pytest.mark.asyncio
    async def test_heartbeat_ignores_unknown_agent(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        # Schedule for agent not registered
        with patch.object(fabric.scheduler, "tick", return_value={"ghost": ["wake"]}):
            await fabric.heartbeat()  # Should not raise

    @pytest.mark.asyncio
    async def test_heartbeat_skips_wake_when_not_sleeping(self, tmp_path) -> None:
        from cortiva.core.agent import AgentState

        fabric = self._make_fabric(tmp_path)
        agent = fabric.register_agent("agent-01")
        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        with patch.object(fabric, "wake", new_callable=AsyncMock) as mock_wake:
            with patch.object(fabric.scheduler, "tick", return_value={"agent-01": ["wake"]}):
                await fabric.heartbeat()
            mock_wake.assert_not_called()


# ---------------------------------------------------------------------------
# Config integration — schedules in cortiva.yaml
# ---------------------------------------------------------------------------


class TestConfigScheduleIntegration:
    def test_build_fabric_with_schedules(self, tmp_path) -> None:
        from unittest.mock import patch as _patch

        from cortiva.core.config import build_fabric

        config = {
            "fabric": {"name": "test"},
            "memory": {"adapter": "inmemory"},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
            "schedules": {
                "bookkeep-01": {"wake": "09:00 mon-fri", "sleep": "17:00"},
                "dev-01": {"wake": "10:00", "replan": "14:00"},
            },
        }

        def _mock_import(registry, name, kind):
            if kind == "memory":
                from cortiva.adapters.memory.inmemory import InMemoryAdapter
                return InMemoryAdapter
            class MockCls:
                def __init__(self, **kw): pass
                async def think(self, **kw): pass
                async def reflect(self, **kw): pass
            return MockCls

        with _patch("cortiva.core.config._import_adapter", side_effect=_mock_import):
            fabric = build_fabric(config)

        assert fabric.scheduler.get_schedule("bookkeep-01") is not None
        assert fabric.scheduler.get_schedule("dev-01") is not None
        assert len(fabric.scheduler.agent_ids) == 2

    def test_build_fabric_without_schedules(self, tmp_path) -> None:
        from unittest.mock import patch as _patch

        from cortiva.core.config import build_fabric

        config = {
            "fabric": {"name": "test"},
            "memory": {"adapter": "inmemory"},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
        }

        def _mock_import(registry, name, kind):
            if kind == "memory":
                from cortiva.adapters.memory.inmemory import InMemoryAdapter
                return InMemoryAdapter
            class MockCls:
                def __init__(self, **kw): pass
            return MockCls

        with _patch("cortiva.core.config._import_adapter", side_effect=_mock_import):
            fabric = build_fabric(config)

        assert fabric.scheduler.agent_ids == []


# ---------------------------------------------------------------------------
# Agent self-scheduling tests
# ---------------------------------------------------------------------------


class TestAgentSelfScheduling:
    def test_add_alarm(self) -> None:
        scheduler = Scheduler()
        alarm = scheduler.add_alarm(
            "dev-cortiva", "wake",
            datetime(2026, 4, 7, 6, 0, tzinfo=timezone.utc),
            "deploy day",
        )
        assert alarm.agent_id == "dev-cortiva"
        assert alarm.action == "wake"
        assert not alarm.fired

    def test_alarm_fires_on_tick(self) -> None:
        scheduler = Scheduler()
        scheduler.add_alarm(
            "dev-cortiva", "wake",
            datetime(2026, 4, 7, 6, 0, tzinfo=timezone.utc),
        )
        # Before alarm time — nothing fires
        result = scheduler.tick(datetime(2026, 4, 7, 5, 59, tzinfo=timezone.utc))
        assert "dev-cortiva" not in result

        # At alarm time — fires
        result = scheduler.tick(datetime(2026, 4, 7, 6, 0, tzinfo=timezone.utc))
        assert "dev-cortiva" in result
        assert "wake" in result["dev-cortiva"]

        # After firing — doesn't fire again
        result = scheduler.tick(datetime(2026, 4, 7, 6, 1, tzinfo=timezone.utc))
        assert "dev-cortiva" not in result

    def test_request_overtime(self) -> None:
        scheduler = Scheduler()
        scheduler.register("dev-cortiva", {"wake": "09:00", "sleep": "17:00"})
        alarm = scheduler.request_overtime("dev-cortiva", 2.0)
        assert alarm.action == "sleep"
        assert "overtime" in alarm.reason

    def test_request_early_sleep(self) -> None:
        scheduler = Scheduler()
        alarm = scheduler.request_early_sleep("dev-cortiva")
        assert alarm.action == "sleep"
        assert "early" in alarm.reason

    def test_set_wake_alarm(self) -> None:
        scheduler = Scheduler()
        alarm = scheduler.set_wake_alarm("dev-cortiva", 6, 0, "deploy")
        assert alarm.action == "wake"
        assert alarm.time.hour == 6
        assert alarm.reason == "deploy"

    def test_set_reminder(self) -> None:
        scheduler = Scheduler()
        alarm = scheduler.set_reminder("dev-cortiva", 14, 0, "check CI")
        assert alarm.action == "remind"
        assert "check CI" in alarm.reason

    def test_pending_alarms(self) -> None:
        scheduler = Scheduler()
        scheduler.add_alarm(
            "dev", "wake", datetime(2026, 4, 7, 6, 0, tzinfo=timezone.utc),
        )
        scheduler.add_alarm(
            "dev", "remind", datetime(2026, 4, 7, 14, 0, tzinfo=timezone.utc),
        )
        assert len(scheduler.pending_alarms("dev")) == 2

        # Fire one
        scheduler.tick(datetime(2026, 4, 7, 6, 0, tzinfo=timezone.utc))
        assert len(scheduler.pending_alarms("dev")) == 1

    def test_apply_schedule_request_overtime(self) -> None:
        scheduler = Scheduler()
        scheduler.register("dev", {"sleep": "17:00"})
        result = scheduler.apply_schedule_request("dev", {"overtime": 2.0})
        assert result is not None
        assert "Overtime" in result

    def test_apply_schedule_request_early_sleep(self) -> None:
        scheduler = Scheduler()
        result = scheduler.apply_schedule_request("dev", {"early_sleep": True})
        assert result is not None
        assert "Early sleep" in result

    def test_apply_schedule_request_wake_alarm(self) -> None:
        scheduler = Scheduler()
        result = scheduler.apply_schedule_request("dev", {
            "wake_alarm": "06:00",
            "reason": "deploy",
        })
        assert result is not None
        assert "06:00" in result

    def test_apply_schedule_request_reminder(self) -> None:
        scheduler = Scheduler()
        result = scheduler.apply_schedule_request("dev", {
            "reminder": "14:00",
            "content": "check CI pipeline",
        })
        assert result is not None
        assert "14:00" in result

    def test_apply_schedule_request_empty(self) -> None:
        scheduler = Scheduler()
        assert scheduler.apply_schedule_request("dev", {}) is None
        assert scheduler.apply_schedule_request("dev", None) is None

    def test_combined_schedule_and_alarm(self) -> None:
        """Recurring schedule and one-shot alarm both fire."""
        scheduler = Scheduler()
        scheduler.register("dev", {"replan": "12:00"})
        scheduler.add_alarm(
            "dev", "remind",
            datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc),
            "check deploy",
        )

        now = datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)
        result = scheduler.tick(now)
        assert "dev" in result
        assert "replan" in result["dev"]
        assert "remind" in result["dev"]
