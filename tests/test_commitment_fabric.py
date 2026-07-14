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


# ---------------------------------------------------------------------------
# Overtime-before-complaint: the saveable band (doesn't fit normal hours,
# overtime still lands it) must force an explicit choice — and escalations
# must say whether overtime was used.
# ---------------------------------------------------------------------------


def _register_with_u(agent_dir, *, u, hours_left=10.0, to="maren@x", what="the thing"):
    """Register an open commitment engineered to a required-utilisation of ~u."""
    now = datetime.now(UTC)
    return cm.register(
        agent_dir,
        to=to,
        what=what,
        due=(now + timedelta(hours=hours_left)).isoformat(),
        effort_hours=u * hours_left,
    )


def test_overtime_can_save_band() -> None:
    now = datetime.now(UTC)

    def mk(u, hours_left=10.0, overdue=False):
        c = cm.Commitment(
            id="x",
            to="a@x",
            what="w",
            due_at=(now + timedelta(hours=(-1 if overdue else hours_left))).isoformat(),
            effort_hours=u * hours_left,
        )
        return c

    assert not cm.overtime_can_save(mk(0.1), now)  # fits a normal day
    assert cm.overtime_can_save(mk(0.5), now)  # the saveable band
    assert cm.overtime_can_save(mk(0.95), now)  # still under wall-clock
    assert not cm.overtime_can_save(mk(1.5), now)  # beyond wall-clock
    assert not cm.overtime_can_save(mk(0.5, overdue=True), now)  # past saving


def test_overtime_block_fires_in_band_and_demands_choice(tmp_path) -> None:
    a = _agent(tmp_path)
    _register_with_u(a.directory, u=0.5)
    out = _fab()._overtime_decision_context(a)
    assert "Overtime decision" in out
    assert "drink_coffee" in out
    assert "Renegotiate" in out
    assert "Escalate for help" in out
    assert "not acceptable" in out  # the ban on bare complaints


def test_overtime_block_silent_when_on_track_or_hopeless(tmp_path) -> None:
    a = _agent(tmp_path)
    _register_with_u(a.directory, u=0.1, what="easy")  # fits normal hours
    _register_with_u(a.directory, u=2.0, what="hopeless")  # beyond wall-clock
    assert _fab()._overtime_decision_context(a) == ""


def test_overtime_block_suppressed_while_caffeinated(tmp_path) -> None:
    a = _agent(tmp_path)
    _register_with_u(a.directory, u=0.5)
    # Stamp a fresh coffee — the agent is already pushing.
    today = a.directory / "today"
    today.mkdir()
    (today / "coffee.json").write_text(json.dumps([datetime.now(UTC).isoformat()]))
    assert _fab()._overtime_decision_context(a) == ""


def test_escalation_reports_overtime_honesty(tmp_path) -> None:
    a = _agent(tmp_path)
    now = datetime.now(UTC)
    cm.register(
        a.directory,
        to="maren@x",
        what="doomed",
        due=(now + timedelta(hours=1)).isoformat(),
        effort_hours=40,
    )
    fab = _fab()
    caught: list[str] = []
    fab._route_escalation = lambda agent, desc, esc: caught.append(esc)

    # No coffee taken → the escalation says so.
    fab._escalate_at_risk_commitments(a)
    assert caught and "No overtime was taken" in caught[0]

    # Second agent DID pull overtime → the escalation credits it.
    b_dir = tmp_path / "cfo"
    b_dir.mkdir()
    b = SimpleNamespace(id="cfo", directory=b_dir)
    cm.register(
        b_dir,
        to="maren@x",
        what="doomed too",
        due=(now + timedelta(hours=1)).isoformat(),
        effort_hours=40,
    )
    (b_dir / "today").mkdir()
    (b_dir / "today" / "coffee.json").write_text(json.dumps([datetime.now(UTC).isoformat()]))
    caught.clear()
    fab._escalate_at_risk_commitments(b)
    assert caught and "pulled overtime first (1 coffee(s)" in caught[0]


# ---------------------------------------------------------------------------
# Founder-brief ritual: the org head periodically consults the OWNER on
# direction — pull the leadership's roadmap/strategy material, push it to the
# founder for feedback. Resolves only on a real sent "Founder Brief" email.
# ---------------------------------------------------------------------------


def _head_fab(founder_addr="alex@x.io", *, head=True):
    fab = Fabric.__new__(Fabric)
    fab.org = SimpleNamespace(
        subordinates_of=lambda aid: ["cto", "cpo"] if aid == "ceo" else [],
        manager_of=lambda aid: "human-founder" if aid == "ceo" else "ceo",
    )
    fab.agents = {"ceo": object(), "cto": object(), "cpo": object()}
    fab._email_meta = lambda: {"contacts": [{"address": founder_addr}]} if founder_addr else {}
    return fab


def test_founder_brief_fires_for_org_head_never_briefed(tmp_path) -> None:
    a = _agent(tmp_path)  # id="ceo"
    out = _head_fab()._founder_brief_context(a)
    assert "Founder brief due" in out
    assert "never sent" in out
    assert "PULL" in out and "PUSH" in out
    assert "feedback" in out
    assert "not a status report" in out.lower() or "CONSULTATION" in out


def test_founder_brief_silent_for_non_head_and_no_founder(tmp_path) -> None:
    fab = _head_fab()
    # A mid-org manager (cto reports to ceo, an in-org agent) never sees it.
    d = tmp_path / "cto"
    d.mkdir()
    cto = SimpleNamespace(id="cto", directory=d)
    assert fab._founder_brief_context(cto) == ""
    # No founder contact configured → nowhere to send → silent.
    a = _agent(tmp_path)
    assert _head_fab(founder_addr=None)._founder_brief_context(a) == ""


def _write_sent_brief(agent_dir, *, to="alex@x.io", subject="Founder Brief — W28", when=None):
    sent = agent_dir / "outbox" / "email" / "sent"
    sent.mkdir(parents=True, exist_ok=True)
    when = when or datetime.now(UTC)
    (sent / "b.json").write_text(
        json.dumps({"to": to, "subject": subject, "queued_at": when.isoformat()})
    )


def test_founder_brief_resolves_on_sent_brief_and_reopens_after_cadence(tmp_path) -> None:
    a = _agent(tmp_path)
    fab = _head_fab()
    # A brief sent yesterday → quiet.
    _write_sent_brief(a.directory, when=datetime.now(UTC) - timedelta(days=1))
    assert fab._founder_brief_context(a) == ""
    # …and the resolution is cached.
    assert json.loads((a.directory / "founder_briefs.json").read_text())["last_brief_at"]
    # A brief 8 days old → cadence elapsed → fires again with the age named.
    _write_sent_brief(a.directory, when=datetime.now(UTC) - timedelta(days=8))
    (a.directory / "founder_briefs.json").unlink()
    out = fab._founder_brief_context(a)
    assert "Founder brief due" in out
    assert "8 days ago" in out


def test_founder_brief_ignores_non_brief_mail_to_founder(tmp_path) -> None:
    a = _agent(tmp_path)
    # Ordinary founder mail (status/asks) does NOT count as a brief.
    _write_sent_brief(a.directory, subject="Two items I need from you this week")
    out = _head_fab()._founder_brief_context(a)
    assert "Founder brief due" in out


# ---------------------------------------------------------------------------
# Own your deadlines: a self-set deadline that slips is the OWNER's decision
# (reschedule/descope/overtime) — never a "just flagging it" mail to a human.
# A promise to someone else escalates as a DECISION with a committed new date.
# ---------------------------------------------------------------------------


def test_is_self_owed_detection() -> None:
    def mk(to):
        return cm.Commitment(id="x", to=to, what="w", due_at="", effort_hours=1)

    kw = {"agent_id": "ceo", "first_name": "Maren", "email": "maren@workforce.x"}
    assert cm.is_self_owed(mk("Maren"), **kw)
    assert cm.is_self_owed(mk("maren@workforce.x"), **kw)
    assert cm.is_self_owed(mk("ceo"), **kw)
    assert cm.is_self_owed(mk(""), **kw)  # promise to nobody = self-plan
    assert not cm.is_self_owed(mk("alex@px.io"), **kw)
    assert not cm.is_self_owed(mk("Samantha"), **kw)


def _esc_fab(cards):
    fab = _fab()
    fab._load_directory_cards = lambda: cards
    fab._recent_coffees = lambda agent, hours: 0
    fab._emit = lambda *a, **k: None
    return fab


def test_self_set_deadline_never_emails_a_human(tmp_path) -> None:
    a = _agent(tmp_path)
    now = datetime.now(UTC)
    cm.register(
        a.directory,
        to="Maren",
        what="my own planning item",
        due=(now + timedelta(hours=1)).isoformat(),
        effort_hours=40,
    )
    fab = _esc_fab([{"id": "ceo", "first": "Maren", "email": "maren@workforce.x"}])
    sent: list = []
    fab._route_escalation = lambda agent, desc, esc: sent.append(esc)
    fab._escalate_at_risk_commitments(a)
    assert sent == []  # no flag mail, ever
    # …and it doesn't re-fire every heartbeat: escalated_at is stamped.
    items = cm.load(a.directory)
    assert items[0].escalated_at


def test_other_owed_escalation_is_a_decision_not_a_flag(tmp_path) -> None:
    a = _agent(tmp_path)
    now = datetime.now(UTC)
    cm.register(
        a.directory,
        to="alex@px.io",
        what="board pack",
        due=(now + timedelta(hours=1)).isoformat(),
        effort_hours=40,
    )
    fab = _esc_fab([{"id": "ceo", "first": "Maren", "email": "maren@workforce.x"}])
    sent: list = []
    fab._route_escalation = lambda agent, desc, esc: sent.append(esc)
    fab._escalate_at_risk_commitments(a)
    assert len(sent) == 1
    esc = sent[0]
    assert "My decision: I am rescheduling delivery to" in esc
    assert "push back now" in esc  # actionable, recipient can veto
    assert "honest reason" in esc.lower()
    assert "I need help" not in esc  # the old shrug is gone
    assert "No overtime was taken" in esc  # honesty line preserved


def test_salience_mirror_for_failing_self_deadline(tmp_path) -> None:
    a = _agent(tmp_path)
    now = datetime.now(UTC)
    cm.register(
        a.directory,
        to="Maren",
        what="my own planning item",
        due=(now + timedelta(hours=1)).isoformat(),
        effort_hours=40,
    )
    fab = _esc_fab([{"id": "ceo", "first": "Maren", "email": "maren@workforce.x"}])
    out = fab._commitment_salience_context(a)
    assert "This deadline is YOURS" in out
    assert "Do NOT email anyone a flag" in out
    # An on-track self item gets no mirror.
    a2_dir = tmp_path / "cfo"
    a2_dir.mkdir()
    a2 = SimpleNamespace(id="cfo", directory=a2_dir)
    cm.register(
        a2_dir,
        to="Simone",
        what="easy",
        due=(now + timedelta(days=10)).isoformat(),
        effort_hours=1,
    )
    fab2 = _esc_fab([{"id": "cfo", "first": "Simone", "email": "simone@workforce.x"}])
    assert "This deadline is YOURS" not in fab2._commitment_salience_context(a2)
