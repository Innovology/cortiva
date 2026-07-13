"""The calculated-action brake + bounded outbox reset.

Myelin had only accelerators (chase / reply / do) and no counterweight, so
agents re-sent the same message every cycle and ping-ponged courtesy acks
forever (84 "SRE load/routing brief" emails over 11 days). These tests pin the
brake: it reckons with what the agent already sent and still owes, says HOLD on
a still-warm thread, says STOP after the cap, and — crucially — a reply no
longer WIPES the throttle (it allows one more send, not unlimited).
"""

import json
import tempfile
import time as _time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from cortiva.core.fabric import Fabric, _human_age


def _agent():
    d = Path(tempfile.mkdtemp())
    (d / "outbox").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(id="amara", directory=d)


def _fab():
    # The brake only touches _load_outbox_ledger/_save_outbox_ledger + the
    # constants; bind the real unbound methods onto a minimal shim.
    return SimpleNamespace(
        _OUTBOX_MAX_SENDS=Fabric._OUTBOX_MAX_SENDS,
        _RECHASE_HOLD_HOURS=Fabric._RECHASE_HOLD_HOURS,
        _load_outbox_ledger=lambda a: Fabric._load_outbox_ledger(SimpleNamespace(), a),
        _save_outbox_ledger=lambda a, led: Fabric._save_outbox_ledger(SimpleNamespace(), a, led),
    )


def _write_ledger(agent, entries):
    (agent.directory / "outbox" / ".threads.json").write_text(json.dumps(entries))


def _brake(fab, agent):
    return Fabric._calculated_action_context(fab, agent)


def test_human_age():
    assert _human_age(0.5) == "30m"
    assert _human_age(3) == "3h"
    assert _human_age(48) == "2d"


def test_no_history_no_brake():
    assert _brake(_fab(), _agent()) == ""


def test_warm_thread_says_hold():
    agent = _agent()
    recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _write_ledger(
        agent, {"k": {"to": "maren@x", "subject": "SRE brief", "count": 1, "last": recent}}
    )
    out = _brake(_fab(), agent)
    assert "HOLD" in out
    assert "SRE brief" in out
    # The rubric questions are present — it's reasoning, not a silent block.
    assert "truly blocked, or was that just information" in out


def test_capped_thread_says_stop_and_escalate():
    agent = _agent()
    old = (datetime.now(UTC) - timedelta(hours=50)).isoformat()
    _write_ledger(
        agent, {"k": {"to": "maren@x", "subject": "URGENT data", "count": 3, "last": old}}
    )
    out = _brake(_fab(), agent)
    assert "STOP" in out
    assert "escalate" in out.lower()


def test_stale_single_send_allows_a_followup():
    agent = _agent()
    old = (datetime.now(UTC) - timedelta(hours=50)).isoformat()
    _write_ledger(agent, {"k": {"to": "x@y", "subject": "old ask", "count": 1, "last": old}})
    out = _brake(_fab(), agent)
    assert "HOLD" not in out and "STOP" not in out
    assert "something genuinely new" in out


def test_own_commitments_surface_as_finish_first(monkeypatch):
    agent = _agent()
    # Seed a real undelivered commitment via the commitments module.
    from cortiva.core import commitments as cm

    cm.register(agent.directory, to="anika@x", what="approve PR #44", due=None, effort_hours=0.5)
    out = _brake(_fab(), agent)
    assert "unfinished commitments" in out
    assert "approve PR #44" in out


# --- bounded reset + genuine-reply gating (the ping-pong / storm fix) ------


def _clear_shim():
    return SimpleNamespace(
        _load_outbox_ledger=lambda a: Fabric._load_outbox_ledger(SimpleNamespace(), a),
        _save_outbox_ledger=lambda a, led: Fabric._save_outbox_ledger(SimpleNamespace(), a, led),
    )


def test_fresh_reply_decrements_and_lifts_debounce_but_not_wipe():
    """A reply NEWER than our last send frees one slot — decrement, not zero."""
    agent = _agent()
    _write_ledger(
        agent,
        {
            "k": {
                "to": "maren@x",
                "subject": "brief",
                "count": 3,
                "last": "2026-01-01T00:00:00+00:00",
            }
        },
    )
    # reply mtime well after the 2026-01-01 last-send → genuine reply
    Fabric._clear_awaiting_for_senders(_clear_shim(), agent, {"maren@x": _time.time()})
    entry = json.loads((agent.directory / "outbox" / ".threads.json").read_text())["k"]
    assert entry["count"] == 2  # 3 -> 2, NOT deleted, NOT 0
    assert "last" not in entry  # a real reply lifts the debounce for a response


def test_stale_mail_does_NOT_clear_the_throttle():  # noqa: N802
    """The storm fix: old mail sitting in read/ must not keep clearing the
    debounce every reassess (that let the same ack fire 4x in a minute)."""
    agent = _agent()
    just_sent = _iso_now()  # we sent moments ago
    _write_ledger(
        agent, {"k": {"to": "marcus@x", "subject": "deck", "count": 1, "last": just_sent}}
    )
    # counterpart's newest mail is OLD (epoch ~ 2020) — no reply since we wrote
    Fabric._clear_awaiting_for_senders(_clear_shim(), agent, {"marcus@x": 1_577_836_800.0})
    entry = json.loads((agent.directory / "outbox" / ".threads.json").read_text())["k"]
    assert entry["count"] == 1  # untouched
    assert entry["last"] == just_sent  # debounce PRESERVED → next rapid send is blocked


def _iso_now():
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def test_same_reply_clears_only_once():
    """The echo fix: ONE distinct reply spends ONE clear. A re-delivered
    inbound (fresh mtime = same reply) must not re-clear the throttle every
    reassess — that raced two near-identical mails to the founder 69s apart."""
    agent = _agent()
    reply_mtime = _time.time()
    _write_ledger(
        agent,
        {
            "k": {
                "to": "alex@x",
                "subject": "two items",
                "count": 3,
                "last": "2026-01-01T00:00:00+00:00",
            }
        },
    )
    Fabric._clear_awaiting_for_senders(_clear_shim(), agent, {"alex@x": reply_mtime})
    entry = json.loads((agent.directory / "outbox" / ".threads.json").read_text())["k"]
    assert entry["count"] == 2  # first clear honoured
    # Same reply seen again on the next reassess ("last" is gone, so without
    # the cleared_for guard this would decrement again).
    Fabric._clear_awaiting_for_senders(_clear_shim(), agent, {"alex@x": reply_mtime})
    entry = json.loads((agent.directory / "outbox" / ".threads.json").read_text())["k"]
    assert entry["count"] == 2  # NOT decremented again
    # A genuinely NEWER reply clears one more.
    Fabric._clear_awaiting_for_senders(_clear_shim(), agent, {"alex@x": reply_mtime + 60})
    entry = json.loads((agent.directory / "outbox" / ".threads.json").read_text())["k"]
    assert entry["count"] == 1


def _queue_shim(agent, meta):
    ns = SimpleNamespace(
        _load_outbox_ledger=lambda a: Fabric._load_outbox_ledger(SimpleNamespace(), a),
        _save_outbox_ledger=lambda a, led: Fabric._save_outbox_ledger(SimpleNamespace(), a, led),
        _thread_key=Fabric._thread_key,
        _email_meta=lambda: meta,
        _emit=lambda *a, **k: None,
        _OUTBOX_MAX_SENDS=Fabric._OUTBOX_MAX_SENDS,
        _OUTBOX_DEBOUNCE_S=Fabric._OUTBOX_DEBOUNCE_S,
        _OUTBOX_HUMAN_DEBOUNCE_S=Fabric._OUTBOX_HUMAN_DEBOUNCE_S,
    )
    return ns


def test_human_recipient_gets_daily_debounce():
    """Chasing the founder twice in a working day is nagging: the debounce for
    founder contacts / off-domain humans is 24h, while agents keep 6h."""
    from datetime import UTC, datetime, timedelta

    agent = _agent()
    meta = {"domain": "workforce.x", "contacts": [{"address": "Alex <alex@px.io>"}]}
    ten_h_ago = (datetime.now(UTC) - timedelta(hours=10)).isoformat()

    # Founder thread last mailed 10h ago: inside the 24h human debounce → suppressed.
    key_f = Fabric._thread_key("alex@px.io", "two items")
    _write_ledger(
        agent, {key_f: {"to": "alex@px.io", "subject": "two items", "count": 1, "last": ten_h_ago}}
    )
    Fabric._queue_outbound_email(
        _queue_shim(agent, meta),
        agent,
        {"to": "alex@px.io", "subject": "Re: two items", "body": "chase"},
    )
    assert not list((agent.directory / "outbox" / "email").glob("*.json"))

    # Same staleness to a workforce AGENT: past the 6h debounce → sends.
    key_a = Fabric._thread_key("astrid@workforce.x", "deck")
    _write_ledger(
        agent,
        {key_a: {"to": "astrid@workforce.x", "subject": "deck", "count": 1, "last": ten_h_ago}},
    )
    Fabric._queue_outbound_email(
        _queue_shim(agent, meta),
        agent,
        {"to": "astrid@workforce.x", "subject": "Re: deck", "body": "chase"},
    )
    assert len(list((agent.directory / "outbox" / "email").glob("*.json"))) == 1
