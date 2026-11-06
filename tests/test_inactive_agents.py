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


def test_team_delivery_load_ignores_off_rota_reports(tmp_path: Path) -> None:
    from cortiva.core import commitments as _cm

    fabric = _org(tmp_path)
    # coo promised the ceo something and is overdue — but coo is off the rota.
    _cm.register(
        fabric.agents["coo"].directory, to="ceo@x", what="roadmap", due="2020-01-01T00:00:00+00:00"
    )
    fabric.load_schedules({"ceo": ROTA, "noor": ROTA})
    assert fabric._team_delivery_load(fabric.agents["ceo"])["promised"] == 0
    assert fabric._reports_commitment_context(fabric.agents["ceo"]) == ""
    fabric.load_schedules({"ceo": ROTA, "noor": ROTA, "coo": ROTA})
    assert fabric._team_delivery_load(fabric.agents["ceo"])["promised"] >= 1


def test_grounding_backfills_references_on_older_directives(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import json

    from cortiva.core import grounding

    fabric = _org(tmp_path)
    ceo = fabric.agents["ceo"]
    url = "https://github.com/Innovology/innovology.io/issues/228"
    (ceo.directory / "directives.json").write_text(
        json.dumps(
            [
                {
                    "from_addr": "alex@x",
                    "from": "alex@x",
                    "subject": "Priority Number 1",
                    "snippet": f"focus on {url}",
                    "status": "open",
                    "opened_at": "2026-11-04T18:33:14+00:00",
                    "rank_weight": 1.0,
                }
            ]
        )
    )
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {**ref, "status": "unreadable", "reason": "no creds"},
    )
    asyncio.run(fabric._ground_directives(ceo))
    saved = json.loads((ceo.directory / "directives.json").read_text())
    assert (
        saved[0]["references"][0]["url"] == url
        and saved[0]["references"][0]["status"] == "unreadable"
    )
    assert "could **NOT** read" in fabric._directive_salience_context(ceo)


def test_planned_tasks_aimed_at_off_rota_colleagues_are_struck(tmp_path: Path) -> None:
    # 2026-11-06 10:03–10:10: the CEO planned chases to the CMO, CFO and COO,
    # spent a session on each, and the sender held every mail.
    fabric = _org(tmp_path)
    fabric.load_schedules({"ceo": ROTA, "noor": ROTA})  # coo (Marcus) off the rota
    ceo = fabric.agents["ceo"]
    ceo.set_plan(
        "- [ ] **[HIGH]** Chase COO for W44 EFD Pre-Read\n"
        "- [ ] Email Marcus re: availability\n"
        "- [ ] Ask Noor for the rota optimiser output\n"
        "- [ ] Draft the W45 Founder Brief\n"
    )
    struck = fabric._strike_tasks_for_inactive(ceo)
    assert struck == 2
    by_desc = {t.description.split(" *(")[0]: t.status for t in ceo.task_queue.tasks}
    assert by_desc["Chase COO for W44 EFD Pre-Read"] == "skipped"
    assert by_desc["Email Marcus re: availability"] == "skipped"
    assert by_desc["Ask Noor for the rota optimiser output"] == "pending"
    assert by_desc["Draft the W45 Founder Brief"] == "pending"
    plan = (ceo.directory / "today" / "plan.md").read_text()
    assert "struck: Marcus" in plan
    # Nobody off the rota → nothing struck.
    fabric.load_schedules({"ceo": ROTA, "noor": ROTA, "coo": ROTA})
    ceo.set_plan("- [ ] Chase COO again\n")
    assert fabric._strike_tasks_for_inactive(ceo) == 0


def test_tasks_about_an_off_rota_colleague_are_not_struck(tmp_path: Path) -> None:
    # 10:19 2026-11-06: the first strike also hit "Assemble & Route W45 Founder
    # Brief: consolidate CMO/CFO inputs, COO availability concern…" and "File
    # Executive Escalations to Founder: document COO availability" — tasks for
    # the founder that merely mention the people who are off.
    fabric = _org(tmp_path)
    fabric.load_schedules({"ceo": ROTA, "noor": ROTA})  # coo (Marcus) off
    ceo = fabric.agents["ceo"]
    ceo.set_plan(
        "- [ ] Assemble & Route W45 Founder Brief: consolidate CMO/CFO inputs, COO availability concern\n"
        "- [ ] File Executive Escalations to Founder: document COO availability (24+ days dark)\n"
        "- [ ] Close W44 diagnostic; if COO pre-read is missing, note it\n"
        "- [ ] Follow-up with Marcus on the W44 pre-read\n"
        "- [ ] Direct the COO to file the pre-read by EOD\n"
    )
    assert fabric._strike_tasks_for_inactive(ceo) == 2
    statuses = [t.status for t in ceo.task_queue.tasks]
    assert statuses == ["pending", "pending", "pending", "skipped", "skipped"]


def test_held_mail_is_shown_to_the_sender_with_the_reason(tmp_path: Path) -> None:
    import json as _json

    fabric = _org(tmp_path)
    ceo = fabric.agents["ceo"]
    held = ceo.directory / "outbox" / "email" / "held"
    held.mkdir(parents=True)
    (held / "held-a.json").write_text(
        _json.dumps(
            {
                "to": "samantha@innovology.io",
                "subject": "CI/PAT Access Resolution",
                "held_reason": "nobody called samantha@innovology.io is known at innovology.io — not a listed contact",
            }
        )
    )
    block = fabric._held_mail_context(ceo)
    assert (
        "NOT sent" in block
        and "samantha@innovology.io" in block
        and "not a listed contact" in block
    )
    assert "Do not simply resend" in block
    assert fabric._held_mail_context(fabric.agents["coo"]) == ""


def test_delegation_phrased_with_the_name_at_the_end_is_struck(tmp_path: Path) -> None:
    # 11:30 2026-11-06: "Delegate ownership of the Decision-Log PR to Willow"
    # cost the CTO a session; six words sat between the verb and the name.
    fabric = _org(tmp_path)
    fabric.load_schedules({"ceo": ROTA, "noor": ROTA})  # coo (Marcus) off
    ceo = fabric.agents["ceo"]
    ceo.set_plan(
        "- [ ] Delegate ownership of the W44 pre-read reconciliation to Marcus\n"
        "- [ ] Email the founder about the COO's continued absence\n"
        "- [ ] Assign the EFD log review to the COO by EOD\n"
    )
    assert fabric._strike_tasks_for_inactive(ceo) == 2
    assert [t.status for t in ceo.task_queue.tasks] == ["skipped", "pending", "skipped"]
