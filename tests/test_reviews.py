"""Tests for the periodic performance review system."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from cortiva.core.reviews import (
    PerformanceMetrics,
    PerformanceReview,
    ReviewManager,
    ReviewPeriod,
    WorkEntry,
    _determine_trend,
    compute_metrics,
)


# ---------------------------------------------------------------------------
# ReviewPeriod
# ---------------------------------------------------------------------------


class TestReviewPeriod:
    def test_values(self) -> None:
        assert ReviewPeriod.WEEKLY.value == "weekly"
        assert ReviewPeriod.MONTHLY.value == "monthly"
        assert ReviewPeriod.QUARTERLY.value == "quarterly"

    def test_days(self) -> None:
        assert ReviewPeriod.WEEKLY.days == 7
        assert ReviewPeriod.MONTHLY.days == 30
        assert ReviewPeriod.QUARTERLY.days == 90


# ---------------------------------------------------------------------------
# compute_metrics — pure function
# ---------------------------------------------------------------------------


class TestComputeMetrics:
    def test_empty_entries(self) -> None:
        m = compute_metrics([])
        assert m.total_hours == 0.0
        assert m.days_active == 0
        assert m.tasks_completed == 0

    def test_single_entry(self) -> None:
        entries = [
            WorkEntry(
                date=date(2026, 3, 1),
                hours_worked=8.0,
                scheduled_hours=8.0,
                tasks_completed=5,
                tasks_escalated=1,
                consciousness_calls=3,
            )
        ]
        m = compute_metrics(entries)
        assert m.total_hours == 8.0
        assert m.scheduled_hours == 8.0
        assert m.overtime_hours == 0.0
        assert m.tasks_completed == 5
        assert m.tasks_escalated == 1
        assert m.escalation_ratio == pytest.approx(1 / 6)
        assert m.consciousness_calls == 3
        assert m.budget_efficiency == pytest.approx(3 / 5)
        assert m.days_active == 1
        assert m.avg_hours_per_day == 8.0

    def test_overtime_calculation(self) -> None:
        entries = [
            WorkEntry(date=date(2026, 3, 1), hours_worked=10.0, scheduled_hours=8.0),
            WorkEntry(date=date(2026, 3, 2), hours_worked=6.0, scheduled_hours=8.0),
        ]
        m = compute_metrics(entries)
        assert m.overtime_hours == 2.0  # only first day has overtime

    def test_zero_tasks_no_division_error(self) -> None:
        entries = [
            WorkEntry(date=date(2026, 3, 1), hours_worked=4.0),
        ]
        m = compute_metrics(entries)
        assert m.escalation_ratio == 0.0
        assert m.budget_efficiency == 0.0

    def test_multiple_entries(self) -> None:
        entries = [
            WorkEntry(
                date=date(2026, 3, d),
                hours_worked=8.0,
                tasks_completed=3,
                tasks_escalated=1,
                consciousness_calls=2,
            )
            for d in range(1, 6)
        ]
        m = compute_metrics(entries)
        assert m.total_hours == 40.0
        assert m.tasks_completed == 15
        assert m.tasks_escalated == 5
        assert m.consciousness_calls == 10
        assert m.days_active == 5
        assert m.avg_hours_per_day == 8.0

    def test_inactive_days_excluded_from_avg(self) -> None:
        entries = [
            WorkEntry(date=date(2026, 3, 1), hours_worked=10.0),
            WorkEntry(date=date(2026, 3, 2), hours_worked=0.0),
            WorkEntry(date=date(2026, 3, 3), hours_worked=6.0),
        ]
        m = compute_metrics(entries)
        assert m.days_active == 2
        assert m.avg_hours_per_day == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# PerformanceMetrics serialization
# ---------------------------------------------------------------------------


class TestPerformanceMetricsSerialization:
    def test_roundtrip(self) -> None:
        m = PerformanceMetrics(
            total_hours=40.0,
            scheduled_hours=40.0,
            overtime_hours=2.5,
            tasks_completed=20,
            tasks_escalated=3,
            escalation_ratio=0.1304,
            consciousness_calls=15,
            budget_efficiency=0.75,
            days_active=5,
            avg_hours_per_day=8.0,
        )
        d = m.to_dict()
        m2 = PerformanceMetrics.from_dict(d)
        assert m2.total_hours == pytest.approx(m.total_hours, abs=0.01)
        assert m2.tasks_completed == m.tasks_completed
        assert m2.days_active == m.days_active


# ---------------------------------------------------------------------------
# PerformanceReview serialization
# ---------------------------------------------------------------------------


class TestPerformanceReviewSerialization:
    def test_roundtrip(self) -> None:
        r = PerformanceReview(
            agent_id="agent-1",
            period=ReviewPeriod.WEEKLY,
            start_date="2026-03-22",
            end_date="2026-03-29",
            metrics=PerformanceMetrics(total_hours=40.0, days_active=5),
            trend="improving",
            created_at="2026-03-29T12:00:00+00:00",
        )
        d = r.to_dict()
        assert d["period"] == "weekly"
        assert d["trend"] == "improving"

        r2 = PerformanceReview.from_dict(d)
        assert r2.agent_id == "agent-1"
        assert r2.period == ReviewPeriod.WEEKLY
        assert r2.trend == "improving"
        assert r2.metrics.total_hours == 40.0


# ---------------------------------------------------------------------------
# _determine_trend
# ---------------------------------------------------------------------------


class TestDetermineTrend:
    def test_improving(self) -> None:
        prev = PerformanceMetrics(
            tasks_completed=10,
            escalation_ratio=0.3,
            avg_hours_per_day=6.0,
            budget_efficiency=2.0,
        )
        curr = PerformanceMetrics(
            tasks_completed=15,
            escalation_ratio=0.1,
            avg_hours_per_day=8.0,
            budget_efficiency=1.0,
        )
        assert _determine_trend(curr, prev) == "improving"

    def test_declining(self) -> None:
        prev = PerformanceMetrics(
            tasks_completed=15,
            escalation_ratio=0.1,
            avg_hours_per_day=8.0,
            budget_efficiency=1.0,
        )
        curr = PerformanceMetrics(
            tasks_completed=10,
            escalation_ratio=0.3,
            avg_hours_per_day=6.0,
            budget_efficiency=2.0,
        )
        assert _determine_trend(curr, prev) == "declining"

    def test_stable_mixed_signals(self) -> None:
        prev = PerformanceMetrics(
            tasks_completed=10,
            escalation_ratio=0.2,
            avg_hours_per_day=7.0,
            budget_efficiency=1.5,
        )
        curr = PerformanceMetrics(
            tasks_completed=12,  # better
            escalation_ratio=0.25,  # worse
            avg_hours_per_day=7.0,  # same
            budget_efficiency=1.5,  # same
        )
        assert _determine_trend(curr, prev) == "stable"

    def test_stable_identical(self) -> None:
        m = PerformanceMetrics(tasks_completed=10, escalation_ratio=0.2)
        assert _determine_trend(m, m) == "stable"


# ---------------------------------------------------------------------------
# ReviewManager — integration with filesystem
# ---------------------------------------------------------------------------


class TestReviewManager:
    def _setup_agent(self, tmp_path: Path, entries: list[dict]) -> Path:
        agent_dir = tmp_path / "agent-1"
        journal = agent_dir / "journal"
        journal.mkdir(parents=True)
        log_path = journal / "work-log.json"
        log_path.write_text(json.dumps(entries), encoding="utf-8")
        return agent_dir

    def test_generate_review_no_data(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        mgr = ReviewManager()
        review = mgr.generate_review(
            agent_dir, ReviewPeriod.WEEKLY, ref_date=date(2026, 3, 29)
        )
        assert review.agent_id == "agent-1"
        assert review.metrics.days_active == 0
        assert review.trend == "stable"

    def test_generate_review_with_data(self, tmp_path: Path) -> None:
        ref = date(2026, 3, 29)
        entries = [
            {
                "date": (ref - timedelta(days=i)).isoformat(),
                "hours_worked": 8.0,
                "scheduled_hours": 8.0,
                "tasks_completed": 4,
                "tasks_escalated": 1,
                "consciousness_calls": 2,
            }
            for i in range(7)
        ]
        agent_dir = self._setup_agent(tmp_path, entries)
        mgr = ReviewManager()
        review = mgr.generate_review(
            agent_dir, ReviewPeriod.WEEKLY, ref_date=ref
        )
        assert review.metrics.days_active == 7
        assert review.metrics.tasks_completed == 28
        assert review.metrics.total_hours == 56.0
        assert review.period == ReviewPeriod.WEEKLY

    def test_save_and_load_review(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        mgr = ReviewManager()
        review = PerformanceReview(
            agent_id="agent-1",
            period=ReviewPeriod.MONTHLY,
            start_date="2026-02-27",
            end_date="2026-03-29",
            metrics=PerformanceMetrics(total_hours=160.0, days_active=20),
            trend="improving",
        )
        path = mgr.save_review(agent_dir, review)
        assert path.exists()
        assert "review-monthly-2026-03-29.json" in path.name

        loaded = mgr.load_reviews(agent_dir)
        assert len(loaded) == 1
        assert loaded[0].agent_id == "agent-1"
        assert loaded[0].period == ReviewPeriod.MONTHLY
        assert loaded[0].trend == "improving"

    def test_load_reviews_empty(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        mgr = ReviewManager()
        assert mgr.load_reviews(agent_dir) == []

    def test_load_reviews_multiple(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        journal = agent_dir / "journal"
        journal.mkdir(parents=True)
        mgr = ReviewManager()

        for i, period in enumerate([ReviewPeriod.WEEKLY, ReviewPeriod.MONTHLY]):
            review = PerformanceReview(
                agent_id="agent-1",
                period=period,
                start_date=f"2026-03-0{i + 1}",
                end_date=f"2026-03-{10 + i}",
                metrics=PerformanceMetrics(days_active=i + 1),
            )
            mgr.save_review(agent_dir, review)

        loaded = mgr.load_reviews(agent_dir)
        assert len(loaded) == 2

    def test_compare_to_previous_no_prev_data(self, tmp_path: Path) -> None:
        ref = date(2026, 3, 29)
        entries = [
            {
                "date": (ref - timedelta(days=i)).isoformat(),
                "hours_worked": 8.0,
                "tasks_completed": 5,
            }
            for i in range(7)
        ]
        agent_dir = self._setup_agent(tmp_path, entries)
        mgr = ReviewManager()
        trend = mgr.compare_to_previous(
            agent_dir, ReviewPeriod.WEEKLY, ref_date=ref
        )
        assert trend == "stable"  # no previous data -> stable

    def test_compare_to_previous_improving(self, tmp_path: Path) -> None:
        ref = date(2026, 3, 29)
        entries = []
        # Previous period: fewer tasks, higher escalation
        for i in range(7, 14):
            entries.append({
                "date": (ref - timedelta(days=i)).isoformat(),
                "hours_worked": 6.0,
                "tasks_completed": 2,
                "tasks_escalated": 2,
                "consciousness_calls": 5,
            })
        # Current period: more tasks, lower escalation
        for i in range(7):
            entries.append({
                "date": (ref - timedelta(days=i)).isoformat(),
                "hours_worked": 8.0,
                "tasks_completed": 5,
                "tasks_escalated": 0,
                "consciousness_calls": 2,
            })
        agent_dir = self._setup_agent(tmp_path, entries)
        mgr = ReviewManager()
        trend = mgr.compare_to_previous(
            agent_dir, ReviewPeriod.WEEKLY, ref_date=ref
        )
        assert trend == "improving"

    def test_compare_to_previous_declining(self, tmp_path: Path) -> None:
        ref = date(2026, 3, 29)
        entries = []
        # Previous period: good performance
        for i in range(7, 14):
            entries.append({
                "date": (ref - timedelta(days=i)).isoformat(),
                "hours_worked": 8.0,
                "tasks_completed": 5,
                "tasks_escalated": 0,
                "consciousness_calls": 2,
            })
        # Current period: worse performance
        for i in range(7):
            entries.append({
                "date": (ref - timedelta(days=i)).isoformat(),
                "hours_worked": 4.0,
                "tasks_completed": 1,
                "tasks_escalated": 3,
                "consciousness_calls": 5,
            })
        agent_dir = self._setup_agent(tmp_path, entries)
        mgr = ReviewManager()
        trend = mgr.compare_to_previous(
            agent_dir, ReviewPeriod.WEEKLY, ref_date=ref
        )
        assert trend == "declining"

    def test_generate_review_uses_trend(self, tmp_path: Path) -> None:
        """generate_review should include trend from compare_to_previous."""
        ref = date(2026, 3, 29)
        entries = []
        # Previous period
        for i in range(7, 14):
            entries.append({
                "date": (ref - timedelta(days=i)).isoformat(),
                "hours_worked": 6.0,
                "tasks_completed": 2,
                "tasks_escalated": 2,
                "consciousness_calls": 5,
            })
        # Current period — better
        for i in range(7):
            entries.append({
                "date": (ref - timedelta(days=i)).isoformat(),
                "hours_worked": 8.0,
                "tasks_completed": 5,
                "tasks_escalated": 0,
                "consciousness_calls": 2,
            })
        agent_dir = self._setup_agent(tmp_path, entries)
        mgr = ReviewManager()
        review = mgr.generate_review(
            agent_dir, ReviewPeriod.WEEKLY, ref_date=ref
        )
        assert review.trend == "improving"

    def test_work_log_dict_format(self, tmp_path: Path) -> None:
        """work-log.json can be a dict with an 'entries' key."""
        ref = date(2026, 3, 29)
        agent_dir = tmp_path / "agent-1"
        journal = agent_dir / "journal"
        journal.mkdir(parents=True)
        log_data = {
            "entries": [
                {
                    "date": ref.isoformat(),
                    "hours_worked": 7.5,
                    "tasks_completed": 3,
                }
            ]
        }
        (journal / "work-log.json").write_text(
            json.dumps(log_data), encoding="utf-8"
        )
        mgr = ReviewManager()
        review = mgr.generate_review(
            agent_dir, ReviewPeriod.WEEKLY, ref_date=ref
        )
        assert review.metrics.tasks_completed == 3
        assert review.metrics.total_hours == 7.5
