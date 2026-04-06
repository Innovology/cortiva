"""Extended tests for timesheet.py — week(), _persist_history, DaySummary
round-trip, multiple clock cycles, and TimesheetManager.all_today()."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cortiva.core.timesheet import DaySummary, Timesheet, TimesheetManager, WorkEntry


class TestDaySummaryRoundTrip:
    def test_to_dict_and_back(self) -> None:
        wake1 = datetime(2026, 3, 29, 9, 0, tzinfo=UTC)
        sleep1 = datetime(2026, 3, 29, 12, 30, tzinfo=UTC)
        wake2 = datetime(2026, 3, 29, 13, 30, tzinfo=UTC)
        sleep2 = datetime(2026, 3, 29, 17, 0, tzinfo=UTC)

        original = DaySummary(
            date="2026-03-29",
            entries=[
                WorkEntry(wake_time=wake1, sleep_time=sleep1, tasks_completed=2),
                WorkEntry(wake_time=wake2, sleep_time=sleep2, tasks_completed=3, tasks_escalated=1),
            ],
            scheduled_hours=8.0,
        )
        d = original.to_dict()

        assert d["date"] == "2026-03-29"
        assert d["total_hours"] == 7.0
        assert d["scheduled_hours"] == 8.0
        assert d["overtime_hours"] == 0.0
        assert d["tasks_completed"] == 5
        assert d["tasks_escalated"] == 1
        assert len(d["entries"]) == 2

        # Reconstruct entries from dict
        restored_entries = [WorkEntry.from_dict(e) for e in d["entries"]]
        restored = DaySummary(
            date=d["date"],
            entries=restored_entries,
            scheduled_hours=d["scheduled_hours"],
        )
        assert restored.total_hours == original.total_hours
        assert restored.total_tasks_completed == original.total_tasks_completed
        assert restored.total_tasks_escalated == original.total_tasks_escalated


class TestPersistHistory:
    def test_persist_history_creates_journal_file(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        (agent_dir / "today").mkdir(parents=True)
        (agent_dir / "journal").mkdir(parents=True)

        ts = Timesheet(agent_dir, scheduled_hours=8.0)
        entry = ts.clock_in()
        ts.clock_out(tasks_completed=4, tasks_escalated=1, consciousness_calls=8)

        date_str = entry.wake_time.strftime("%Y-%m-%d")
        history_path = agent_dir / "journal" / f"timesheet-{date_str}.json"
        assert history_path.exists()

        data = json.loads(history_path.read_text(encoding="utf-8"))
        assert data["date"] == date_str
        assert data["scheduled_hours"] == 8.0
        assert len(data["entries"]) == 1
        assert data["entries"][0]["tasks_completed"] == 4

    def test_persist_history_multiple_entries(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        (agent_dir / "today").mkdir(parents=True)
        (agent_dir / "journal").mkdir(parents=True)

        ts = Timesheet(agent_dir)
        ts.clock_in()
        ts.clock_out(tasks_completed=2)
        ts.clock_in()
        ts.clock_out(tasks_completed=3)

        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        history_path = agent_dir / "journal" / f"timesheet-{date_str}.json"
        data = json.loads(history_path.read_text(encoding="utf-8"))
        assert len(data["entries"]) == 2


class TestMultipleClockCycles:
    def test_multiple_clock_in_out_cycles(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        (agent_dir / "today").mkdir(parents=True)
        (agent_dir / "journal").mkdir(parents=True)

        ts = Timesheet(agent_dir, scheduled_hours=8.0)

        # Morning session
        ts.clock_in()
        assert ts.is_clocked_in
        entry1 = ts.clock_out(tasks_completed=3)
        assert entry1 is not None
        assert not ts.is_clocked_in

        # Afternoon session
        ts.clock_in()
        assert ts.is_clocked_in
        entry2 = ts.clock_out(tasks_completed=2, consciousness_calls=5)
        assert entry2 is not None
        assert not ts.is_clocked_in

        # Evening session
        ts.clock_in()
        entry3 = ts.clock_out(tasks_completed=1, tasks_escalated=1)
        assert entry3 is not None

        today = ts.today()
        assert len(today.entries) == 3
        assert today.total_tasks_completed == 6
        assert today.total_tasks_escalated == 1

    def test_clock_out_without_clock_in_returns_none(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        (agent_dir / "today").mkdir(parents=True)
        ts = Timesheet(agent_dir)
        assert ts.clock_out() is None


class TestWeek:
    def test_week_returns_today(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        (agent_dir / "today").mkdir(parents=True)
        (agent_dir / "journal").mkdir(parents=True)

        ts = Timesheet(agent_dir, scheduled_hours=6.0)
        ts.clock_in()
        ts.clock_out(tasks_completed=5)

        week = ts.week()
        assert len(week) >= 1  # At least today

        # Today should have our entry
        today_str = datetime.now(UTC).strftime("%Y-%m-%d")
        today_summary = next((d for d in week if d.date == today_str), None)
        assert today_summary is not None
        assert today_summary.total_tasks_completed == 5

    def test_week_loads_historical_entries(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        (agent_dir / "today").mkdir(parents=True)
        journal_dir = agent_dir / "journal"
        journal_dir.mkdir(parents=True)

        # Write a historical entry for yesterday (if not Monday)
        now = datetime.now(UTC)
        if now.weekday() > 0:
            yesterday = now - timedelta(days=1)
            date_str = yesterday.strftime("%Y-%m-%d")
            history_data = {
                "date": date_str,
                "scheduled_hours": 8.0,
                "entries": [
                    {
                        "wake_time": yesterday.replace(hour=9).isoformat(),
                        "sleep_time": yesterday.replace(hour=17).isoformat(),
                        "hours": 8.0,
                        "tasks_completed": 10,
                        "tasks_escalated": 0,
                        "consciousness_calls": 20,
                    }
                ],
            }
            (journal_dir / f"timesheet-{date_str}.json").write_text(
                json.dumps(history_data), encoding="utf-8",
            )

        ts = Timesheet(agent_dir)
        week = ts.week()

        if now.weekday() > 0:
            assert len(week) >= 2  # yesterday + today
            yesterday_summary = week[-2]  # second to last
            assert yesterday_summary.total_tasks_completed == 10

    def test_week_missing_history_returns_empty_summary(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        (agent_dir / "today").mkdir(parents=True)
        (agent_dir / "journal").mkdir(parents=True)

        ts = Timesheet(agent_dir)
        week = ts.week()
        # All days should have a summary (possibly empty)
        for day in week:
            assert isinstance(day, DaySummary)
            assert day.date != ""


class TestTimesheetManagerAllToday:
    def test_all_today_with_multiple_agents(self, tmp_path: Path) -> None:
        mgr = TimesheetManager(tmp_path)

        for aid in ("alice", "bob", "charlie"):
            (tmp_path / aid / "today").mkdir(parents=True)
            (tmp_path / aid / "journal").mkdir(parents=True)

        mgr.clock_in("alice", scheduled_hours=8.0)
        mgr.clock_out("alice", tasks_completed=4)

        mgr.clock_in("bob", scheduled_hours=6.0)
        mgr.clock_out("bob", tasks_completed=7, tasks_escalated=2)

        mgr.clock_in("charlie", scheduled_hours=10.0)
        mgr.clock_out("charlie", tasks_completed=1)

        result = mgr.all_today()
        assert len(result) == 3
        assert result["alice"].total_tasks_completed == 4
        assert result["bob"].total_tasks_completed == 7
        assert result["bob"].total_tasks_escalated == 2
        assert result["charlie"].total_tasks_completed == 1
        assert result["charlie"].scheduled_hours == 10.0

    def test_all_today_empty(self, tmp_path: Path) -> None:
        mgr = TimesheetManager(tmp_path)
        assert mgr.all_today() == {}


class TestTimesheetLoadCorruptData:
    def test_corrupt_today_file(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        today_dir = agent_dir / "today"
        today_dir.mkdir(parents=True)
        (today_dir / "timesheet.json").write_text("not json!!!", encoding="utf-8")

        ts = Timesheet(agent_dir)
        assert len(ts.today().entries) == 0

    def test_corrupt_history_file(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        (agent_dir / "today").mkdir(parents=True)
        journal_dir = agent_dir / "journal"
        journal_dir.mkdir(parents=True)

        now = datetime.now(UTC)
        if now.weekday() > 0:
            yesterday = now - timedelta(days=1)
            date_str = yesterday.strftime("%Y-%m-%d")
            (journal_dir / f"timesheet-{date_str}.json").write_text(
                "broken{json", encoding="utf-8",
            )

            ts = Timesheet(agent_dir)
            week = ts.week()
            # Should not crash, corrupt day returns empty
            assert isinstance(week, list)
