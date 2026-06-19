"""Timesheet reconciliation on load — the phantom-hours / day-rollover fix."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from cortiva.core.timesheet import Timesheet


def _write(agent_dir, date, entries):
    p = agent_dir / "today" / "timesheet.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": date, "entries": entries}))


def _entry(wake, sleep=None, hours=None):
    e = {"wake_time": wake.isoformat(), "sleep_time": sleep.isoformat() if sleep else None}
    if hours is not None:
        e["hours"] = hours
    return e


def test_cross_day_open_session_does_not_blow_up_today(tmp_path):
    # A session left OPEN since 3 days ago — the exact phantom that read 130h.
    three_days_ago = datetime.now(UTC) - timedelta(days=3)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    _write(tmp_path, today, [_entry(three_days_ago)])  # open, ancient
    ts = Timesheet(tmp_path, scheduled_hours=7.5)
    total = ts.today().total_hours
    assert total < 16, f"today total should be sane, got {total}"
    # The ancient entry was archived out of today, not counted.
    assert all(e.wake_time.strftime("%Y-%m-%d") == today for e in ts.today().entries)


def test_same_day_orphan_open_session_is_capped(tmp_path):
    today_dt = datetime.now(UTC).replace(hour=1, minute=0)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    # Open session that started early today (e.g. 1am) with no clock-out.
    _write(tmp_path, today, [_entry(today_dt)])
    ts = Timesheet(tmp_path, scheduled_hours=7.5)
    entries = ts.today().entries
    assert len(entries) == 1
    assert entries[0].sleep_time is not None, "orphan must be closed on load"
    assert entries[0].hours <= 12.0 + 0.01, f"orphan hours capped, got {entries[0].hours}"


def test_previous_day_file_archived_and_today_fresh(tmp_path):
    yday = datetime.now(UTC) - timedelta(days=1)
    yday_str = yday.strftime("%Y-%m-%d")
    _write(tmp_path, yday_str, [_entry(yday.replace(hour=9), yday.replace(hour=17))])
    ts = Timesheet(tmp_path, scheduled_hours=8.0)
    # Today starts empty…
    assert ts.today().entries == []
    # …and yesterday's work is preserved in history.
    hist = tmp_path / "journal" / f"timesheet-{yday_str}.json"
    assert hist.exists()
    assert json.loads(hist.read_text())["entries"]


def test_clean_same_day_entries_preserved(tmp_path):
    now = datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    w = now.replace(hour=9, minute=0)
    s = now.replace(hour=11, minute=0)
    _write(tmp_path, today, [_entry(w, s)])
    ts = Timesheet(tmp_path, scheduled_hours=7.5)
    assert len(ts.today().entries) == 1
    assert 1.9 < ts.today().total_hours < 2.1


def test_multiple_stale_opens_cannot_block(tmp_path):
    # Several ancient open sessions — must not sum into a blocking total today.
    base = datetime.now(UTC) - timedelta(days=2)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    _write(
        tmp_path,
        today,
        [_entry(base), _entry(base + timedelta(hours=1)), _entry(base + timedelta(hours=2))],
    )
    ts = Timesheet(tmp_path, scheduled_hours=7.5)
    assert ts.today().total_hours < 12.0
