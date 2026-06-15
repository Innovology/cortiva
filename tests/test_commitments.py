"""Commitments ledger — pressure math (required utilisation), IO, and the
native-tool wiring (register/update/coffee → ReflectionSuffix)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cortiva.core import commitments as cm
from cortiva.core.agent_tools import apply_tool_calls_to_suffix, tools_for_agent
from cortiva.core.reflection import ReflectionSuffix


def _at(now: datetime, **kw) -> datetime:
    return now + timedelta(**kw)


def test_required_utilisation_captures_size_vs_time(tmp_path) -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    week = (now + timedelta(days=7)).isoformat()
    day = (now + timedelta(days=1)).isoformat()

    # 10-minute task, a week out → ~0 (calm).
    tiny = cm.Commitment(id="a", what="x", due_at=week, effort_hours=10 / 60)
    assert cm.required_utilisation(tiny, now) < 0.01

    # 20h of work, a week out → mild.
    mild = cm.Commitment(id="b", what="20 bugs", due_at=week, effort_hours=20)
    u_mild = cm.required_utilisation(mild, now)
    assert 0.05 < u_mild < 0.25

    # Same 20h, one day left, nothing done → high.
    tight = cm.Commitment(id="c", what="20 bugs", due_at=day, effort_hours=20)
    assert cm.required_utilisation(tight, now) > 0.7

    # 20h with two hours left → impossible (U > 1).
    crunch = cm.Commitment(
        id="d", what="20 bugs", due_at=_at(now, hours=2).isoformat(), effort_hours=20
    )
    assert cm.required_utilisation(crunch, now) >= cm.AT_RISK_UTILISATION


def test_progress_reduces_pressure(tmp_path) -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    due = (now + timedelta(hours=10)).isoformat()
    c = cm.Commitment(id="a", what="x", due_at=due, effort_hours=10)
    before = cm.required_utilisation(c, now)  # 10h work / 10h left = 1.0
    c.progress = 0.8  # only 2h left of work
    after = cm.required_utilisation(c, now)
    assert before >= 1.0 and after < 0.3


def test_subtasks_drive_objective_progress() -> None:
    c = cm.Commitment(
        id="a", what="x", effort_hours=4,
        subtasks=[{"desc": "one", "done": True}, {"desc": "two", "done": False}],
    )
    assert cm.progress_of(c) == 0.5
    assert cm.work_remaining_hours(c) == 2.0  # half of 4h


def test_overdue_and_no_deadline() -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    overdue = cm.Commitment(
        id="a", what="x", due_at=(now - timedelta(hours=3)).isoformat(), effort_hours=2
    )
    assert cm.is_overdue(overdue, now)
    assert cm.required_utilisation(overdue, now) >= cm.AT_RISK_UTILISATION
    # No deadline → tracked but no pressure.
    none = cm.Commitment(id="b", what="someday", effort_hours=5)
    assert cm.required_utilisation(none, now) == 0.0
    assert not cm.is_overdue(none, now)


def test_felt_pressure_aggregates_dominant_plus_load() -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    soon = (now + timedelta(hours=1)).isoformat()
    # One impossible deadline dominates → high felt pressure.
    one = [cm.Commitment(id="a", what="x", due_at=soon, effort_hours=10)]
    assert cm.felt_pressure(one, now) >= 0.9
    # Many merely-tight ones still add up beyond any single max.
    week = (now + timedelta(days=7)).isoformat()
    many = [cm.Commitment(id=str(i), what="x", due_at=week, effort_hours=30) for i in range(4)]
    single = cm.required_utilisation(many[0], now)
    assert cm.felt_pressure(many, now) > single


def test_register_is_idempotent_and_persists(tmp_path) -> None:
    cm.register(tmp_path, to="alex@x.io", what="readout", due="2026-06-18", effort_hours=3)
    cm.register(tmp_path, to="alex@x.io", what="readout", due="2026-06-18", effort_hours=3)
    items = cm.load(tmp_path)
    assert len(items) == 1  # same promise didn't duplicate
    c = items[0]
    assert c.to == "alex@x.io" and c.effort_hours == 3
    assert c.due_at.startswith("2026-06-18T17:00")  # bare date → EOD


def test_update_progress_and_deliver(tmp_path) -> None:
    c = cm.register(tmp_path, to="a@x", what="job", due="2026-06-20", effort_hours=4)
    cm.update(tmp_path, commitment_id=c.id, progress=0.5)
    assert cm.load(tmp_path)[0].progress == 0.5
    # delivered=true is the only thing that discharges it.
    cm.update(tmp_path, commitment_id=c.id, delivered=True, artifact="https://doc")
    got = cm.load(tmp_path)[0]
    assert got.status == "delivered" and got.artifact == "https://doc"
    assert cm.required_utilisation(got) == 0.0  # discharged → no pressure


def test_update_no_id_targets_most_pressing(tmp_path) -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    cm.register(tmp_path, to="a@x", what="relaxed", due="2026-12-31", effort_hours=1)
    hot = cm.register(
        tmp_path, to="b@x", what="urgent", due=(now + timedelta(hours=1)).isoformat(),
        effort_hours=10,
    )
    cm.update(tmp_path, delivered=True, now=now)  # no id → the pressing one
    by_id = {c.id: c for c in cm.load(tmp_path)}
    assert by_id[hot.id].status == "delivered"


def test_prune_archives_long_overdue_as_missed(tmp_path) -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    old = (now - timedelta(days=10)).isoformat()
    cm.register(tmp_path, to="a@x", what="forgotten", due=old, effort_hours=2, now=now)
    items = cm.prune(cm.load(tmp_path), now=now)
    assert items[0].status == "missed"


def test_summarise_reports_top_and_counts() -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    soon = (now + timedelta(hours=1)).isoformat()
    items = [
        cm.Commitment(id="a", to="founder@x", what="big", due_at=soon, effort_hours=20),
        cm.Commitment(id="b", to="peer@x", what="small", due_at="2026-12-31T17:00:00+00:00", effort_hours=1),
    ]
    s = cm.summarise(items, now)
    assert s["open"] == 2 and s["at_risk"] >= 1
    assert s["top_to"] == "founder@x"  # highest-U commitment surfaced
    assert s["pressure"] > 0.5


def test_parse_due_forms() -> None:
    assert cm.parse_due("2026-06-18").startswith("2026-06-18T17:00")
    assert cm.parse_due("2026-06-18T09:30").startswith("2026-06-18T09:30")
    assert cm.parse_due("not a date") == ""
    assert cm.parse_due("") == ""


# --- native tool wiring -----------------------------------------------------


def test_commitment_tools_offered_to_every_agent() -> None:
    names = [t["function"]["name"] for t in tools_for_agent("nobody", scheduling_authorised=set())]
    assert "register_commitment" in names
    assert "update_commitment" in names
    assert "drink_coffee" in names


def test_tool_calls_overlay_onto_suffix() -> None:
    suffix = ReflectionSuffix()
    apply_tool_calls_to_suffix(suffix, [
        {"name": "register_commitment",
         "arguments": {"to": "a@x", "what": "job", "due": "2026-06-20", "effort_hours": 3}},
        {"name": "update_commitment", "arguments": {"progress": 0.5}},
        {"name": "drink_coffee", "arguments": {}},
    ])
    assert suffix.register_commitment["what"] == "job"
    assert suffix.update_commitment["progress"] == 0.5
    assert suffix.drink_coffee == {}  # empty-but-present → handler fires on `is not None`


def test_register_commitment_schema_requires_core_fields() -> None:
    from cortiva.core.agent_tools import REGISTER_COMMITMENT_TOOL
    req = REGISTER_COMMITMENT_TOOL["function"]["parameters"]["required"]
    assert set(req) == {"to", "what", "due", "effort_hours"}


# --- count-load, reschedule, withdraw (the reconciliation hardening) --------


def test_count_load_grows_with_number_of_open_commitments() -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    far = (now + timedelta(days=30)).isoformat()
    def stack(n):
        return [cm.Commitment(id=str(i), what="x", due_at=far, effort_hours=1) for i in range(n)]
    # All far-future + tiny → ~no deadline pressure, but the PILE itself bites.
    assert cm.count_load(stack(3)) == 0.0     # under the comfort line: count adds nothing
    assert cm.count_load(stack(20)) == 1.0    # well over → full count-load
    p_few = cm.felt_pressure(stack(3), now)
    p_many = cm.felt_pressure(stack(12), now)
    p_huge = cm.felt_pressure(stack(20), now)
    assert p_few < p_many < p_huge            # pressure grows with the count
    assert p_huge >= 0.4                       # a big stack is real pressure on its own


def test_reschedule_keeps_original_due_and_counts(tmp_path) -> None:
    c = cm.register(tmp_path, to="a@x", what="job", due="2026-06-18", effort_hours=4)
    assert c.original_due.startswith("2026-06-18")
    # owner pushes their own deadline → tracked as a scar
    cm.update(tmp_path, commitment_id=c.id, due="2026-06-25", reschedule_by="owner")
    g = cm.load(tmp_path)[0]
    assert g.due_at.startswith("2026-06-25")
    assert g.original_due.startswith("2026-06-18")   # original preserved
    assert g.reschedule_count == 1
    assert g.last_reschedule_by == "owner"


def test_reschedule_by_counterparty_is_legit(tmp_path) -> None:
    c = cm.register(tmp_path, to="maren@x", what="deck", due="2026-06-19", effort_hours=2)
    cm.update(tmp_path, commitment_id=c.id, due="2026-07-20", reschedule_by="counterparty")
    g = cm.load(tmp_path)[0]
    assert g.last_reschedule_by == "counterparty"   # Maren relaxed it — no scar on the agent


def test_reschedule_drops_pressure(tmp_path) -> None:
    now = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
    # 10h of work due in 2h = panic.
    c = cm.register(tmp_path, to="a@x", what="big", due=(now + timedelta(hours=2)).isoformat(),
                    effort_hours=10, now=now)
    assert cm.required_utilisation(cm.load(tmp_path)[0], now) >= cm.AT_RISK_UTILISATION
    # requester bumps it a month → pressure collapses.
    cm.update(tmp_path, commitment_id=c.id, due=(now + timedelta(days=30)).isoformat(),
              reschedule_by="counterparty")
    assert cm.required_utilisation(cm.load(tmp_path)[0], now) < 0.05


def test_withdraw_is_clean_close_not_failure(tmp_path) -> None:
    now = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
    c = cm.register(tmp_path, to="a@x", what="pulled", due=(now - timedelta(hours=5)).isoformat(),
                    effort_hours=3, now=now)
    # overdue → would be at-risk, but the requester withdrew it.
    cm.update(tmp_path, commitment_id=c.id, withdrawn=True)
    g = cm.load(tmp_path)[0]
    assert g.status == "withdrawn"
    assert cm.required_utilisation(g, now) == 0.0      # no pressure
    assert not cm.is_overdue(g, now)                    # not a failure
    assert cm.summarise(cm.load(tmp_path), now)["open"] == 0
