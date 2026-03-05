"""
Agent Scheduler — time-based lifecycle management.

Checks agent schedules on each heartbeat tick and triggers state
transitions: wake sleeping agents, trigger replan for executing
agents, and sleep agents at end-of-day.

Schedule format (simple, no cron dependency):
    wake: "09:00"           # daily at 9am
    wake: "09:00 mon-fri"   # weekdays only
    replan: "12:00,15:00"   # multiple times
    sleep: "17:00"          # daily at 5pm
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Day name → weekday number (Monday=0)
_DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}

# Precomputed day range shortcuts
_DAY_RANGES = {
    "weekdays": {0, 1, 2, 3, 4},
    "weekends": {5, 6},
    "daily": {0, 1, 2, 3, 4, 5, 6},
}


def _parse_days(day_spec: str) -> set[int]:
    """Parse a day specification like 'mon-fri' or 'mon,wed,fri'."""
    day_spec = day_spec.strip().lower()
    if day_spec in _DAY_RANGES:
        return _DAY_RANGES[day_spec]

    # Range: mon-fri
    range_match = re.match(r"(\w{3})-(\w{3})", day_spec)
    if range_match:
        start = _DAY_MAP.get(range_match.group(1))
        end = _DAY_MAP.get(range_match.group(2))
        if start is not None and end is not None:
            if start <= end:
                return set(range(start, end + 1))
            # Wrap around: fri-mon = {4,5,6,0}
            return set(range(start, 7)) | set(range(0, end + 1))

    # Comma list: mon,wed,fri
    days = set()
    for part in day_spec.split(","):
        d = _DAY_MAP.get(part.strip())
        if d is not None:
            days.add(d)
    return days if days else _DAY_RANGES["daily"]


def _parse_times(time_spec: str) -> list[tuple[int, int]]:
    """Parse time(s) like '09:00' or '12:00,15:00'."""
    times = []
    for part in time_spec.split(","):
        part = part.strip()
        match = re.match(r"(\d{1,2}):(\d{2})", part)
        if match:
            times.append((int(match.group(1)), int(match.group(2))))
    return times


@dataclass
class ScheduleEntry:
    """A single scheduled event."""
    action: str              # "wake" | "replan" | "sleep"
    times: list[tuple[int, int]]   # [(hour, minute), ...]
    days: set[int]           # weekday numbers (0=Mon)

    def is_due(self, now: datetime, tolerance_minutes: int = 5) -> bool:
        """Check if this entry is due at *now* (within tolerance window)."""
        if now.weekday() not in self.days:
            return False
        for hour, minute in self.times:
            target_min = hour * 60 + minute
            current_min = now.hour * 60 + now.minute
            if 0 <= (current_min - target_min) < tolerance_minutes:
                return True
        return False


@dataclass
class AgentSchedule:
    """Complete schedule for one agent."""
    agent_id: str
    entries: list[ScheduleEntry] = field(default_factory=list)
    # Track last trigger time per action to avoid re-triggering within same window
    last_triggered: dict[str, str] = field(default_factory=dict)

    def due_actions(self, now: datetime, tolerance_minutes: int = 5) -> list[str]:
        """Return list of actions that are due right now."""
        window_key = now.strftime("%Y-%m-%d-%H-%M")
        actions = []
        for entry in self.entries:
            if entry.is_due(now, tolerance_minutes):
                # Check if already triggered in this window
                last = self.last_triggered.get(entry.action)
                # Use a coarser key (hour+minute block) to prevent re-trigger
                trigger_key = f"{now.strftime('%Y-%m-%d')}-{entry.action}"
                for h, m in entry.times:
                    target_min = h * 60 + m
                    current_min = now.hour * 60 + now.minute
                    if 0 <= (current_min - target_min) < tolerance_minutes:
                        block_key = f"{now.strftime('%Y-%m-%d')}-{h:02d}:{m:02d}-{entry.action}"
                        if self.last_triggered.get(entry.action) != block_key:
                            self.last_triggered[entry.action] = block_key
                            actions.append(entry.action)
                            break
        return actions


def parse_schedule(agent_id: str, config: dict[str, str]) -> AgentSchedule:
    """Parse a schedule config dict into an AgentSchedule.

    Config format:
        {"wake": "09:00 mon-fri", "replan": "12:00,15:00", "sleep": "17:00"}
    """
    entries = []
    for action in ("wake", "replan", "sleep"):
        spec = config.get(action)
        if not spec:
            continue

        parts = spec.strip().split(maxsplit=1)
        time_part = parts[0]
        day_part = parts[1] if len(parts) > 1 else "daily"

        times = _parse_times(time_part)
        days = _parse_days(day_part)

        if times:
            entries.append(ScheduleEntry(action=action, times=times, days=days))

    return AgentSchedule(agent_id=agent_id, entries=entries)


class Scheduler:
    """Manages schedules for all agents and checks for due actions."""

    def __init__(self) -> None:
        self._schedules: dict[str, AgentSchedule] = {}

    def register(self, agent_id: str, config: dict[str, str]) -> None:
        """Register or update an agent's schedule."""
        self._schedules[agent_id] = parse_schedule(agent_id, config)

    def unregister(self, agent_id: str) -> None:
        """Remove an agent's schedule."""
        self._schedules.pop(agent_id, None)

    def tick(self, now: datetime | None = None) -> dict[str, list[str]]:
        """Check all schedules and return due actions per agent.

        Returns a dict like ``{"bookkeep-01": ["wake"], "dev-01": ["replan"]}``.
        """
        now = now or datetime.now(timezone.utc)
        result: dict[str, list[str]] = {}
        for agent_id, schedule in self._schedules.items():
            actions = schedule.due_actions(now)
            if actions:
                result[agent_id] = actions
        return result

    def get_schedule(self, agent_id: str) -> AgentSchedule | None:
        return self._schedules.get(agent_id)

    @property
    def agent_ids(self) -> list[str]:
        return list(self._schedules.keys())
