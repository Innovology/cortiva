"""Commit attribution: agents commit AS themselves, never as the OS user +
Claude co-author. Covers the identity sanitisation and the defence-in-depth
setup (Claude includeCoAuthoredBy=false + identity as a git-config layer)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from cortiva.core.fabric import Fabric


def _fab(tmp_path):
    f = Fabric.__new__(Fabric)
    f.agents_dir = tmp_path  # _email_meta reads .email_meta.json here (absent → local email)
    return f


def _agent(tmp_path, name):
    d = tmp_path / "cto"
    d.mkdir()
    (d / "deploy.yaml").write_text(f"agent:\n  name: {name}\n  role: CTO\n")
    return SimpleNamespace(id="cto", directory=d)


def test_identity_strips_role_suffix_and_stray_bracket(tmp_path) -> None:
    fab = _fab(tmp_path)
    # the exact malformed string seen in SailCoach commits
    name, email = fab._agent_git_identity(_agent(tmp_path, "Samantha (CTO @ Innovology]"))
    assert name == "Samantha"  # not "Samantha (CTO @ Innovology]"
    assert email.startswith("samantha@")  # first-name local part


def test_identity_keeps_clean_full_name(tmp_path) -> None:
    name, _ = _fab(tmp_path)._agent_git_identity(_agent(tmp_path, "Samantha Ize"))
    assert name == "Samantha Ize"


def test_attribution_disables_claude_coauthor_and_sets_identity(tmp_path) -> None:
    fab = _fab(tmp_path)
    a = _agent(tmp_path, "Samantha Ize")
    cwd = a.directory
    env = fab._ensure_git_attribution(a, cwd)
    # Claude Code told NOT to add the co-author trailer (the real fix).
    settings = json.loads((cwd / ".claude" / "settings.json").read_text())
    assert settings["includeCoAuthoredBy"] is False
    # Identity carried as a git-config layer too (survives env loss), without
    # replacing global config (gh credential helper keeps working).
    assert env["GIT_CONFIG_COUNT"] == "3"
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(3)}
    assert pairs["user.name"] == "Samantha Ize"
    assert pairs["user.email"].startswith("samantha@")
    assert "core.hooksPath" in pairs
    # Backstop hook still installed.
    assert (cwd / ".githooks" / "commit-msg").exists()


def test_attribution_is_idempotent(tmp_path) -> None:
    fab = _fab(tmp_path)
    a = _agent(tmp_path, "Samantha Ize")
    fab._ensure_git_attribution(a, a.directory)
    # second pass must not corrupt the settings file
    fab._ensure_git_attribution(a, a.directory)
    settings = json.loads((a.directory / ".claude" / "settings.json").read_text())
    assert settings["includeCoAuthoredBy"] is False
