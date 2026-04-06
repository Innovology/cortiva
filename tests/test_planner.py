"""Tests for multi-horizon planning."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cortiva.core.planner import (
    HorizonPlan,
    PlanHorizon,
    PlanStore,
    Planner,
    build_daily_context,
    build_monthly_context,
    build_weekly_context,
)


class TestHorizonPlan:
    def test_roundtrip(self) -> None:
        plan = HorizonPlan(
            horizon=PlanHorizon.MONTHLY,
            period="2026-04",
            content="## Objectives\n- Ship v1.0",
            created_at="2026-04-01T00:00:00",
            revision=2,
        )
        d = plan.to_dict()
        restored = HorizonPlan.from_dict(d)
        assert restored.horizon == PlanHorizon.MONTHLY
        assert restored.period == "2026-04"
        assert restored.revision == 2


class TestPlanStore:
    def test_save_and_load(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-1")
        plan = HorizonPlan(
            horizon=PlanHorizon.WEEKLY,
            period="2026-W14",
            content="## Milestones\n- Finish auth",
        )
        store.save(plan)
        loaded = store.load(PlanHorizon.WEEKLY, "2026-W14")
        assert loaded is not None
        assert loaded.content == plan.content

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-1")
        assert store.load(PlanHorizon.MONTHLY, "2099-01") is None

    def test_list_plans(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-1")
        store.save(HorizonPlan(horizon=PlanHorizon.MONTHLY, period="2026-03", content="March"))
        store.save(HorizonPlan(horizon=PlanHorizon.MONTHLY, period="2026-04", content="April"))
        store.save(HorizonPlan(horizon=PlanHorizon.WEEKLY, period="2026-W14", content="Week 14"))

        all_plans = store.list_plans()
        assert len(all_plans) == 3

        monthly_only = store.list_plans(PlanHorizon.MONTHLY)
        assert len(monthly_only) == 2


class TestPlanner:
    def test_needs_monthly_plan(self, tmp_path: Path) -> None:
        planner = Planner(tmp_path / "agent-1")
        assert planner.needs_monthly_plan() is True

    def test_needs_monthly_plan_exists(self, tmp_path: Path) -> None:
        planner = Planner(tmp_path / "agent-1")
        planner.save_monthly("## Plan for the month")
        assert planner.needs_monthly_plan() is False

    def test_needs_weekly_plan(self, tmp_path: Path) -> None:
        planner = Planner(tmp_path / "agent-1")
        assert planner.needs_weekly_plan() is True

    def test_save_monthly_increments_revision(self, tmp_path: Path) -> None:
        planner = Planner(tmp_path / "agent-1")
        p1 = planner.save_monthly("v1")
        assert p1.revision == 0
        p2 = planner.save_monthly("v2")
        assert p2.revision == 1

    def test_cascade_context(self, tmp_path: Path) -> None:
        planner = Planner(tmp_path / "agent-1")
        planner.save_monthly("Ship v1.0")
        planner.save_weekly("Finish auth module")

        ctx = planner.cascade_context()
        assert "Monthly Objectives" in ctx
        assert "Ship v1.0" in ctx
        assert "This Week" in ctx
        assert "Finish auth" in ctx

    def test_cascade_context_empty(self, tmp_path: Path) -> None:
        planner = Planner(tmp_path / "agent-1")
        assert planner.cascade_context() == ""


class TestContextBuilders:
    @pytest.mark.asyncio
    async def test_monthly_context(self) -> None:
        memory = AsyncMock()
        memory.recall.return_value = []

        ctx = await build_monthly_context(
            "agent-1", memory,
            goals_context="Ship v1.0 by end of Q2",
            performance_context="Last month: 85% task completion",
        )
        assert "Monthly Planning" in ctx
        assert "Ship v1.0" in ctx
        assert "85%" in ctx

    @pytest.mark.asyncio
    async def test_weekly_context(self) -> None:
        memory = AsyncMock()
        memory.search.return_value = []

        monthly = HorizonPlan(
            horizon=PlanHorizon.MONTHLY,
            period="2026-04",
            content="Ship v1.0",
        )
        ctx = await build_weekly_context(
            "agent-1", memory,
            monthly_plan=monthly,
            delegation_context="Fix auth bug",
        )
        assert "Weekly Planning" in ctx
        assert "Ship v1.0" in ctx
        assert "Fix auth" in ctx

    @pytest.mark.asyncio
    async def test_daily_context(self) -> None:
        memory = AsyncMock()

        weekly = HorizonPlan(
            horizon=PlanHorizon.WEEKLY,
            period="2026-W14",
            content="Finish auth module",
        )
        ctx = await build_daily_context(
            "agent-1", memory,
            weekly_plan=weekly,
            yesterday_reflection="Completed 3 tasks, 2 carry-over.",
        )
        assert "Daily Planning" in ctx
        assert "Finish auth" in ctx
        assert "advance these weekly milestones" in ctx
        assert "Completed 3 tasks" in ctx
