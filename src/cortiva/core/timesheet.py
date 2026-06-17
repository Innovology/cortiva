"""
Agent timesheet — tracks working hours, overtime, and activity.

Each agent accumulates :class:`WorkEntry` records as it wakes and
sleeps.  The :class:`Timesheet` persists these to
``today/timesheet.json`` inside the agent's directory so they survive
daemon restarts.

Overtime is calculated by comparing actual hours worked against the
scheduled hours derived from the agent's schedule config.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortiva.timesheet")

# An orphaned (crash-left) open session has no real sleep time. We close it
# with a BOUNDED duration so it can never read as ``now - wake_time`` — a
# session left open across restarts otherwise accumulated 100h+ and tripped
# the max-hours-per-day gate, blocking agents from working entirely.
_MAX_ORPHAN_HOURS = 12.0


@dataclass
class WorkEntry:
    """A single wake-to-sleep work period."""

    wake_time: datetime
    sleep_time: datetime | None = None
    tasks_completed: int = 0
    tasks_escalated: int = 0
    consciousness_calls: int = 0

    @property
    def duration(self) -> timedelta:
        end = self.sleep_time or datetime.now(UTC)
        return end - self.wake_time

    @property
    def hours(self) -> float:
        return self.duration.total_seconds() / 3600

    def to_dict(self) -> dict[str, Any]:
        return {
            "wake_time": self.wake_time.isoformat(),
            "sleep_time": self.sleep_time.isoformat() if self.sleep_time else None,
            "hours": round(self.hours, 2),
            "tasks_completed": self.tasks_completed,
            "tasks_escalated": self.tasks_escalated,
            "consciousness_calls": self.consciousness_calls,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkEntry:
        wake = datetime.fromisoformat(data["wake_time"])
        sleep = datetime.fromisoformat(data["sleep_time"]) if data.get("sleep_time") else None
        return cls(
            wake_time=wake,
            sleep_time=sleep,
            tasks_completed=data.get("tasks_completed", 0),
            tasks_escalated=data.get("tasks_escalated", 0),
            consciousness_calls=data.get("consciousness_calls", 0),
        )


@dataclass
class DaySummary:
    """Aggregated hours for a single day."""

    date: str
    entries: list[WorkEntry] = field(default_factory=list)
    scheduled_hours: float = 8.0

    @property
    def total_hours(self) -> float:
        return sum(e.hours for e in self.entries)

    @property
    def overtime_hours(self) -> float:
        return max(0.0, self.total_hours - self.scheduled_hours)

    @property
    def total_tasks_completed(self) -> int:
        return sum(e.tasks_completed for e in self.entries)

    @property
    def total_tasks_escalated(self) -> int:
        return sum(e.tasks_escalated for e in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "total_hours": round(self.total_hours, 2),
            "scheduled_hours": self.scheduled_hours,
            "overtime_hours": round(self.overtime_hours, 2),
            "entries": [e.to_dict() for e in self.entries],
            "tasks_completed": self.total_tasks_completed,
            "tasks_escalated": self.total_tasks_escalated,
        }


class Timesheet:
    """Manages work entries for a single agent.

    Entries are persisted to ``today/timesheet.json`` and historical
    entries to ``journal/timesheet-YYYY-MM-DD.json``.
    """

    def __init__(self, agent_dir: Path, scheduled_hours: float = 8.0) -> None:
        self._agent_dir = agent_dir
        self._scheduled_hours = scheduled_hours
        self._current_entry: WorkEntry | None = None
        self._today_entries: list[WorkEntry] = []
        self._load_today()

    def _today_path(self) -> Path:
        return self._agent_dir / "today" / "timesheet.json"

    def _history_path(self, date: str) -> Path:
        return self._agent_dir / "journal" / f"timesheet-{date}.json"

    def _load_today(self) -> None:
        """Load today's entries from disk, reconciling crashes + day rollover.

        Two failure modes are healed here, both of which otherwise produced
        phantom hours that blocked agents on the max-hours gate:

        1. **Orphaned open sessions.** A crash/hard-restart leaves a session
           with no ``sleep_time``; its ``hours`` would compute as
           ``now - wake_time`` (days). We close every loaded open session with
           a bounded duration so it can never blow up.
        2. **Stale day.** Nothing rolled ``today/timesheet.json`` over at
           midnight, so yesterday's entries kept counting toward today. If the
           persisted file is from a previous day we archive it to history and
           start today empty; any stray prior-day entries in a same-day file
           are archived out too. Today's summary only ever counts today.
        """
        path = self._today_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, OSError):
            self._today_entries = []
            return

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        file_date = str(data.get("date") or "")
        entries: list[WorkEntry] = []
        for e in data.get("entries", []):
            try:
                entries.append(WorkEntry.from_dict(e))
            except (KeyError, ValueError, TypeError):
                continue

        # 1. Close orphaned open sessions with a bounded duration (never now-wake).
        now = datetime.now(UTC)
        changed = False
        for e in entries:
            if e.sleep_time is None:
                cap_h = max(0.0, min(self._scheduled_hours, _MAX_ORPHAN_HOURS))
                e.sleep_time = min(now, e.wake_time + timedelta(hours=cap_h))
                changed = True

        # 2. Day rollover: a previous-day file is archived whole; same-day files
        #    keep only today's entries (any stragglers archived to their day).
        if file_date and file_date != today:
            self._archive_entries(entries)
            self._today_entries = []
            self._current_entry = None
            self._persist()
            return

        todays = [e for e in entries if e.wake_time.strftime("%Y-%m-%d") == today]
        older = [e for e in entries if e.wake_time.strftime("%Y-%m-%d") != today]
        if older:
            self._archive_entries(older)
        self._today_entries = todays
        # All loaded sessions are now closed — a fresh wake clocks in anew.
        self._current_entry = None
        if changed or older:
            self._persist()

    def _archive_entries(self, entries: list[WorkEntry]) -> None:
        """Merge closed entries into their day's history file (no overwrite)."""
        from collections import defaultdict

        by_date: dict[str, list[WorkEntry]] = defaultdict(list)
        for e in entries:
            by_date[e.wake_time.strftime("%Y-%m-%d")].append(e)
        for date_str, day_entries in by_date.items():
            path = self._history_path(date_str)
            path.parent.mkdir(parents=True, exist_ok=True)
            existing: list[dict[str, Any]] = []
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8")).get("entries", [])
                except (json.JSONDecodeError, OSError):
                    existing = []
            path.write_text(
                json.dumps(
                    {
                        "date": date_str,
                        "scheduled_hours": self._scheduled_hours,
                        "entries": existing + [e.to_dict() for e in day_entries],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    def _persist(self) -> None:
        """Write current entries to disk."""
        path = self._today_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "date": datetime.now(UTC).strftime("%Y-%m-%d"),
            "entries": [e.to_dict() for e in self._today_entries],
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def clock_in(self) -> WorkEntry:
        """Record a wake event."""
        entry = WorkEntry(wake_time=datetime.now(UTC))
        self._current_entry = entry
        self._today_entries.append(entry)
        self._persist()
        logger.debug("Agent clocked in at %s", entry.wake_time)
        return entry

    def clock_out(
        self,
        tasks_completed: int = 0,
        tasks_escalated: int = 0,
        consciousness_calls: int = 0,
    ) -> WorkEntry | None:
        """Record a sleep event."""
        if self._current_entry is None:
            return None
        entry = self._current_entry
        entry.sleep_time = datetime.now(UTC)
        entry.tasks_completed = tasks_completed
        entry.tasks_escalated = tasks_escalated
        entry.consciousness_calls = consciousness_calls
        self._current_entry = None
        self._persist()

        # Also persist to historical file
        date_str = entry.wake_time.strftime("%Y-%m-%d")
        self._persist_history(date_str)

        logger.debug("Agent clocked out at %s (%.1fh)", entry.sleep_time, entry.hours)
        return entry

    def _persist_history(self, date_str: str) -> None:
        """Write a day's entries to the journal history file."""
        path = self._history_path(date_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        day_entries = [
            e for e in self._today_entries if e.wake_time.strftime("%Y-%m-%d") == date_str
        ]
        data = {
            "date": date_str,
            "scheduled_hours": self._scheduled_hours,
            "entries": [e.to_dict() for e in day_entries],
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @property
    def current_entry(self) -> WorkEntry | None:
        return self._current_entry

    @property
    def is_clocked_in(self) -> bool:
        return self._current_entry is not None

    def today(self) -> DaySummary:
        """Get today's summary."""
        return DaySummary(
            date=datetime.now(UTC).strftime("%Y-%m-%d"),
            entries=list(self._today_entries),
            scheduled_hours=self._scheduled_hours,
        )

    def week(self) -> list[DaySummary]:
        """Get this week's summaries from historical files."""
        now = datetime.now(UTC)
        # Go back to Monday
        monday = now - timedelta(days=now.weekday())
        summaries: list[DaySummary] = []

        for i in range(7):
            day = monday + timedelta(days=i)
            if day > now:
                break
            date_str = day.strftime("%Y-%m-%d")
            summary = self._load_day(date_str)
            summaries.append(summary)

        return summaries

    def _load_day(self, date_str: str) -> DaySummary:
        """Load a day's summary from history."""
        # Check if it's today
        if date_str == datetime.now(UTC).strftime("%Y-%m-%d"):
            return self.today()

        path = self._history_path(date_str)
        if not path.exists():
            return DaySummary(date=date_str, scheduled_hours=self._scheduled_hours)

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = [WorkEntry.from_dict(e) for e in data.get("entries", [])]
            return DaySummary(
                date=date_str,
                entries=entries,
                scheduled_hours=data.get("scheduled_hours", self._scheduled_hours),
            )
        except (json.JSONDecodeError, KeyError):
            return DaySummary(date=date_str, scheduled_hours=self._scheduled_hours)

    def reset_today(self) -> None:
        """Clear today's entries (called at start of new day)."""
        self._today_entries.clear()
        self._current_entry = None
        path = self._today_path()
        if path.exists():
            path.unlink()


class TimesheetManager:
    """Manages timesheets for all agents in a Fabric."""

    def __init__(self, agents_dir: Path) -> None:
        self._agents_dir = agents_dir
        self._timesheets: dict[str, Timesheet] = {}

    def get(self, agent_id: str, scheduled_hours: float = 8.0) -> Timesheet:
        """Get or create a timesheet for an agent."""
        if agent_id not in self._timesheets:
            agent_dir = self._agents_dir / agent_id
            self._timesheets[agent_id] = Timesheet(agent_dir, scheduled_hours)
        return self._timesheets[agent_id]

    def clock_in(self, agent_id: str, scheduled_hours: float = 8.0) -> WorkEntry:
        return self.get(agent_id, scheduled_hours).clock_in()

    def clock_out(
        self,
        agent_id: str,
        tasks_completed: int = 0,
        tasks_escalated: int = 0,
        consciousness_calls: int = 0,
    ) -> WorkEntry | None:
        ts = self._timesheets.get(agent_id)
        if ts is None:
            return None
        return ts.clock_out(
            tasks_completed=tasks_completed,
            tasks_escalated=tasks_escalated,
            consciousness_calls=consciousness_calls,
        )

    def all_today(self) -> dict[str, DaySummary]:
        """Get today's summary for all tracked agents."""
        return {aid: ts.today() for aid, ts in self._timesheets.items()}
