"""Extended tests for planner.py — build_monthly_context with shared org
knowledge, build_weekly_context with previous weekly plan,
PlanStore.previous_weekly(), current_monthly(), current_weekly()."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cortiva.core.planner import (
    HorizonPlan,
    PlanHorizon,
    PlanStore,
    Planner,
    build_monthly_context,
    build_weekly_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeMemory:
    content: str
    importance: float = 7.0


def _make_memory_with_shared() -> AsyncMock:
    """Create an AsyncMock memory adapter with recall_shared support."""
    memory = AsyncMock()
    memory.recall.return_value = [
        FakeMemory(content="Learned: always add tests before merging"),
        FakeMemory(content="Pattern: retry transient errors 3 times"),
    ]
    memory.recall_shared = AsyncMock(return_value=[
        FakeMemory(content="Org policy: deploy only on Tuesdays"),
        FakeMemory(content="Shared: use structured logging everywhere"),
    ])
    return memory


def _make_memory_without_shared() -> AsyncMock:
    """Memory without recall_shared (uses fallback to recall __org_shared__)."""
    memory = AsyncMock()
    memory.recall.return_value = [
        FakeMemory(content="Learned something useful"),
    ]
    # No recall_shared attribute
    memory.spec = []
    del memory.recall_shared
    return memory


# ---------------------------------------------------------------------------
# build_monthly_context with shared org knowledge
# ---------------------------------------------------------------------------


class TestBuildMonthlyContextShared:
    @pytest.mark.asyncio
    async def test_includes_shared_org_knowledge(self) -> None:
        memory = _make_memory_with_shared()

        ctx = await build_monthly_context(
            "agent-1", memory,
            goals_context="Ship v2.0",
            performance_context="90% completion rate",
        )

        assert "Monthly Planning" in ctx
        assert "Ship v2.0" in ctx
        assert "90%" in ctx
        assert "Org Knowledge" in ctx
        assert "deploy only on Tuesdays" in ctx

    @pytest.mark.asyncio
    async def test_includes_key_learnings(self) -> None:
        memory = _make_memory_with_shared()

        ctx = await build_monthly_context("agent-1", memory)
        assert "Key Learnings" in ctx
        assert "always add tests" in ctx

    @pytest.mark.asyncio
    async def test_includes_previous_monthly_plan(self) -> None:
        memory = _make_memory_with_shared()
        prev = HorizonPlan(
            horizon=PlanHorizon.MONTHLY,
            period="2026-02",
            content="## Feb objectives\n- Shipped v1.5\n- Improved test coverage",
        )

        ctx = await build_monthly_context(
            "agent-1", memory, previous_monthly=prev,
        )
        assert "Previous Month" in ctx
        assert "2026-02" in ctx
        assert "Shipped v1.5" in ctx

    @pytest.mark.asyncio
    async def test_fallback_to_recall_org_shared(self) -> None:
        """When recall_shared is absent, falls back to recall(__org_shared__)."""
        memory = _make_memory_without_shared()

        # The fallback calls memory.recall("__org_shared__", ...)
        ctx = await build_monthly_context("agent-1", memory)
        # Should not crash; recall is called for both agent and __org_shared__
        assert "Monthly Planning" in ctx

    @pytest.mark.asyncio
    async def test_memory_error_is_swallowed(self) -> None:
        memory = AsyncMock()
        memory.recall.side_effect = RuntimeError("memory down")

        ctx = await build_monthly_context("agent-1", memory)
        # Should still return a valid context (errors are swallowed)
        assert "Monthly Planning" in ctx


# ---------------------------------------------------------------------------
# build_weekly_context with previous weekly plan
# ---------------------------------------------------------------------------


class TestBuildWeeklyContextExtended:
    @pytest.mark.asyncio
    async def test_includes_previous_weekly(self) -> None:
        memory = AsyncMock()
        memory.search.return_value = []

        prev_weekly = HorizonPlan(
            horizon=PlanHorizon.WEEKLY,
            period="2026-W12",
            content="- [x] Finished auth\n- [ ] Started billing",
        )

        ctx = await build_weekly_context(
            "agent-1", memory,
            previous_weekly=prev_weekly,
        )
        assert "Previous Week" in ctx
        assert "2026-W12" in ctx
        assert "Finished auth" in ctx

    @pytest.mark.asyncio
    async def test_includes_recent_learnings(self) -> None:
        memory = AsyncMock()
        memory.search.return_value = [
            FakeMemory(content="Learned: caching reduces latency by 40%"),
        ]

        ctx = await build_weekly_context("agent-1", memory)
        assert "Recent Learnings" in ctx
        assert "caching reduces latency" in ctx

    @pytest.mark.asyncio
    async def test_search_error_is_swallowed(self) -> None:
        memory = AsyncMock()
        memory.search.side_effect = RuntimeError("search broken")

        ctx = await build_weekly_context("agent-1", memory)
        assert "Weekly Planning" in ctx


# ---------------------------------------------------------------------------
# PlanStore: previous_weekly, current_monthly, current_weekly
# ---------------------------------------------------------------------------


class TestPlanStoreExtended:
    def test_current_monthly_returns_stored_plan(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-1")
        period = datetime.now(UTC).strftime("%Y-%m")
        plan = HorizonPlan(
            horizon=PlanHorizon.MONTHLY,
            period=period,
            content="Monthly objectives here",
        )
        store.save(plan)
        loaded = store.current_monthly()
        assert loaded is not None
        assert loaded.content == "Monthly objectives here"
        assert loaded.period == period

    def test_current_monthly_returns_none_when_missing(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-1")
        assert store.current_monthly() is None

    def test_current_weekly_returns_stored_plan(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-1")
        period = datetime.now(UTC).strftime("%Y-W%V")
        plan = HorizonPlan(
            horizon=PlanHorizon.WEEKLY,
            period=period,
            content="Weekly milestones here",
        )
        store.save(plan)
        loaded = store.current_weekly()
        assert loaded is not None
        assert loaded.content == "Weekly milestones here"

    def test_current_weekly_returns_none_when_missing(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-1")
        assert store.current_weekly() is None

    def test_previous_weekly_returns_last_week(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-1")
        now = datetime.now(UTC)
        prev = now - timedelta(weeks=1)
        prev_period = prev.strftime("%Y-W%V")

        plan = HorizonPlan(
            horizon=PlanHorizon.WEEKLY,
            period=prev_period,
            content="Last week stuff",
        )
        store.save(plan)
        loaded = store.previous_weekly()
        assert loaded is not None
        assert loaded.content == "Last week stuff"
        assert loaded.period == prev_period

    def test_previous_weekly_returns_none_when_missing(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-1")
        assert store.previous_weekly() is None


# ---------------------------------------------------------------------------
# Planner: save_weekly increments revision
# ---------------------------------------------------------------------------


class TestPlannerWeeklyRevision:
    def test_save_weekly_increments_revision(self, tmp_path: Path) -> None:
        planner = Planner(tmp_path / "agent-1")
        p1 = planner.save_weekly("v1")
        assert p1.revision == 0
        p2 = planner.save_weekly("v2")
        assert p2.revision == 1
        p3 = planner.save_weekly("v3")
        assert p3.revision == 2

    def test_needs_weekly_after_save(self, tmp_path: Path) -> None:
        planner = Planner(tmp_path / "agent-1")
        assert planner.needs_weekly_plan() is True
        planner.save_weekly("Weekly plan content")
        assert planner.needs_weekly_plan() is False


class TestPlanStoreListPlansEmpty:
    def test_list_plans_no_dir(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-nonexistent")
        assert store.list_plans() == []

    def test_list_plans_with_corrupt_file(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "agent-1")
        plans_dir = tmp_path / "agent-1" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "monthly-2026-01.json").write_text("broken json{", encoding="utf-8")
        # Should skip corrupt files without crashing
        result = store.list_plans()
        assert result == []
