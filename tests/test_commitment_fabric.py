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
