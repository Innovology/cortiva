"""
Multi-horizon planning — daily, weekly, monthly.

Agents plan at three horizons:

- **Monthly**: Objectives and themes for the month, informed by OKRs,
  performance history, and accumulated experience.
- **Weekly**: Milestones that advance the monthly objectives, informed
  by the previous week's outcomes and current blockers.
- **Daily**: Concrete tasks for today, derived from the weekly plan
  plus delegations, urgent messages, and carry-over from yesterday.

Each horizon cascades: monthly → weekly → daily.  Higher-level plans
persist across sleep cycles in ``agents/{id}/plans/``.  The memory
system feeds into planning at each level:

- Monthly: performance reviews, goal progress, high-importance memories
- Weekly: recent learnings, blockers, delegation outcomes
- Daily: familiarity signals, yesterday's reflection, pending items

Plans are regenerated on-demand when context changes (new delegation,
urgent message, blocked task, goal update).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortiva.planner")


class PlanHorizon(Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass
class HorizonPlan:
    """A plan at a specific time horizon."""

    horizon: PlanHorizon
    period: str
    """Period identifier, e.g. ``2026-04`` for monthly, ``2026-W14``
    for weekly, ``2026-04-06`` for daily."""

    content: str
    """Markdown plan text."""

    created_at: str = ""
    updated_at: str = ""
    revision: int = 0
    """How many times this plan has been revised within the period."""

    memory_context: str = ""
    """Summary of what memory signals informed this plan."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon.value,
            "period": self.period,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "memory_context": self.memory_context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HorizonPlan:
        return cls(
            horizon=PlanHorizon(data["horizon"]),
            period=data["period"],
            content=data.get("content", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            revision=data.get("revision", 0),
            memory_context=data.get("memory_context", ""),
        )


class PlanStore:
    """Persists multi-horizon plans for an agent.

    Plans are stored in ``agents/{id}/plans/`` as JSON files:
    ``monthly-2026-04.json``, ``weekly-2026-W14.json``.
    Daily plans remain in ``today/plan.md`` (existing convention).
    """

    def __init__(self, agent_dir: Path) -> None:
        self._plans_dir = agent_dir / "plans"

    def _path(self, horizon: PlanHorizon, period: str) -> Path:
        return self._plans_dir / f"{horizon.value}-{period}.json"

    def save(self, plan: HorizonPlan) -> None:
        self._plans_dir.mkdir(parents=True, exist_ok=True)
        self._path(plan.horizon, plan.period).write_text(
            json.dumps(plan.to_dict(), indent=2), encoding="utf-8",
        )

    def load(self, horizon: PlanHorizon, period: str) -> HorizonPlan | None:
        path = self._path(horizon, period)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return HorizonPlan.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            return None

    def current_monthly(self) -> HorizonPlan | None:
        return self.load(PlanHorizon.MONTHLY, _current_month())

    def current_weekly(self) -> HorizonPlan | None:
        return self.load(PlanHorizon.WEEKLY, _current_week())

    def previous_weekly(self) -> HorizonPlan | None:
        now = datetime.now(UTC)
        prev = now - timedelta(weeks=1)
        return self.load(PlanHorizon.WEEKLY, prev.strftime("%Y-W%V"))

    def list_plans(self, horizon: PlanHorizon | None = None) -> list[HorizonPlan]:
        """List all stored plans, optionally filtered by horizon."""
        if not self._plans_dir.exists():
            return []
        plans = []
        for path in sorted(self._plans_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                plan = HorizonPlan.from_dict(data)
                if horizon is None or plan.horizon == horizon:
                    plans.append(plan)
            except (json.JSONDecodeError, KeyError):
                continue
        return plans


# ---------------------------------------------------------------------------
# Memory-informed context builders
# ---------------------------------------------------------------------------


async def build_monthly_context(
    agent_id: str,
    memory: Any,
    *,
    goals_context: str = "",
    performance_context: str = "",
    previous_monthly: HorizonPlan | None = None,
) -> str:
    """Build planning context for monthly planning.

    Pulls from: performance reviews, goal progress, high-importance
    memories, and the previous month's plan outcomes.
    """
    sections: list[str] = []

    sections.append("## Monthly Planning Context\n")

    if goals_context:
        sections.append(f"### Current Goals\n{goals_context}\n")

    if performance_context:
        sections.append(f"### Recent Performance\n{performance_context}\n")

    if previous_monthly:
        sections.append(
            f"### Previous Month ({previous_monthly.period})\n"
            f"{previous_monthly.content[:500]}\n"
        )

    # High-importance memories (learnings, patterns)
    try:
        memories = await memory.recall(agent_id, limit=10, min_importance=7.0)
        if memories:
            mem_lines = [f"- {m.content[:100]}" for m in memories]
            sections.append(
                "### Key Learnings from Memory\n" + "\n".join(mem_lines) + "\n"
            )
    except Exception:
        pass

    # Shared org knowledge
    try:
        if hasattr(memory, "recall_shared"):
            shared = await memory.recall_shared(limit=5, min_importance=6.0)
        else:
            shared = await memory.recall("__org_shared__", limit=5, min_importance=6.0)
        if shared:
            shared_lines = [f"- {m.content[:100]}" for m in shared]
            sections.append(
                "### Org Knowledge\n" + "\n".join(shared_lines) + "\n"
            )
    except Exception:
        pass

    return "\n".join(sections)


async def build_weekly_context(
    agent_id: str,
    memory: Any,
    *,
    monthly_plan: HorizonPlan | None = None,
    previous_weekly: HorizonPlan | None = None,
    delegation_context: str = "",
) -> str:
    """Build planning context for weekly planning.

    Pulls from: monthly plan, previous week's outcomes, recent
    learnings, delegation queue, and blockers.
    """
    sections: list[str] = []

    sections.append("## Weekly Planning Context\n")

    if monthly_plan:
        sections.append(
            f"### Monthly Objectives ({monthly_plan.period})\n"
            f"{monthly_plan.content[:500]}\n"
        )

    if previous_weekly:
        sections.append(
            f"### Previous Week ({previous_weekly.period})\n"
            f"{previous_weekly.content[:400]}\n"
        )

    if delegation_context:
        sections.append(f"### Delegated Work\n{delegation_context}\n")

    # Recent learnings (last 7 days worth)
    try:
        memories = await memory.search(
            agent_id, "learned", limit=10, tags=["learning"],
        )
        if memories:
            learn_lines = [f"- {m.content[:100]}" for m in memories[:5]]
            sections.append(
                "### Recent Learnings\n" + "\n".join(learn_lines) + "\n"
            )
    except Exception:
        pass

    return "\n".join(sections)


async def build_daily_context(
    agent_id: str,
    memory: Any,
    *,
    weekly_plan: HorizonPlan | None = None,
    yesterday_reflection: str = "",
    delegation_context: str = "",
    familiarity_context: str = "",
) -> str:
    """Build planning context for daily planning.

    Pulls from: weekly plan, yesterday's reflection, delegations,
    familiarity signals, and carry-over items.
    """
    sections: list[str] = []

    sections.append("## Daily Planning Context\n")

    if weekly_plan:
        sections.append(
            f"### This Week's Plan ({weekly_plan.period})\n"
            f"{weekly_plan.content[:400]}\n"
            "Your daily tasks should advance these weekly milestones.\n"
        )

    if yesterday_reflection:
        sections.append(
            f"### Yesterday's Reflection\n{yesterday_reflection[:300]}\n"
        )

    if delegation_context:
        sections.append(f"### Delegated Tasks\n{delegation_context}\n")

    if familiarity_context:
        sections.append(f"### Familiarity\n{familiarity_context}\n")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Planning prompts
# ---------------------------------------------------------------------------

MONTHLY_PROMPT = """\
Plan your objectives for the month. Consider your goals, recent \
performance, and accumulated experience. Write 3-5 monthly objectives \
as a structured list:

- **Objective**: What you aim to achieve
  - Key milestone or deliverable
  - How you'll know it's done

Focus on outcomes, not tasks. These will guide your weekly planning.
"""

WEEKLY_PROMPT = """\
Plan your milestones for this week. Your weekly plan should advance \
your monthly objectives. Consider delegated work and recent learnings. \
Write 3-7 milestones as a structured checklist:

- [ ] **[HIGH]** Milestone that advances a monthly objective
- [ ] Milestone or deliverable for this week

These will guide your daily task planning each morning.
"""

DAILY_PROMPT = """\
Create your plan for today as a structured checklist. Your daily \
tasks should advance this week's milestones. Include delegated \
tasks from your manager at the top.

- [ ] **[CRITICAL]** Task description (for critical tasks)
- [ ] **[HIGH]** Task description (for high-priority tasks)
- [ ] Task description (for normal tasks)

IMPORTANT: Delegated tasks take priority. Carry over incomplete \
items from yesterday if they're still relevant.
"""


# ---------------------------------------------------------------------------
# Planning orchestrator
# ---------------------------------------------------------------------------


class Planner:
    """Orchestrates multi-horizon planning for an agent.

    Called by the Fabric during the wake phase.  Determines which
    planning horizons need refreshing and builds memory-informed
    context for each.
    """

    def __init__(self, agent_dir: Path) -> None:
        self.store = PlanStore(agent_dir)

    def needs_monthly_plan(self) -> bool:
        """True if no monthly plan exists for the current month."""
        return self.store.current_monthly() is None

    def needs_weekly_plan(self) -> bool:
        """True if no weekly plan exists for the current week."""
        return self.store.current_weekly() is None

    def save_monthly(self, content: str, memory_context: str = "") -> HorizonPlan:
        now = datetime.now(UTC).isoformat()
        existing = self.store.current_monthly()
        plan = HorizonPlan(
            horizon=PlanHorizon.MONTHLY,
            period=_current_month(),
            content=content,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            revision=(existing.revision + 1) if existing else 0,
            memory_context=memory_context,
        )
        self.store.save(plan)
        return plan

    def save_weekly(self, content: str, memory_context: str = "") -> HorizonPlan:
        now = datetime.now(UTC).isoformat()
        existing = self.store.current_weekly()
        plan = HorizonPlan(
            horizon=PlanHorizon.WEEKLY,
            period=_current_week(),
            content=content,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            revision=(existing.revision + 1) if existing else 0,
            memory_context=memory_context,
        )
        self.store.save(plan)
        return plan

    def cascade_context(self) -> str:
        """Build a cascade context from monthly → weekly for daily planning."""
        parts: list[str] = []
        monthly = self.store.current_monthly()
        if monthly:
            parts.append(
                f"## Monthly Objectives ({monthly.period})\n\n"
                f"{monthly.content}\n"
            )
        weekly = self.store.current_weekly()
        if weekly:
            parts.append(
                f"## This Week's Plan ({weekly.period})\n\n"
                f"{weekly.content}\n"
            )
        return "\n---\n\n".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _current_week() -> str:
    return datetime.now(UTC).strftime("%Y-W%V")
