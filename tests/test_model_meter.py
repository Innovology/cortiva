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


def test_context_line_is_silent_until_the_first_ask_then_shows_the_budget(
    tmp_path: Path, monkeypatch
) -> None:
    d = tmp_path / "cto"
    assert mm.context_line(d, today="2026-11-06") == ""
    monkeypatch.setenv("CORTIVA_MODEL_CALL_BUDGET", "4")
    mm.bump(d, "execute", today="2026-11-06")
    mm.bump(d, "replan", today="2026-11-06")
    (d / "today" / "external_calls.json").write_text(
        json.dumps({"date": "2026-11-06", "count": 1, "est_cost_gbp": 0.2})
    )
    line = mm.context_line(d, today="2026-11-06")
    assert "2 of a soft budget of 4" in line
    assert "execute 1, replan 1" in line
    assert "1 Claude escalation" in line
    assert "near" not in line and "over" not in line
    mm.bump(d, "execute", today="2026-11-06")
    assert "near today's budget" in mm.context_line(d, today="2026-11-06")
    mm.bump(d, "execute", today="2026-11-06")
    assert "over today's budget" in mm.context_line(d, today="2026-11-06")


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
    assert "Model asks today" in fabric._model_meter_context(agent)
