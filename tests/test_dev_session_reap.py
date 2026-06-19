"""Fabric reaping of detached dev sessions onto the task queue."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.core.agent import Task, TaskQueue
from cortiva.core.dev_sessions import SessionResult
from cortiva.core.fabric import Fabric


def _fabric(tmp_path: Path) -> Fabric:
    f = Fabric(agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=MagicMock())
    f._dev_sessions_enabled = True
    return f


def _agent(*tasks: Task):
    return SimpleNamespace(
        id="a1",
        task_queue=TaskQueue(tasks=list(tasks)),
        tasks_completed_today=0,
        tasks_escalated_today=0,
    )


@pytest.mark.asyncio
async def test_reap_marks_success_done(tmp_path: Path):
    f = _fabric(tmp_path)
    t = Task(id="t1", description="fix CI", status="in_progress")
    agent = _agent(t)
    f.dev_sessions._completed["a1"].append(
        SessionResult(agent_id="a1", task_id="t1", ok=True, outcome="fixed it", critique="LGTM")
    )
    await f._reap_dev_sessions(agent)
    assert t.status == "done" and t.outcome == "fixed it"
    assert agent.tasks_completed_today == 1
    assert f.dev_sessions.drain_completed("a1") == []  # consumed


@pytest.mark.asyncio
async def test_reap_marks_failure_exception(tmp_path: Path):
    f = _fabric(tmp_path)
    t = Task(id="t1", description="fix CI", status="in_progress")
    agent = _agent(t)
    f.dev_sessions._completed["a1"].append(
        SessionResult(agent_id="a1", task_id="t1", ok=False, error="claude timed out")
    )
    await f._reap_dev_sessions(agent)
    assert t.status == "exception" and "timed out" in t.error
    assert t in agent.task_queue.exceptions
    assert agent.tasks_escalated_today == 1


@pytest.mark.asyncio
async def test_reap_noop_when_nothing_completed(tmp_path: Path):
    f = _fabric(tmp_path)
    t = Task(id="t1", description="x", status="in_progress")
    agent = _agent(t)
    await f._reap_dev_sessions(agent)
    assert t.status == "in_progress"  # untouched
