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
async def test_inbound_mail_or_a_directive_makes_the_next_wake_replan(tmp_path: Path) -> None:
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
    assert calls.count("plan") == 2

    await f.sleep("cto")
    await f.wake("cto")
    assert calls.count("plan") == 2  # the mail was read during that plan; nothing new since

    await f.sleep("cto")
    time.sleep(0.01)
    (agent.directory / "directives.json").write_text("[]")
    await f.wake("cto")
    assert calls.count("plan") == 3


@pytest.mark.asyncio
async def test_yesterdays_plan_is_not_resumed(tmp_path: Path) -> None:
    model = _Model()
    f = _fabric(tmp_path, model)
    agent = f.register_agent("cto")
    await f.wake("cto")
    await f.sleep("cto")
    plan = agent.identity_path("plan")
    old = time.time() - 36 * 3600
    os.utime(plan, (old, old))
    f._last_plan_ts.pop("cto", None)
    assert f._resume_plan(agent) is None


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
    assert not any(today.iterdir())
