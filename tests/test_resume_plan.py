"""A wake is a clock event, not new information: today's standing plan is
resumed unless something inbound changed since it was made, and a cycle
reassesses only on inbound change (2026-11-06: 29 full re-plans by 11:00)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from cortiva.core.agent import AgentState
from tests.test_wake_recovery import _fabric, _Model


@pytest.mark.asyncio
async def test_a_wake_resumes_todays_plan_when_nothing_inbound_changed(tmp_path: Path) -> None:
    model = _Model()
    f = _fabric(tmp_path, model)
    agent = f.register_agent("cto")
    calls = []
    orig = model.think

    async def _think(agent_id, context, prompt, **kw):
        calls.append((kw.get("metadata") or {}).get("call_type"))
        return await orig(agent_id, context, prompt, **kw)

    model.think = _think
    await f.wake("cto")
    assert calls.count("plan") == 1 and agent.state == AgentState.EXECUTING
    pending_before = [t.description for t in agent.task_queue.tasks if t.status == "pending"]
    assert pending_before

    await f.sleep("cto")
    assert agent.task_queue is None
    await f.wake("cto")
    assert calls.count("plan") == 1, "second wake must not re-plan"
    assert agent.state == AgentState.EXECUTING
    assert [
        t.description for t in agent.task_queue.tasks if t.status == "pending"
    ] == pending_before


@pytest.mark.asyncio
async def test_inbound_mail_or_a_directive_makes_the_next_wake_adjust(tmp_path: Path) -> None:
    model = _Model()
    f = _fabric(tmp_path, model)
    agent = f.register_agent("cto")
    calls = []
    orig = model.think

    async def _think(agent_id, context, prompt, **kw):
        calls.append((kw.get("metadata") or {}).get("call_type"))
        return await orig(agent_id, context, prompt, **kw)

    model.think = _think
    await f.wake("cto")
    await f.sleep("cto")
    inbox = agent.directory / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "m.json").write_text(
        json.dumps({"from": "maren@x", "subject": "Please", "text": "do X"})
    )
    await f.wake("cto")
    assert calls.count("plan") == 1 and calls.count("replan") == 1  # adjusted, not rebuilt

    await f.sleep("cto")
    await f.wake("cto")
    assert calls.count("replan") == 1  # the mail was read during that adjustment; nothing new since

    await f.sleep("cto")
    time.sleep(0.01)
    (agent.directory / "directives.json").write_text("[]")
    await f.wake("cto")
    assert calls.count("plan") == 1 and calls.count("replan") == 2


@pytest.mark.asyncio
async def test_the_standing_plan_carries_across_days(tmp_path: Path) -> None:
    # A person wakes with the list they had. A new day resets the day's
    # bookkeeping, not the plan.
    model = _Model()
    f = _fabric(tmp_path, model)
    agent = f.register_agent("cto")
    await f.wake("cto")
    await f.sleep("cto")
    today = agent.directory / "today"
    (today / "timesheet.json").write_text("{}")
    old = time.time() - 36 * 3600
    for p in today.iterdir():
        os.utime(p, (old, old))
    f._last_plan_ts.pop("cto", None)
    assert agent.reset_today() is True
    assert (today / "plan.md").exists() and not (today / "timesheet.json").exists()
    assert f._resume_plan(agent) == 1


@pytest.mark.asyncio
async def test_a_replan_on_inbound_change_is_a_small_adjustment(tmp_path: Path) -> None:
    model = _Model()
    f = _fabric(tmp_path, model)
    agent = f.register_agent("cto")
    seen: list[tuple[str, str, str]] = []
    orig = model.think

    async def _think(agent_id, context, prompt, **kw):
        seen.append(((kw.get("metadata") or {}).get("call_type"), context, prompt))
        return await orig(agent_id, context, prompt, **kw)

    model.think = _think
    await f.wake("cto")
    plan_ctx = next(c for t, c, _ in seen if t == "plan")
    inbox = agent.directory / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "m.json").write_text(
        json.dumps(
            {"from": "maren@x", "subject": "Lead form", "text": "Please start the lead form today."}
        )
    )
    await f._replan(agent, [])
    t, ctx, prompt = seen[-1]
    assert t == "replan"
    assert "Your current checklist" in ctx and "Lead form" in ctx
    assert "Adjust the checklist for it and nothing else" in prompt
    assert "Company directory" not in ctx and "What actually moved" not in ctx
    assert len(ctx) < len(plan_ctx) / 2, (len(ctx), len(plan_ctx))


def test_the_wind_down_floor(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    f = _fabric(tmp_path, _Model())
    agent = f.register_agent("cto")
    agent.last_wake = datetime.now(UTC) - timedelta(minutes=5)
    assert f._awake_long_enough_to_wind_down(agent) is False
    agent.last_wake = datetime.now(UTC) - timedelta(minutes=25)
    assert f._awake_long_enough_to_wind_down(agent) is True


@pytest.mark.asyncio
async def test_a_cycle_reassesses_only_on_inbound_change(tmp_path: Path) -> None:
    model = _Model()
    f = _fabric(tmp_path, model)
    agent = f.register_agent("cto")
    await f.wake("cto")

    class _Eager:
        async def dispatch_should_reassess(self, agent_id, messages):
            return True

    f.plugin_manager.dispatch_should_reassess = _Eager().dispatch_should_reassess  # type: ignore[assignment]
    assert await f._should_replan(agent, []) is False
    time.sleep(0.01)
    (agent.directory / "github_feedback.json").write_text("[]")
    assert await f._should_replan(agent, []) is True


def test_reset_today_keeps_todays_files_and_clears_stale_ones(tmp_path: Path) -> None:
    f = _fabric(tmp_path, _Model())
    agent = f.register_agent("cto")
    today = agent.directory / "today"
    today.mkdir(parents=True, exist_ok=True)
    (today / "plan.md").write_text("- [ ] x")
    (today / "timesheet.json").write_text("{}")
    assert agent.reset_today() is False
    assert (today / "plan.md").exists() and (today / "timesheet.json").exists()
    old = time.time() - 30 * 3600
    for p in today.iterdir():
        os.utime(p, (old, old))
    assert agent.reset_today() is True
    # The plan carries across days; the day's bookkeeping does not.
    assert [p.name for p in today.iterdir()] == ["plan.md"]


@pytest.mark.asyncio
async def test_a_wake_with_new_input_adjusts_the_standing_plan_not_rebuilds_it(
    tmp_path: Path,
) -> None:
    # 12:02 2026-11-06: held mail made the CEO's wake fall through to the full
    # daily plan — 92k characters of context. A standing plan plus new input
    # is an adjustment; only an agent with no plan at all builds one.
    model = _Model()
    f = _fabric(tmp_path, model)
    agent = f.register_agent("cto")
    seen = []
    orig = model.think

    async def _think(agent_id, context, prompt, **kw):
        md = kw.get("metadata") or {}
        seen.append((md.get("call_type"), bool(md.get("light")), len(context)))
        return await orig(agent_id, context, prompt, **kw)

    model.think = _think
    await f.wake("cto")  # first wake of its life: a full plan
    assert [t for t, _, _ in seen if t == "plan"] == ["plan"]
    await f.sleep("cto")
    inbox = agent.directory / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "m.json").write_text(
        json.dumps({"from": "maren@x", "subject": "Lead form", "text": "start it"})
    )
    await f.wake("cto")
    assert seen[-1][0] == "replan" and seen[-1][1] is True, seen
    assert [t for t, _, _ in seen if t == "plan"] == ["plan"], "no second full plan"
    assert agent.state == AgentState.EXECUTING
    assert any(t.status == "pending" for t in agent.task_queue.tasks)


def test_an_adjustment_cannot_resurrect_a_done_or_struck_task(tmp_path: Path) -> None:
    # "Land PR #48" completed twice in twelve minutes (2026-11-06 12:09,
    # 12:21): each adjustment re-listed it as open.
    f = _fabric(tmp_path, _Model())
    agent = f.register_agent("cto")
    agent.set_plan(
        "- [ ] Land PR #48\n- [ ] Chase COO *(struck: Marcus is not currently working)*\n- [ ] Build the lead form\n"
    )
    q = agent.task_queue
    q.tasks[0].status = "done"
    q.tasks[1].status = "skipped"
    agent.update_plan(
        "- [ ] **[HIGH]** Land PR #48\n- [ ] Chase COO\n- [ ] Build the lead form\n- [ ] Write the pipeline screen\n"
    )
    statuses = {t.description.split(" *(")[0]: t.status for t in agent.task_queue.tasks}
    assert statuses["Land PR #48"] == "done"
    assert statuses["Chase COO"] == "skipped"
    assert statuses["Build the lead form"] == "pending"
    assert statuses["Write the pipeline screen"] == "pending"
    f._write_plan(agent)
    plan = (agent.directory / "today" / "plan.md").read_text()
    assert "- [x] " in plan and "Land PR #48" in plan
    assert f._resume_plan(agent) == 2  # only the genuinely open items are pending
