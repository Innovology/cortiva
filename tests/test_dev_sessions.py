"""DevSessionManager: cap, detach, drain/reap, crash-safety."""

from __future__ import annotations

import asyncio

import pytest

from cortiva.core.dev_sessions import DevSessionManager, SessionResult


def _runner(agent, task, *, delay=0.0, boom=False):
    async def run() -> SessionResult:
        if delay:
            await asyncio.sleep(delay)
        if boom:
            raise RuntimeError("kaboom")
        return SessionResult(agent_id=agent, task_id=task, ok=True, outcome="did it")

    return run


@pytest.mark.asyncio
async def test_cap_enforced_per_agent():
    m = DevSessionManager(max_per_agent=2)
    assert m.launch("a", "t1", _runner("a", "t1", delay=0.2))
    assert m.launch("a", "t2", _runner("a", "t2", delay=0.2))
    # third is over the cap → refused
    assert m.launch("a", "t3", _runner("a", "t3", delay=0.2)) is False
    assert m.active_count("a") == 2
    # a different agent is independent
    assert m.launch("b", "t1", _runner("b", "t1", delay=0.2))
    await asyncio.gather(*[t for s in m._active.values() for t in s])


@pytest.mark.asyncio
async def test_drain_returns_results_and_frees_slot():
    m = DevSessionManager(max_per_agent=2)
    m.launch("a", "t1", _runner("a", "t1", delay=0.05))
    assert m.is_in_flight("a", "t1")
    await asyncio.sleep(0.15)
    done = m.drain_completed("a")
    assert len(done) == 1 and done[0].ok and done[0].outcome == "did it"
    assert m.drain_completed("a") == []  # drained once
    assert not m.is_in_flight("a", "t1")  # slot freed
    assert m.can_launch("a")


@pytest.mark.asyncio
async def test_crash_becomes_failed_result_not_explosion():
    m = DevSessionManager(max_per_agent=2)
    m.launch("a", "t1", _runner("a", "t1", boom=True))
    await asyncio.sleep(0.1)
    done = m.drain_completed("a")
    assert len(done) == 1 and done[0].ok is False and "kaboom" in done[0].error
    assert m.active_count("a") == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_inflight():
    m = DevSessionManager(max_per_agent=2)
    m.launch("a", "t1", _runner("a", "t1", delay=5.0))
    assert m.active_count("a") == 1
    await m.shutdown()
    assert m.total_active() == 0
