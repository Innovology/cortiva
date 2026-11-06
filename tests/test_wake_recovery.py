"""Planning is serialised per node, and a wake whose planning fails recovers.

Regression for 2026-11-05 on Mini-2: cto and yuki each timed out on their
daily-plan call (195s) while the CEO was mid-cycle on the same local model,
and were stranded in PLANNING — re-waking was a no-op and nothing retried.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core.agent import AgentState
from cortiva.core.fabric import Fabric


class _Model:
    """Mock local model: planning can be made to time out, and calls can overlap."""

    def __init__(self) -> None:
        self.fail_plans = 0
        self.inflight = 0
        self.peak = 0

    async def think(self, agent_id, context, prompt, **kwargs):
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0.01)
            if self.fail_plans and "plan" in str(
                (kwargs.get("metadata") or {}).get("call_type", "")
            ):
                self.fail_plans -= 1
                raise TimeoutError
            return ConsciousResponse(
                content="- [ ] **[HIGH]** Do the work\n",
                tokens_in=10,
                tokens_out=5,
                model="mock",
            )
        finally:
            self.inflight -= 1

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(content="ok", tokens_in=1, tokens_out=1, model="mock")


def _fabric(tmp_path: Path, model: _Model) -> Fabric:
    f = Fabric(agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=model)
    f._WAKE_RETRY_BASE_S = 0.01  # type: ignore[misc]
    return f


@pytest.mark.asyncio
async def test_failed_planning_returns_agent_to_sleep(tmp_path: Path) -> None:
    model = _Model()
    model.fail_plans = 99
    fabric = _fabric(tmp_path, model)
    fabric.register_agent("cto")
    fabric._WAKE_RETRY_MAX = 0  # type: ignore[misc]

    with pytest.raises(TimeoutError):
        await fabric.wake("cto")

    assert fabric.get_agent("cto").state is AgentState.SLEEPING


@pytest.mark.asyncio
async def test_failed_wake_is_retried_until_it_plans(tmp_path: Path) -> None:
    model = _Model()
    model.fail_plans = 1  # first planning call times out, the retry succeeds
    fabric = _fabric(tmp_path, model)
    fabric.register_agent("yuki")

    with pytest.raises(TimeoutError):
        await fabric.wake("yuki")
    for _ in range(100):
        if fabric.get_agent("yuki").state is AgentState.EXECUTING:
            break
        await asyncio.sleep(0.01)

    assert fabric.get_agent("yuki").state is AgentState.EXECUTING
    assert "yuki" not in fabric._wake_retries


@pytest.mark.asyncio
async def test_retries_give_up_after_max(tmp_path: Path) -> None:
    model = _Model()
    model.fail_plans = 99
    fabric = _fabric(tmp_path, model)
    fabric.register_agent("cto")

    with pytest.raises(TimeoutError):
        await fabric.wake("cto")
    for _ in range(200):
        if not fabric._wake_retry_tasks:
            break
        await asyncio.sleep(0.01)

    assert not fabric._wake_retry_tasks
    assert "cto" not in fabric._wake_retries
    assert fabric.get_agent("cto").state is AgentState.SLEEPING


@pytest.mark.asyncio
async def test_planning_is_serialised_across_agents(tmp_path: Path) -> None:
    model = _Model()
    fabric = _fabric(tmp_path, model)
    for aid in ("ceo", "cto", "yuki"):
        fabric.register_agent(aid)

    await asyncio.gather(*(fabric.wake(a) for a in ("ceo", "cto", "yuki")))

    assert model.peak == 1
    assert all(fabric.get_agent(a).state is AgentState.EXECUTING for a in ("ceo", "cto", "yuki"))


@pytest.mark.asyncio
async def test_waking_an_awake_agent_skips_the_gate(tmp_path: Path) -> None:
    model = _Model()
    fabric = _fabric(tmp_path, model)
    fabric.register_agent("ceo")
    await fabric.wake("ceo")

    async with fabric._planning_gate():  # gate held elsewhere
        agent = await asyncio.wait_for(fabric.wake("ceo", override_minutes=45), timeout=1)

    assert agent.state is AgentState.EXECUTING


class _DownModel(_Model):
    """A model that is DOWN (breaker open) rather than slow."""

    async def think(self, agent_id, context, prompt, **kwargs):
        from cortiva.adapters.consciousness.openai_compat import ModelUnavailableError

        if self.fail_plans and "plan" in str((kwargs.get("metadata") or {}).get("call_type", "")):
            self.fail_plans -= 1
            raise ModelUnavailableError("local model circuit breaker open")
        return await super().think(agent_id, context, prompt, **kwargs)


@pytest.mark.asyncio
async def test_model_down_failure_does_not_consume_an_attempt(tmp_path: Path) -> None:
    # Mini-2 2026-11-05 19:55: five agents burned all three attempts in 70s
    # against a model that was being relaunched.
    model = _DownModel()
    model.fail_plans = 5  # more than _WAKE_RETRY_MAX, fewer than the model-down cap
    fabric = _fabric(tmp_path, model)
    fabric._WAKE_RETRY_MODEL_DOWN_S = 0.01  # type: ignore[misc]
    fabric.register_agent("ceo")

    with pytest.raises(Exception, match="breaker"):
        await fabric.wake("ceo")
    for _ in range(300):
        if fabric.get_agent("ceo").state is AgentState.EXECUTING:
            break
        await asyncio.sleep(0.01)

    assert fabric.get_agent("ceo").state is AgentState.EXECUTING
    assert "ceo" not in fabric._wake_retries
    assert "ceo" not in fabric._model_down_retries


@pytest.mark.asyncio
async def test_stop_cancels_pending_wake_retries_and_refuses_new_wakes(tmp_path: Path) -> None:
    # The fabric was still waking agents two minutes after SIGTERM.
    model = _Model()
    model.fail_plans = 99
    fabric = _fabric(tmp_path, model)
    fabric._WAKE_RETRY_BASE_S = 60.0  # type: ignore[misc]  # far in the future
    fabric.register_agent("ceo")
    with pytest.raises(TimeoutError):
        await fabric.wake("ceo")
    assert fabric._wake_retry_tasks

    await fabric.stop()
    await asyncio.sleep(0)

    assert not fabric._wake_retry_tasks
    with pytest.raises(RuntimeError, match="stopping"):
        await fabric.wake("ceo")
    await fabric.stop()  # idempotent


@pytest.mark.asyncio
async def test_stop_forces_stragglers_to_sleep_at_the_drain_deadline(tmp_path: Path) -> None:
    model = _Model()
    fabric = _fabric(tmp_path, model)
    for aid in ("ceo", "cto"):
        fabric.register_agent(aid)
        await fabric.wake(aid)

    async def _hang(*a, **k):
        await asyncio.sleep(3600)

    fabric._session_reflection = _hang  # type: ignore[method-assign]  # clock-off never returns

    await asyncio.wait_for(fabric.stop(drain_timeout_s=0.2), timeout=5)

    assert all(fabric.get_agent(a).state is AgentState.SLEEPING for a in ("ceo", "cto"))
    assert not fabric.timesheet_manager.get("ceo").is_clocked_in


@pytest.mark.asyncio
async def test_failed_replan_leaves_the_agent_executing(tmp_path: Path) -> None:
    # 2026-11-05 23:32 Mini-2: the model server was relaunched mid-replan,
    # think() raised, and all five working agents sat in REPLANNING for half
    # an hour — the rota's wakes no-op on an agent that isn't asleep.
    model = _Model()
    f = _fabric(tmp_path, model)
    agent = f.register_agent("cto")
    await f.wake("cto")
    assert agent.state == AgentState.EXECUTING

    model.fail_plans = 1  # the reassessment call raises
    await f._replan(agent, [])
    assert agent.state == AgentState.EXECUTING, "a failed replan must not strand the agent"

    await f._replan(agent, [])  # the next cycle reassesses normally
    assert agent.state == AgentState.EXECUTING
