"""Tests for agent timesheet and working hours tracking."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortiva.core.timesheet import DaySummary, Timesheet, TimesheetManager, WorkEntry


class TestWorkEntry:
    def test_duration_with_sleep(self) -> None:
        wake = datetime(2026, 3, 30, 9, 0, tzinfo=UTC)
        sleep = datetime(2026, 3, 30, 17, 0, tzinfo=UTC)
        entry = WorkEntry(wake_time=wake, sleep_time=sleep)
        assert entry.hours == 8.0

    def test_duration_without_sleep(self) -> None:
        entry = WorkEntry(wake_time=datetime.now(UTC))
        # Should return a small positive number (still working)
        assert entry.hours >= 0

    def test_to_dict(self) -> None:
        wake = datetime(2026, 3, 30, 9, 0, tzinfo=UTC)
        sleep = datetime(2026, 3, 30, 17, 0, tzinfo=UTC)
        entry = WorkEntry(
            wake_time=wake, sleep_time=sleep,
            tasks_completed=5, tasks_escalated=1, consciousness_calls=12,
        )
        d = entry.to_dict()
        assert d["hours"] == 8.0
        assert d["tasks_completed"] == 5
        assert d["consciousness_calls"] == 12

    def test_from_dict_roundtrip(self) -> None:
        wake = datetime(2026, 3, 30, 9, 0, tzinfo=UTC)
        sleep = datetime(2026, 3, 30, 17, 0, tzinfo=UTC)
        original = WorkEntry(wake_time=wake, sleep_time=sleep, tasks_completed=3)
        restored = WorkEntry.from_dict(original.to_dict())
        assert restored.wake_time == wake
        assert restored.sleep_time == sleep
        assert restored.tasks_completed == 3


class TestDaySummary:
    def test_total_hours(self) -> None:
        wake1 = datetime(2026, 3, 30, 9, 0, tzinfo=UTC)
        sleep1 = datetime(2026, 3, 30, 12, 0, tzinfo=UTC)
        wake2 = datetime(2026, 3, 30, 13, 0, tzinfo=UTC)
        sleep2 = datetime(2026, 3, 30, 17, 0, tzinfo=UTC)
        summary = DaySummary(
            date="2026-03-30",
            entries=[
                WorkEntry(wake_time=wake1, sleep_time=sleep1),
                WorkEntry(wake_time=wake2, sleep_time=sleep2),
            ],
            scheduled_hours=8.0,
        )
        assert summary.total_hours == 7.0

    def test_overtime(self) -> None:
        wake = datetime(2026, 3, 30, 9, 0, tzinfo=UTC)
        sleep = datetime(2026, 3, 30, 20, 0, tzinfo=UTC)
        summary = DaySummary(
            date="2026-03-30",
            entries=[WorkEntry(wake_time=wake, sleep_time=sleep)],
            scheduled_hours=8.0,
        )
        assert summary.overtime_hours == 3.0

    def test_no_overtime(self) -> None:
        wake = datetime(2026, 3, 30, 9, 0, tzinfo=UTC)
        sleep = datetime(2026, 3, 30, 15, 0, tzinfo=UTC)
        summary = DaySummary(
            date="2026-03-30",
            entries=[WorkEntry(wake_time=wake, sleep_time=sleep)],
            scheduled_hours=8.0,
        )
        assert summary.overtime_hours == 0.0

    def test_aggregate_tasks(self) -> None:
        wake = datetime(2026, 3, 30, 9, 0, tzinfo=UTC)
        sleep = datetime(2026, 3, 30, 17, 0, tzinfo=UTC)
        summary = DaySummary(
            date="2026-03-30",
            entries=[
                WorkEntry(wake_time=wake, sleep_time=sleep,
                          tasks_completed=3, tasks_escalated=1),
                WorkEntry(wake_time=wake, sleep_time=sleep,
                          tasks_completed=2, tasks_escalated=0),
            ],
        )
        assert summary.total_tasks_completed == 5
        assert summary.total_tasks_escalated == 1


class TestTimesheet:
    def test_clock_in_out(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        (agent_dir / "today").mkdir()
        (agent_dir / "journal").mkdir()

        ts = Timesheet(agent_dir)
        assert not ts.is_clocked_in

        ts.clock_in()
        assert ts.is_clocked_in

        entry = ts.clock_out(tasks_completed=5, consciousness_calls=10)
        assert entry is not None
        assert entry.tasks_completed == 5
        assert not ts.is_clocked_in

    def test_persistence(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        (agent_dir / "today").mkdir()
        (agent_dir / "journal").mkdir()

        ts1 = Timesheet(agent_dir)
        ts1.clock_in()
        ts1.clock_out(tasks_completed=3)

        # Create a new timesheet instance — should load from disk
        ts2 = Timesheet(agent_dir)
        today = ts2.today()
        assert len(today.entries) == 1
        assert today.entries[0].tasks_completed == 3

    def test_today_summary(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        (agent_dir / "today").mkdir()
        (agent_dir / "journal").mkdir()

        ts = Timesheet(agent_dir, scheduled_hours=8.0)
        ts.clock_in()
        ts.clock_out(tasks_completed=2)
        ts.clock_in()
        ts.clock_out(tasks_completed=3)

        today = ts.today()
        assert len(today.entries) == 2
        assert today.total_tasks_completed == 5

    def test_reset_today(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        (agent_dir / "today").mkdir()
        (agent_dir / "journal").mkdir()

        ts = Timesheet(agent_dir)
        ts.clock_in()
        ts.clock_out()
        assert len(ts.today().entries) == 1

        ts.reset_today()
        assert len(ts.today().entries) == 0

    def test_clock_out_without_clock_in(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        (agent_dir / "today").mkdir()

        ts = Timesheet(agent_dir)
        result = ts.clock_out()
        assert result is None


class TestTimesheetManager:
    def test_clock_in_out(self, tmp_path: Path) -> None:
        mgr = TimesheetManager(tmp_path)
        (tmp_path / "agent-1" / "today").mkdir(parents=True)
        (tmp_path / "agent-1" / "journal").mkdir(parents=True)

        mgr.clock_in("agent-1")
        entry = mgr.clock_out("agent-1", tasks_completed=3)
        assert entry is not None
        assert entry.tasks_completed == 3

    def test_all_today(self, tmp_path: Path) -> None:
        mgr = TimesheetManager(tmp_path)
        for aid in ("agent-1", "agent-2"):
            (tmp_path / aid / "today").mkdir(parents=True)
            (tmp_path / aid / "journal").mkdir(parents=True)

        mgr.clock_in("agent-1")
        mgr.clock_in("agent-2")
        mgr.clock_out("agent-1", tasks_completed=2)
        mgr.clock_out("agent-2", tasks_completed=5)

        all_today = mgr.all_today()
        assert "agent-1" in all_today
        assert "agent-2" in all_today
        assert all_today["agent-1"].total_tasks_completed == 2
        assert all_today["agent-2"].total_tasks_completed == 5

    def test_clock_out_unknown_agent(self, tmp_path: Path) -> None:
        mgr = TimesheetManager(tmp_path)
        result = mgr.clock_out("nonexistent")
        assert result is None
