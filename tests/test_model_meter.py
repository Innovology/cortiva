"""The model meter: every consciousness call is counted per agent per day
and the count is put in front of the agent next to a soft budget.

"There should never be an event in the LLM that an agent needs to brain
dump everything… hundreds of small asks" — the meter makes the asks
visible, to the agent and over IPC, without blocking any of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core import model_meter as mm
from cortiva.core.fabric import Fabric


class _Model:
    def __init__(self) -> None:
        self.calls = 0

    async def think(self, agent_id, context, prompt, **kwargs):
        self.calls += 1
        return ConsciousResponse(content="- [ ] x", tokens_in=1, tokens_out=1, model="m")

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(content="ok", tokens_in=1, tokens_out=1, model="m")


def test_bump_counts_by_type_and_rolls_over_at_midnight(tmp_path: Path) -> None:
    d = tmp_path / "cto"
    assert mm.read(d, today="2026-11-06")["count"] == 0
    mm.bump(d, "plan", today="2026-11-06")
    mm.bump(d, "execute", today="2026-11-06")
    m = mm.bump(d, "execute", today="2026-11-06")
    assert m["count"] == 3 and m["by_type"] == {"plan": 1, "execute": 2}
    assert json.loads((d / "today" / "model_calls.json").read_text())["count"] == 3
    # A new day starts from zero — the file carries its own date.
    assert mm.read(d, today="2026-11-07")["count"] == 0
    assert mm.bump(d, "replan", today="2026-11-07")["count"] == 1


def test_context_line_is_silent_until_the_first_ask_then_informs_never_budgets(
    tmp_path: Path,
) -> None:
    d = tmp_path / "cto"
    assert mm.context_line(d, today="2026-11-06") == ""
    mm.bump(d, "execute", today="2026-11-06")
    mm.bump(d, "replan", today="2026-11-06")
    (d / "today" / "external_calls.json").write_text(
        json.dumps({"date": "2026-11-06", "count": 1, "est_cost_gbp": 0.2})
    )
    mm.add_tokens(d, "execute", 18_000, 400, today="2026-11-06")
    mm.add_tokens(d, "replan", 5_000, 300, today="2026-11-06")
    line = mm.context_line(d, today="2026-11-06")
    assert "Model asks today: 2 (execute 1, replan 1, ≈23k tokens of context read)" in line
    assert "1 Claude escalation" in line
    assert "factory floor" in line
    assert "budget" not in line  # information, never a quota
    assert mm.read(d, today="2026-11-06")["tokens_by_type"] == {"execute": 18_000, "replan": 5_000}


@pytest.mark.asyncio
async def test_a_wake_that_plans_is_metered_and_the_count_reaches_the_next_context(
    tmp_path: Path,
) -> None:
    model = _Model()
    fabric = Fabric(agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=model)
    agent = fabric.register_agent("cto")
    await fabric.wake("cto")
    assert model.calls >= 1
    m = mm.read(agent.directory)
    assert m["count"] == model.calls, (m, model.calls)
    assert sum(m["by_type"].values()) == model.calls
    assert m["tokens_in"] == model.calls and m["tokens_out"] == model.calls  # 1+1 per fake call
    assert "Model asks today" in fabric._model_meter_context(agent)


@pytest.mark.asyncio
async def test_the_session_reflection_asks_what_the_world_got_not_what_was_done(
    tmp_path: Path,
) -> None:
    """The pre-sleep note is written against the node's outcome ledger and
    the day's ask count — not the agent's own account of the day."""
    import time

    class _Capture(_Model):
        def __init__(self) -> None:
            super().__init__()
            self.prompt = ""

        async def think(self, agent_id, context, prompt, **kwargs):
            self.prompt = prompt
            return await super().think(agent_id, context, prompt, **kwargs)

    model = _Capture()
    fabric = Fabric(agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=model)
    agent = fabric.register_agent("amara")
    (agent.directory / "outcomes.json").write_text(
        json.dumps(
            [
                {"kind": "pr_merged", "title": "feat: lead form", "ref": "u1", "at": time.time()},
                {"kind": "claim_rejected", "title": "RFC-005 done", "ref": "c1", "at": time.time()},
                {"kind": "pr_merged", "title": "old", "ref": "u0", "at": time.time() - 3 * 86400},
            ]
        )
    )
    mm.bump(agent.directory, "execute")
    mm.bump(agent.directory, "execute")
    fabric._running = True  # a running fabric — a stopping one writes no note
    await fabric._session_reflection(agent, "did things", {})
    assert "pr merged: feat: lead form" in model.prompt
    assert "rejected — claim rejected: RFC-005 done" in model.prompt
    assert "old" not in model.prompt
    assert "It took 3 model asks today" in model.prompt  # two executes + this reflect
    assert "what the world got from you" in model.prompt
