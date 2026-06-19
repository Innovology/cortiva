"""Expectations ledger — chase model (due+silent), resolution on inbound."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cortiva.core import expectations as ex


def _e(now, hours, **kw):
    return ex.Expectation(
        id=kw.get("id", "x"),
        sender=kw.get("sender", "marcus@x"),
        what=kw.get("what", "the audit"),
        due_at=(now + timedelta(hours=hours)).isoformat(),
        created_at=(now - timedelta(days=1)).isoformat(),
    )


def test_dormant_until_due_then_chase() -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    far = _e(now, 48)  # due in 2 days
    soon = _e(now, 3)  # due in 3h (within lead window)
    overdue = _e(now, -5)  # 5h overdue
    assert not ex.should_chase(far, now)  # not yet — don't nag early
    assert ex.should_chase(soon, now)  # imminent → chase
    assert ex.should_chase(overdue, now)  # overdue → chase
    assert ex.is_overdue(overdue, now)


def test_chase_pressure_is_mild_and_grows_with_overdue() -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    a_bit = ex.chase_pressure([_e(now, -2)], now)
    lots = ex.chase_pressure([_e(now, -72)], now)
    assert 0 < a_bit < lots <= 1.0
    # nothing due → zero
    assert ex.chase_pressure([_e(now, 48)], now) == 0.0


def test_no_date_never_chases() -> None:
    e = ex.Expectation(
        id="x",
        sender="a@x",
        what="someday thing",
        due_at="",
        created_at="2026-06-13T09:00:00+00:00",
    )
    assert not ex.should_chase(e)
    assert not ex.is_overdue(e)
    assert ex.chase_pressure([e]) == 0.0


def test_register_idempotent_and_persists(tmp_path) -> None:
    ex.register(tmp_path, sender="marcus@x", what="scope cut", due="2026-06-16")
    ex.register(tmp_path, sender="marcus@x", what="scope cut", due="2026-06-16")
    items = ex.load(tmp_path)
    assert len(items) == 1
    assert items[0].due_at.startswith("2026-06-16T17:00")  # bare date → EOD


def test_resolve_from_inbox_marks_received(tmp_path) -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    ex.register(tmp_path, sender="marcus@x", what="scope cut", due="2026-06-16", now=now)
    # Marcus wrote AFTER the expectation opened → received.
    seen = {"marcus@x": (now + timedelta(hours=1)).timestamp()}
    ex.resolve_from_inbox(tmp_path, senders_seen=seen, now=now + timedelta(hours=2))
    assert ex.load(tmp_path)[0].status == "received"


def test_resolve_ignores_older_inbound(tmp_path) -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    ex.register(tmp_path, sender="marcus@x", what="scope cut", due="2026-06-16", now=now)
    # An inbound from BEFORE it opened doesn't count.
    seen = {"marcus@x": (now - timedelta(days=2)).timestamp()}
    ex.resolve_from_inbox(tmp_path, senders_seen=seen, now=now)
    assert ex.load(tmp_path)[0].status == "open"


def test_stale_overdue_dropped(tmp_path) -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    old = (now - timedelta(days=20)).isoformat()
    ex.register(tmp_path, sender="ghost@x", what="forgotten", due=old, now=now)
    ex.resolve_from_inbox(tmp_path, senders_seen={}, now=now)
    assert ex.load(tmp_path)[0].status == "dropped"


def test_reschedule_and_withdraw(tmp_path) -> None:
    e = ex.register(tmp_path, sender="marcus@x", what="scope cut", due="2026-06-16")
    assert e.original_due.startswith("2026-06-16")
    ex.update(tmp_path, expectation_id=e.id, due="2026-07-16")
    g = ex.load(tmp_path)[0]
    assert g.due_at.startswith("2026-07-16") and g.original_due.startswith("2026-06-16")
    assert g.reschedule_count == 1
    # requester withdrew it → clean close, no longer chased
    ex.update(tmp_path, expectation_id=e.id, withdrawn=True)
    assert ex.load(tmp_path)[0].status == "withdrawn"


def test_update_received_no_id_and_missing(tmp_path) -> None:
    ex.register(tmp_path, sender="a@x", what="thing", due="2026-06-20")
    # no id → the single open one; mark it received
    ex.update(tmp_path, received=True)
    assert ex.load(tmp_path)[0].status == "received"
    # nothing open / unknown id → None
    assert ex.update(tmp_path, expectation_id="nope") is None


def test_mark_received_helper(tmp_path) -> None:
    e = ex.register(tmp_path, sender="a@x", what="thing", due="2026-06-20")
    assert ex.mark_received(tmp_path, e.id[:6]) is not None  # prefix id
    assert ex.load(tmp_path)[0].status == "received"


def test_summarise(tmp_path) -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    ex.register(
        tmp_path,
        sender="marcus@x",
        what="late one",
        due=(now - timedelta(hours=3)).isoformat(),
        now=now,
    )
    ex.register(
        tmp_path,
        sender="yuki@x",
        what="future one",
        due=(now + timedelta(days=5)).isoformat(),
        now=now,
    )
    s = ex.summarise(ex.load(tmp_path), now)
    assert s["open"] == 2 and s["to_chase"] == 1 and s["overdue"] == 1
    assert s["top_from"] == "marcus@x"
    assert s["chase_pressure"] > 0
