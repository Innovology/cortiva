"""Fabric consumption of the ledgers — salience blocks, at-risk escalation,
and expectation inbox-resolution. Uses ``Fabric.__new__`` + a stub agent so we
exercise the methods without standing up the whole fabric."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from cortiva.core import commitments as cm
from cortiva.core import expectations as ex
from cortiva.core.fabric import Fabric


def _fab():
    return Fabric.__new__(Fabric)


def _agent(tmp_path):
    d = tmp_path / "ceo"
    d.mkdir()
    return SimpleNamespace(id="ceo", directory=d)


def test_salience_empty_shows_register_nudge(tmp_path) -> None:
    out = _fab()._commitment_salience_context(_agent(tmp_path))
    assert "register_commitment" in out  # the always-on nudge for the first promise


def test_salience_lists_open_with_heat(tmp_path) -> None:
    a = _agent(tmp_path)
    now = datetime.now(UTC)
    cm.register(
        a.directory,
        to="marcus@x",
        what="the big audit",
        due=(now + timedelta(hours=1)).isoformat(),
        effort_hours=20,
    )
    out = _fab()._commitment_salience_context(a)
    assert "the big audit" in out
    assert "at risk" in out.lower()


def test_expectation_salience_only_when_due_and_silent(tmp_path) -> None:
    a = _agent(tmp_path)
    now = datetime.now(UTC)
    # future → not surfaced
    ex.register(
        a.directory,
        sender="lin@x",
        what="future thing",
        due=(now + timedelta(days=5)).isoformat(),
        now=now,
    )
    assert _fab()._expectation_salience_context(a) == ""
    # overdue + silent → chase block
    ex.register(
        a.directory,
        sender="idris@x",
        what="the design",
        due=(now - timedelta(hours=2)).isoformat(),
        now=now,
    )
    out = _fab()._expectation_salience_context(a)
    assert "Waiting on others" in out and "idris@x" in out


def test_escalates_at_risk_once_then_idempotent(tmp_path) -> None:
    a = _agent(tmp_path)
    now = datetime.now(UTC)
    cm.register(
        a.directory,
        to="maren@x",
        what="cannot land this",
        due=(now - timedelta(hours=1)).isoformat(),
        effort_hours=10,
        now=now,
    )
    fab = _fab()
    calls: list = []
    fab._route_escalation = lambda agent, desc, esc: calls.append((desc, esc))
    fab._escalate_at_risk_commitments(a)
    assert len(calls) == 1  # overdue + work owed → escalated
    assert cm.load(a.directory)[0].escalated_at  # marked
    fab._escalate_at_risk_commitments(a)
    assert len(calls) == 1  # idempotent — not re-escalated


def test_withdrawn_and_delivered_never_escalate(tmp_path) -> None:
    a = _agent(tmp_path)
    now = datetime.now(UTC)
    c1 = cm.register(
        a.directory,
        to="x@x",
        what="pulled",
        due=(now - timedelta(hours=2)).isoformat(),
        effort_hours=5,
        now=now,
    )
    cm.update(a.directory, commitment_id=c1.id, withdrawn=True)
    fab = _fab()
    calls: list = []
    fab._route_escalation = lambda agent, desc, esc: calls.append(1)
    fab._escalate_at_risk_commitments(a)
    assert calls == []  # withdrawn is not a failure → never escalates


def test_resolve_expectations_from_inbox(tmp_path) -> None:
    a = _agent(tmp_path)
    now = datetime.now(UTC)
    ex.register(
        a.directory,
        sender="marcus@x",
        what="scope cut",
        due=(now + timedelta(hours=1)).isoformat(),
        now=now,
    )
    inbox = a.directory / "inbox"
    inbox.mkdir()
    (inbox / "m.json").write_text(json.dumps({"from": "marcus@x", "text": "here it is"}))
    _fab()._resolve_expectations_from_inbox(a)
    assert ex.load(a.directory)[0].status == "received"


# ---------------------------------------------------------------------------
# Delivery stewardship — the DOWNWARD mirror: a manager's felt responsibility
# for their team actually delivering value (block + arousal hook).
# ---------------------------------------------------------------------------


def _report_dir(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def _mgr_fab(reports):
    """Fabric stub where 'ceo' manages the given {rid: directory} reports."""
    fab = Fabric.__new__(Fabric)
    fab.org = SimpleNamespace(subordinates_of=lambda aid: list(reports) if aid == "ceo" else [])
    fab.get_agent = lambda rid: SimpleNamespace(id=rid, directory=reports[rid])
    return fab


def _ceo(tmp_path):
    d = tmp_path / "ceo"
    d.mkdir()
    return SimpleNamespace(id="ceo", directory=d)


def test_team_delivery_load_empty_without_reports(tmp_path) -> None:
    fab = Fabric.__new__(Fabric)
    fab.org = None
    load = fab._team_delivery_load(_agent(tmp_path))
    assert load["promised"] == 0 and load["pressure"] == 0.0


def test_stewardship_block_surfaces_promised_and_escalates_slipping(tmp_path) -> None:
    now = datetime.now(UTC)
    astrid = _report_dir(tmp_path, "astrid")
    simone = _report_dir(tmp_path, "simone")
    # Astrid: slipping (big effort, ~no time → U high)
    cm.register(
        astrid,
        to="maren@x",
        what="ship the launch",
        due=(now + timedelta(hours=1)).isoformat(),
        effort_hours=40,
    )
    # Simone: on track (small effort, lots of time)
    cm.register(
        simone,
        to="board@x",
        what="quarterly numbers",
        due=(now + timedelta(days=10)).isoformat(),
        effort_hours=2,
    )
    out = _mgr_fab({"astrid": astrid, "simone": simone})._reports_commitment_context(_ceo(tmp_path))
    assert "ensuring your team delivers" in out.lower()
    assert "OUTRANKS" in out  # imperative register, like directives
    assert "ship the launch" in out  # the slipping one
    assert "Slipping now" in out
    assert "quarterly numbers" in out  # on-track one surfaced PROACTIVELY
    assert "On track" in out


def test_stewardship_block_empty_when_team_has_no_promises(tmp_path) -> None:
    astrid = _report_dir(tmp_path, "astrid")
    assert _mgr_fab({"astrid": astrid})._reports_commitment_context(_ceo(tmp_path)) == ""


def test_stewardship_arousal_fires_when_team_slipping(tmp_path) -> None:
    import asyncio

    now = datetime.now(UTC)
    astrid = _report_dir(tmp_path, "astrid")
    cm.register(
        astrid,
        to="maren@x",
        what="ship the launch",
        due=(now + timedelta(hours=1)).isoformat(),
        effort_hours=40,
    )
    fab = _mgr_fab({"astrid": astrid})
    fired: dict = {}

    async def _dispatch(agent_id, event_type, payload):
        fired["event"] = event_type
        fired["payload"] = payload

    fab.plugin_manager = SimpleNamespace(dispatch_hook=_dispatch)
    asyncio.run(fab._dispatch_stewardship_arousal(_ceo(tmp_path)))
    assert fired.get("event") == "stewardship"
    assert fired["payload"]["pressure"] > 0.0
    assert fired["payload"]["at_risk"] >= 1


def test_stewardship_arousal_silent_when_team_on_track(tmp_path) -> None:
    import asyncio

    now = datetime.now(UTC)
    simone = _report_dir(tmp_path, "simone")
    cm.register(
        simone,
        to="board@x",
        what="quarterly numbers",
        due=(now + timedelta(days=10)).isoformat(),
        effort_hours=2,
    )
    fab = _mgr_fab({"simone": simone})
    fired: dict = {}

    async def _dispatch(agent_id, event_type, payload):
        fired["event"] = event_type

    fab.plugin_manager = SimpleNamespace(dispatch_hook=_dispatch)
    asyncio.run(fab._dispatch_stewardship_arousal(_ceo(tmp_path)))
    assert fired == {}  # on-track team → no pressure → nothing fired
