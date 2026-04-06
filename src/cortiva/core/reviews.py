"""
Periodic performance review system.

Aggregates agent work data into structured performance reviews with
trend analysis.  Reviews are persisted as JSON in the agent's journal
directory and can be compared across periods.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# WorkEntry — lightweight record of one day's work
# ---------------------------------------------------------------------------

@dataclass
class WorkEntry:
    """A single day's work record for an agent."""

    date: date
    hours_worked: float = 0.0
    scheduled_hours: float = 8.0
    tasks_completed: int = 0
    tasks_escalated: int = 0
    consciousness_calls: int = 0


# ---------------------------------------------------------------------------
# Enums & value objects
# ---------------------------------------------------------------------------

class ReviewPeriod(Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"

    @property
    def days(self) -> int:
        return {
            ReviewPeriod.WEEKLY: 7,
            ReviewPeriod.MONTHLY: 30,
            ReviewPeriod.QUARTERLY: 90,
        }[self]


@dataclass
class PerformanceMetrics:
    """Aggregated metrics for a review period."""

    total_hours: float = 0.0
    scheduled_hours: float = 0.0
    overtime_hours: float = 0.0
    tasks_completed: int = 0
    tasks_escalated: int = 0
    escalation_ratio: float = 0.0
    consciousness_calls: int = 0
    budget_efficiency: float = 0.0  # calls per task
    days_active: int = 0
    avg_hours_per_day: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_hours": round(self.total_hours, 2),
            "scheduled_hours": round(self.scheduled_hours, 2),
            "overtime_hours": round(self.overtime_hours, 2),
            "tasks_completed": self.tasks_completed,
            "tasks_escalated": self.tasks_escalated,
            "escalation_ratio": round(self.escalation_ratio, 4),
            "consciousness_calls": self.consciousness_calls,
            "budget_efficiency": round(self.budget_efficiency, 4),
            "days_active": self.days_active,
            "avg_hours_per_day": round(self.avg_hours_per_day, 2),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PerformanceMetrics:
        return cls(
            total_hours=data.get("total_hours", 0.0),
            scheduled_hours=data.get("scheduled_hours", 0.0),
            overtime_hours=data.get("overtime_hours", 0.0),
            tasks_completed=data.get("tasks_completed", 0),
            tasks_escalated=data.get("tasks_escalated", 0),
            escalation_ratio=data.get("escalation_ratio", 0.0),
            consciousness_calls=data.get("consciousness_calls", 0),
            budget_efficiency=data.get("budget_efficiency", 0.0),
            days_active=data.get("days_active", 0),
            avg_hours_per_day=data.get("avg_hours_per_day", 0.0),
        )


@dataclass
class PerformanceReview:
    """A completed performance review for a single period."""

    agent_id: str
    period: ReviewPeriod
    start_date: str  # ISO date
    end_date: str  # ISO date
    metrics: PerformanceMetrics
    trend: str = "stable"  # "improving" | "stable" | "declining"
    created_at: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "period": self.period.value,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "metrics": self.metrics.to_dict(),
            "trend": self.trend,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PerformanceReview:
        return cls(
            agent_id=data["agent_id"],
            period=ReviewPeriod(data["period"]),
            start_date=data["start_date"],
            end_date=data["end_date"],
            metrics=PerformanceMetrics.from_dict(data.get("metrics", {})),
            trend=data.get("trend", "stable"),
            created_at=data.get("created_at", ""),
        )


# ---------------------------------------------------------------------------
# Pure computation
# ---------------------------------------------------------------------------

def compute_metrics(entries: list[WorkEntry]) -> PerformanceMetrics:
    """Compute aggregate metrics from a list of work entries.

    This is a pure function — no I/O, no side-effects.
    """
    if not entries:
        return PerformanceMetrics(scheduled_hours=0.0)

    total_hours = sum(e.hours_worked for e in entries)
    scheduled_hours = sum(e.scheduled_hours for e in entries)
    overtime_hours = sum(
        max(0.0, e.hours_worked - e.scheduled_hours) for e in entries
    )
    tasks_completed = sum(e.tasks_completed for e in entries)
    tasks_escalated = sum(e.tasks_escalated for e in entries)
    consciousness_calls = sum(e.consciousness_calls for e in entries)
    days_active = sum(1 for e in entries if e.hours_worked > 0)

    total_tasks = tasks_completed + tasks_escalated
    escalation_ratio = tasks_escalated / total_tasks if total_tasks > 0 else 0.0
    budget_efficiency = (
        consciousness_calls / tasks_completed if tasks_completed > 0 else 0.0
    )
    avg_hours_per_day = total_hours / days_active if days_active > 0 else 0.0

    return PerformanceMetrics(
        total_hours=total_hours,
        scheduled_hours=scheduled_hours,
        overtime_hours=overtime_hours,
        tasks_completed=tasks_completed,
        tasks_escalated=tasks_escalated,
        escalation_ratio=escalation_ratio,
        consciousness_calls=consciousness_calls,
        budget_efficiency=budget_efficiency,
        days_active=days_active,
        avg_hours_per_day=avg_hours_per_day,
    )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _journal_dir(agent_dir: Path) -> Path:
    return agent_dir / "journal"


def _review_filename(period: ReviewPeriod, end_date: str) -> str:
    return f"review-{period.value}-{end_date}.json"


def _load_work_entries(agent_dir: Path, start: date, end: date) -> list[WorkEntry]:
    """Load work entries from journal/work-log.json for the given date range.

    Each entry in the file is expected to have: date, hours_worked,
    scheduled_hours, tasks_completed, tasks_escalated, consciousness_calls.
    """
    log_path = _journal_dir(agent_dir) / "work-log.json"
    if not log_path.exists():
        return []

    data = json.loads(log_path.read_text(encoding="utf-8"))
    entries_data = data if isinstance(data, list) else data.get("entries", [])

    entries: list[WorkEntry] = []
    for item in entries_data:
        entry_date = date.fromisoformat(item["date"])
        if start <= entry_date <= end:
            entries.append(
                WorkEntry(
                    date=entry_date,
                    hours_worked=item.get("hours_worked", 0.0),
                    scheduled_hours=item.get("scheduled_hours", 8.0),
                    tasks_completed=item.get("tasks_completed", 0),
                    tasks_escalated=item.get("tasks_escalated", 0),
                    consciousness_calls=item.get("consciousness_calls", 0),
                )
            )
    return entries


# ---------------------------------------------------------------------------
# ReviewManager
# ---------------------------------------------------------------------------

def _period_range(period: ReviewPeriod, ref_date: date | None = None) -> tuple[date, date]:
    """Compute (start, end) for the most recent completed period."""
    today = ref_date or date.today()
    end = today
    start = end - timedelta(days=period.days)
    return start, end


def _previous_period_range(
    period: ReviewPeriod, ref_date: date | None = None
) -> tuple[date, date]:
    """Compute (start, end) for the period before the current one."""
    today = ref_date or date.today()
    end = today - timedelta(days=period.days)
    start = end - timedelta(days=period.days)
    return start, end


class ReviewManager:
    """High-level manager for periodic performance reviews."""

    def generate_review(
        self,
        agent_dir: Path,
        period: ReviewPeriod,
        ref_date: date | None = None,
    ) -> PerformanceReview:
        """Aggregate timesheet data for the given period and produce a review."""
        agent_id = agent_dir.name
        current_start, current_end = _period_range(period, ref_date)
        prev_start, prev_end = _previous_period_range(period, ref_date)

        current_entries = _load_work_entries(agent_dir, current_start, current_end)
        prev_entries = _load_work_entries(agent_dir, prev_start, prev_end)

        metrics = compute_metrics(current_entries)
        trend = _compute_trend(current_entries, prev_entries)

        return PerformanceReview(
            agent_id=agent_id,
            period=period,
            start_date=current_start.isoformat(),
            end_date=current_end.isoformat(),
            metrics=metrics,
            trend=trend,
        )

    def compare_to_previous(
        self,
        agent_dir: Path,
        period: ReviewPeriod,
        ref_date: date | None = None,
    ) -> str:
        """Compute trend by comparing current period to the previous one.

        Returns "improving", "stable", or "declining".
        """
        current_start, current_end = _period_range(period, ref_date)
        prev_start, prev_end = _previous_period_range(period, ref_date)

        current_entries = _load_work_entries(agent_dir, current_start, current_end)
        prev_entries = _load_work_entries(agent_dir, prev_start, prev_end)

        return _compute_trend(current_entries, prev_entries)

    def save_review(self, agent_dir: Path, review: PerformanceReview) -> Path:
        """Persist a review to journal/review-{period}-{date}.json."""
        journal = _journal_dir(agent_dir)
        journal.mkdir(parents=True, exist_ok=True)
        filename = _review_filename(review.period, review.end_date)
        path = journal / filename
        path.write_text(
            json.dumps(review.to_dict(), indent=2),
            encoding="utf-8",
        )
        return path

    def load_reviews(self, agent_dir: Path) -> list[PerformanceReview]:
        """Load all past reviews from the journal directory."""
        journal = _journal_dir(agent_dir)
        if not journal.exists():
            return []

        reviews: list[PerformanceReview] = []
        for path in sorted(journal.glob("review-*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            reviews.append(PerformanceReview.from_dict(data))
        return reviews


def _compute_trend(
    current_entries: list[WorkEntry], prev_entries: list[WorkEntry]
) -> str:
    """Compute trend from two sets of work entries."""
    prev_metrics = compute_metrics(prev_entries)
    if prev_metrics.days_active == 0:
        return "stable"
    current_metrics = compute_metrics(current_entries)
    return _determine_trend(current_metrics, prev_metrics)


def _determine_trend(
    current: PerformanceMetrics, previous: PerformanceMetrics
) -> str:
    """Compare two metric sets and return a trend label.

    Scoring: +1 for each improved signal, -1 for each declined signal.
    Signals: tasks_completed (higher=better), escalation_ratio (lower=better),
    avg_hours_per_day (higher=better), budget_efficiency (lower=better).
    """
    score = 0

    if current.tasks_completed > previous.tasks_completed:
        score += 1
    elif current.tasks_completed < previous.tasks_completed:
        score -= 1

    if current.escalation_ratio < previous.escalation_ratio:
        score += 1
    elif current.escalation_ratio > previous.escalation_ratio:
        score -= 1

    if current.avg_hours_per_day > previous.avg_hours_per_day:
        score += 1
    elif current.avg_hours_per_day < previous.avg_hours_per_day:
        score -= 1

    if current.budget_efficiency < previous.budget_efficiency:
        score += 1
    elif current.budget_efficiency > previous.budget_efficiency:
        score -= 1

    if score >= 2:
        return "improving"
    elif score <= -2:
        return "declining"
    return "stable"
