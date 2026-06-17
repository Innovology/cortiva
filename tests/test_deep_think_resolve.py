"""claude binary resolution (#282): the fabric runs under launchd with a
minimal PATH that omits /opt/homebrew/bin, so a bare `claude` doesn't resolve
even though it's installed. The resolver must find it in known locations, and
_claude_env must put those dirs on PATH so claude can also find node."""

from __future__ import annotations

import os

import cortiva.skills.claude_code_deep_think.wrapper as w


def test_resolve_prefers_which(monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda b: "/usr/bin/claude")
    assert w._resolve_claude() == "/usr/bin/claude"


def test_resolve_falls_back_to_known_location(monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda b: None)
    monkeypatch.setattr(
        w.os.path,
        "exists",
        lambda p: p == "/opt/homebrew/bin/claude",
    )
    assert w._resolve_claude() == "/opt/homebrew/bin/claude"


def test_resolve_none_when_absent(monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda b: None)
    monkeypatch.setattr(w.os.path, "exists", lambda p: False)
    assert w._resolve_claude() is None


def test_precondition_passes_with_known_location(monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda b: None)
    monkeypatch.setattr(
        w.os.path,
        "exists",
        lambda p: p == "/opt/homebrew/bin/claude",
    )
    w._check_preconditions()  # must not raise


def test_precondition_raises_when_absent(monkeypatch):
    import pytest

    monkeypatch.setattr(w.shutil, "which", lambda b: None)
    monkeypatch.setattr(w.os.path, "exists", lambda p: False)
    with pytest.raises(w.DeepThinkError):
        w._check_preconditions()


def test_claude_env_puts_brew_on_path(monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda b: None)
    monkeypatch.setattr(
        w.os.path,
        "exists",
        lambda p: p == "/opt/homebrew/bin/claude",
    )
    monkeypatch.setattr(
        "cortiva.core.claude_auth.claude_oauth_env",
        lambda base=None: {"PATH": "/usr/bin"},
    )
    env = w._claude_env()
    parts = env["PATH"].split(os.pathsep)
    assert "/opt/homebrew/bin" in parts
    assert "/usr/bin" in parts  # existing PATH preserved
