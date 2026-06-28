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
    _write_ledger(agent, {"k": {"to": "maren@x", "subject": "SRE brief", "count": 1, "last": recent}})
    out = _brake(_fab(), agent)
    assert "HOLD" in out
    assert "SRE brief" in out
    # The rubric questions are present — it's reasoning, not a silent block.
    assert "truly blocked, or was that just information" in out


def test_capped_thread_says_stop_and_escalate():
    agent = _agent()
    old = (datetime.now(UTC) - timedelta(hours=50)).isoformat()
    _write_ledger(agent, {"k": {"to": "maren@x", "subject": "URGENT data", "count": 3, "last": old}})
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


# --- bounded reset (the ping-pong fix) -------------------------------------


def test_reply_decrements_not_wipes():
    """A reply must NOT reset the cap to zero — it allows one more, no more."""
    agent = _agent()
    _write_ledger(
        agent, {"maua@x|brief": {"to": "maren@x", "subject": "brief", "count": 3, "last": "2026-01-01T00:00:00+00:00"}}
    )
    fab = SimpleNamespace(
        _load_outbox_ledger=lambda a: Fabric._load_outbox_ledger(SimpleNamespace(), a),
        _save_outbox_ledger=lambda a, led: Fabric._save_outbox_ledger(SimpleNamespace(), a, led),
    )
    Fabric._clear_awaiting_for_senders(fab, agent, {"maren@x"})
    led = json.loads((agent.directory / "outbox" / ".threads.json").read_text())
    entry = led["maua@x|brief"]
    assert entry["count"] == 2  # 3 -> 2, NOT deleted, NOT 0
    assert "last" not in entry  # debounce cleared so the one reply can go
