"""Off-rota agents are visibly not working: no delegation, chasing, waiting
or escalating about them, and escalation climbs past an inactive manager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cortiva.core.agent import Task
from cortiva.core.reflection import ReflectionSuffix
from tests.test_fabric_extended import _make_fabric, _write_deploy

ROTA = {"wake": "08:00 mon-fri", "sleep": "17:00 mon-fri"}


def _org(tmp_path: Path, **kw):
    fabric = _make_fabric(tmp_path, **kw)
    agents_dir = tmp_path / "agents"
    _write_deploy(agents_dir, "ceo", name="Maren", department="exec", reports_to="human-founder")
    _write_deploy(agents_dir, "coo", name="Marcus", department="exec", reports_to="ceo")
    _write_deploy(agents_dir, "noor", name="Noor", department="ar", reports_to="coo")
    fabric.discover_agents()
    return fabric


def test_no_rota_means_nobody_is_inactive(tmp_path: Path) -> None:
    fabric = _org(tmp_path)
    assert fabric._inactive_agent_ids() == set()
    assert fabric._inactive_context(fabric.agents["ceo"]) == ""


def test_off_rota_agents_are_named_as_not_working(tmp_path: Path) -> None:
    fabric = _org(tmp_path)
    fabric.load_schedules({"ceo": ROTA, "noor": ROTA})
    assert fabric._inactive_agent_ids() == {"coo"}
    block = fabric._inactive_context(fabric.agents["ceo"])
    assert "Not currently working" in block and "Marcus" in block
    assert "do **not** delegate" in block.lower() or "Do **not** delegate" in block
    assert "NOT CURRENTLY WORKING" in fabric._directory_context(fabric.agents["ceo"])


@pytest.mark.asyncio
async def test_manager_cannot_wake_an_off_rota_report(tmp_path: Path) -> None:
    fabric = _org(tmp_path, channel=AsyncMock())
    fabric.load_schedules({"ceo": ROTA, "noor": ROTA})
    woken: list[str] = []

    async def _spy(aid, **_kw):
        woken.append(aid)
        return fabric.agents[aid]

    fabric.wake = _spy  # type: ignore[assignment]
    task = Task(id="t1", description="rally", status="done")
    await fabric._process_reflection(
        fabric.agents["ceo"],
        task,
        ReflectionSuffix(wake={"agents": ["coo"], "reason": "all hands"}),
    )
    assert woken == []


def test_escalation_climbs_past_an_inactive_manager(tmp_path: Path) -> None:
    fabric = _org(tmp_path)
    fabric.load_schedules({"ceo": ROTA, "noor": ROTA})  # coo off the rota
    mgr, _email = fabric._resolve_manager("noor")
    assert mgr == "ceo"
    fabric.load_schedules({"ceo": ROTA, "noor": ROTA, "coo": ROTA})
    mgr, _email = fabric._resolve_manager("noor")
    assert mgr == "coo"
