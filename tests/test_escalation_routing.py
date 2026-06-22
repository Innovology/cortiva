"""Escalation routes up the management chain, and outbound is throttled so an
unanswered ask can't become a message storm."""
from __future__ import annotations

import json
from types import SimpleNamespace

from cortiva.core.fabric import Fabric


def _fab():
    f = Fabric.__new__(Fabric)
    f._emit = lambda *a, **k: None  # type: ignore[attr-defined]
    return f


def test_thread_key_normalises_subject_and_recipient():
    k1 = Fabric._thread_key("Alex@Example.com", "Re: Re: Hello")
    k2 = Fabric._thread_key(["alex@example.com"], "Hello")
    assert k1 == k2 == "alex@example.com|hello"


def test_resolve_manager_cross_node(tmp_path):
    f = _fab()
    f.org = None
    f.agents_dir = tmp_path
    (tmp_path / ".email_meta.json").write_text(
        json.dumps(
            {
                "directory": [
                    {"id": "amara", "reports_to": "cto"},
                    {"id": "cto", "email": "samantha@workforce.innovology.io", "reports_to": "ceo"},
                    {"id": "ceo", "email": "maren@workforce.innovology.io", "reports_to": "human-founder"},
                ]
            }
        ),
        encoding="utf-8",
    )
    # An IC resolves its manager even though the manager is "on another node".
    assert f._resolve_manager("amara") == ("cto", "samantha@workforce.innovology.io")
    # The CEO reports to the founder (email empty → caller routes to founder).
    assert f._resolve_manager("ceo") == ("human-founder", "")
    # Unknown agent → nothing (caller must NOT fall back to the founder).
    assert f._resolve_manager("ghost") == ("", "")


def test_outbound_rapid_resends_are_suppressed(tmp_path):
    f = _fab()
    agent = SimpleNamespace(id="amara", directory=tmp_path)
    for _ in range(5):
        f._queue_outbound_email(
            agent, {"to": "simone@workforce.innovology.io", "subject": "help", "body": "x"}
        )
    sent = list((tmp_path / "outbox" / "email").glob("*.json"))
    assert len(sent) == 1  # only the first send goes out; the rest are debounced


def test_distinct_threads_not_suppressed(tmp_path):
    f = _fab()
    agent = SimpleNamespace(id="amara", directory=tmp_path)
    f._queue_outbound_email(agent, {"to": "simone@workforce.innovology.io", "subject": "A", "body": "x"})
    f._queue_outbound_email(agent, {"to": "simone@workforce.innovology.io", "subject": "B", "body": "x"})
    sent = list((tmp_path / "outbox" / "email").glob("*.json"))
    assert len(sent) == 2  # different subjects are different threads


def test_reply_clears_throttle(tmp_path):
    f = _fab()
    agent = SimpleNamespace(id="amara", directory=tmp_path)
    f._queue_outbound_email(agent, {"to": "simone@workforce.innovology.io", "subject": "help", "body": "x"})
    # A reply from the recipient resets the thread so the conversation continues.
    f._clear_awaiting_for_senders(agent, {"simone@workforce.innovology.io"})
    f._queue_outbound_email(agent, {"to": "simone@workforce.innovology.io", "subject": "help", "body": "x"})
    sent = list((tmp_path / "outbox" / "email").glob("*.json"))
    assert len(sent) == 2
