"""Self-healing claude binary resolver (the path-launch-wedge cure).

These tests drive the manager entirely through fakes — no real 200MB binary is
copied and no subprocess is spawned. The contract under test:

* lay a node-managed copy on first use, and never exec the brew/Caskroom path;
* ask the package manager for a newer release on a slow tick;
* when the upstream binary changes, promote the new build only after it passes
  ``--version`` AND a real headless call, keeping the old binary for rollback;
* CURE rot — when a timed ``--version`` probe wedges — by laying a fresh copy
  at a brand-new path string (the wedge is path-keyed);
* keep ``claude_binary()`` spawn-free on the hot path.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import cortiva.core.claude_binary as cb


def _setup(monkeypatch, tmp_path, *, source="/opt/homebrew/bin/claude", sig="100:1", healthy=True):
    """Point the manager at a temp managed dir and fake out the filesystem +
    probe so no real binary or subprocess is involved."""
    managed = tmp_path / "claude-bin"
    monkeypatch.setattr(cb, "_MANAGED_DIR", managed)
    monkeypatch.setattr(cb, "_STATE_FILE", managed / "state.json")
    monkeypatch.setattr(cb, "_cached_path", None)

    state = {"source": source, "sig": sig, "version": "2.1.145 (Claude Code)"}
    monkeypatch.setattr(cb, "_resolve_source", lambda: Path(state["source"]))
    monkeypatch.setattr(cb, "_source_sig", lambda src: state["sig"])
    monkeypatch.setattr(cb, "_version_of", lambda p: state["version"])

    laid: list[str] = []

    def fake_lay(source_path, generation):
        managed.mkdir(parents=True, exist_ok=True)
        # Honour the real novel-nonce contract so each lay is a distinct path.
        dst = managed / f"claude-g{generation}-{len(laid):08x}"
        dst.write_text("fake-binary")
        laid.append(str(dst))
        return str(dst)

    monkeypatch.setattr(cb, "_lay_copy", fake_lay)

    # _validate is the rot probe; controlled per-path via `healthy`.
    wedged: set[str] = set()

    def fake_validate(path):
        return path not in wedged

    monkeypatch.setattr(cb, "_validate", fake_validate)
    # The headless probe passes unless a test says otherwise, and no test ever
    # reaches a real package manager.
    monkeypatch.setattr(cb, "_probe_headless", lambda path: True)
    monkeypatch.setattr(cb, "upgrade_claude_source", lambda force=False: False)
    return state, laid, wedged


def test_lays_managed_copy_on_first_use(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    path = cb.claude_binary()
    assert "claude-bin" in path  # managed copy, NOT the brew path
    assert path != "/opt/homebrew/bin/claude"
    assert Path(path).exists()


def test_claude_binary_hot_path_is_spawn_free(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    # If claude_binary() ever probes, this blows up the test.
    monkeypatch.setattr(cb, "_validate", lambda p: pytest_fail())
    cb._cached_path = None
    first = cb.claude_binary()
    second = cb.claude_binary()  # served from cache, no _validate call
    assert first == second


def test_falls_back_to_bare_claude_when_not_installed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(cb, "_resolve_source", lambda: None)
    monkeypatch.setattr(cb, "_which", lambda: None)  # genuinely not installed
    assert cb.claude_binary() == "claude"
    assert cb.ensure_healthy_claude(force=True) is None


def test_healthy_binary_validated_once_then_trusted(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    p1 = cb.ensure_healthy_claude(force=True)
    p2 = cb.ensure_healthy_claude(force=True)
    assert p1 == p2  # healthy → same managed copy, no re-lay


def test_brew_upgrade_re_derives_copy(monkeypatch, tmp_path):
    state, laid, _ = _setup(monkeypatch, tmp_path)
    first = cb.ensure_healthy_claude(force=True)
    # Simulate `brew upgrade claude-code`: upstream bytes change.
    state["sig"] = "200:2"
    state["source"] = "/opt/homebrew/bin/claude"
    second = cb.ensure_healthy_claude(force=True)
    assert second != first  # re-laid from the new source
    assert len(laid) == 2
    # Generation resets to 0 on a new source (it gets a new path anyway).
    assert "claude-g0" in second


def test_rot_is_cured_with_fresh_path(monkeypatch, tmp_path):
    _state, laid, wedged = _setup(monkeypatch, tmp_path)
    good = cb.ensure_healthy_claude(force=True)
    # The current managed copy now wedges (the path-launch-wedge).
    wedged.add(good)
    cured = cb.ensure_healthy_claude(force=True)
    assert cured != good  # cured by moving to a brand-new path
    assert cured not in wedged
    assert "claude-g1" in cured  # generation bumped
    # State now points at the cured copy.
    assert cb._read_state()["managed_path"] == cured


def test_cure_persists_across_restart(monkeypatch, tmp_path):
    _state, _laid, wedged = _setup(monkeypatch, tmp_path)
    good = cb.ensure_healthy_claude(force=True)
    wedged.add(good)
    cured = cb.ensure_healthy_claude(force=True)
    # Simulate a fresh process: drop the in-memory cache, keep state on disk.
    cb._cached_path = None
    assert cb.claude_binary() == cured  # reads persisted state, no re-lay


def test_missing_managed_file_is_relaid(monkeypatch, tmp_path):
    _state, laid, _ = _setup(monkeypatch, tmp_path)
    first = cb.ensure_healthy_claude(force=True)
    Path(first).unlink()  # someone deleted the managed copy
    cb._cached_path = None
    second = cb.ensure_healthy_claude(force=True)
    assert second != first
    assert Path(second).exists()


def test_prune_keeps_only_current(monkeypatch, tmp_path):
    _state, _laid, wedged = _setup(monkeypatch, tmp_path)
    # Use the REAL _lay_copy/_prune so we exercise pruning of ~current copies.
    monkeypatch.undo()
    managed = tmp_path / "claude-bin"
    src = tmp_path / "src-claude"
    src.write_text("x")
    monkeypatch.setattr(cb, "_MANAGED_DIR", managed)
    monkeypatch.setattr(cb, "_STATE_FILE", managed / "state.json")
    monkeypatch.setattr(cb, "_resolve_source", lambda: src)
    monkeypatch.setattr(cb, "_validate", lambda p: True)
    monkeypatch.setattr(cb, "_probe_headless", lambda p: True)
    monkeypatch.setattr(cb, "upgrade_claude_source", lambda force=False: False)
    monkeypatch.setattr(cb, "_version_of", lambda p: "v")
    cb._cached_path = None

    a = cb.ensure_healthy_claude(force=True)
    # Force a brew-upgrade re-derive by touching the source.
    src.write_text("xy")
    b = cb.ensure_healthy_claude(force=True)
    assert a != b
    # The replaced binary is the rollback target, so it survives one upgrade...
    assert sorted(p.name for p in managed.glob("claude-g*")) == sorted([Path(a).name, Path(b).name])
    # ...and is pruned by the next, which keeps only current + previous.
    src.write_text("xyz")
    c = cb.ensure_healthy_claude(force=True)
    assert sorted(p.name for p in managed.glob("claude-g*")) == sorted([Path(b).name, Path(c).name])


# --------------------------------------------------------------------------
# upgrades: probe before promoting, keep the old binary, follow across processes
# --------------------------------------------------------------------------
def test_new_build_that_fails_headless_probe_is_not_promoted(monkeypatch, tmp_path):
    state, laid, _ = _setup(monkeypatch, tmp_path)
    current = cb.ensure_healthy_claude(force=True)
    # A new build lands that launches (--version passes) but can't do real work.
    state["sig"] = "200:2"
    monkeypatch.setattr(cb, "_probe_headless", lambda path: False)
    after = cb.ensure_healthy_claude(force=True)
    assert after == current  # stayed on the working binary
    assert Path(current).exists()
    assert not Path(laid[-1]).exists()  # the failed candidate was removed
    saved = cb._read_state()
    assert saved["managed_path"] == current
    assert saved["source_sig"] == "100:1"  # still derived from the old build
    assert cb.claude_binary() == current


def test_new_build_that_wedges_on_version_is_not_promoted(monkeypatch, tmp_path):
    state, _laid, _ = _setup(monkeypatch, tmp_path)
    current = cb.ensure_healthy_claude(force=True)
    state["sig"] = "200:2"
    monkeypatch.setattr(cb, "_validate", lambda path: path == current)
    monkeypatch.setattr(cb, "_probe_headless", lambda path: pytest_fail())  # never reached
    assert cb.ensure_healthy_claude(force=True) == current


def test_rejected_build_is_retried_only_after_the_backoff(monkeypatch, tmp_path):
    state, laid, _ = _setup(monkeypatch, tmp_path)
    current = cb.ensure_healthy_claude(force=True)
    state["sig"] = "200:2"
    monkeypatch.setattr(cb, "_probe_headless", lambda path: False)
    cb.ensure_healthy_claude(force=True)
    lays = len(laid)
    cb.ensure_healthy_claude(force=True)
    assert len(laid) == lays  # same build, inside the backoff: no 200MB re-copy
    # The outage passes; once the backoff expires the same build is retried.
    monkeypatch.setattr(cb, "_probe_headless", lambda path: True)
    saved = cb._read_state()
    saved["skip_until"] = 1.0
    cb._write_state(saved)
    promoted = cb.ensure_healthy_claude(force=True)
    assert promoted != current
    assert cb._read_state()["source_sig"] == "200:2"


def test_promotion_keeps_previous_binary_and_rollback_restores_it(monkeypatch, tmp_path):
    state, laid, _ = _setup(monkeypatch, tmp_path)
    old = cb.ensure_healthy_claude(force=True)
    state["sig"], state["version"] = "200:2", "2.1.311 (Claude Code)"
    new = cb.ensure_healthy_claude(force=True)
    assert new != old
    saved = cb._read_state()
    assert saved["managed_path"] == new
    assert saved["version"] == "2.1.311 (Claude Code)"
    assert saved["previous_path"] == old
    assert Path(old).exists()  # kept on disk for rollback

    assert cb.rollback_claude() == old
    assert cb.claude_binary() == old
    assert Path(new).exists()  # rollback is itself reversible
    # The build we rolled back from is not re-adopted on the next tick...
    lays = len(laid)
    assert cb.ensure_healthy_claude(force=True) == old
    assert len(laid) == lays
    # ...but the following release is.
    state["sig"] = "300:3"
    assert cb.ensure_healthy_claude(force=True) not in (old, new)


def test_rollback_without_a_previous_binary_is_a_noop(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    current = cb.ensure_healthy_claude(force=True)
    assert cb.rollback_claude() is None
    assert cb.claude_binary() == current


def test_rot_cure_after_declined_build_reuses_the_working_bytes(monkeypatch, tmp_path):
    state, _laid, wedged = _setup(monkeypatch, tmp_path)
    copied_from: list[str] = []
    real_lay = cb._lay_copy

    def recording_lay(source_path, generation):
        copied_from.append(str(source_path))
        return real_lay(source_path, generation)

    monkeypatch.setattr(cb, "_lay_copy", recording_lay)
    current = cb.ensure_healthy_claude(force=True)
    state["sig"] = "200:2"
    monkeypatch.setattr(cb, "_probe_headless", lambda path: False)
    cb.ensure_healthy_claude(force=True)  # new build declined
    wedged.add(current)  # ...and then the working copy's path wedges
    cured = cb.ensure_healthy_claude(force=True)
    assert cured != current
    assert copied_from[-1] == current  # cured from the working bytes, not the bad build
    assert cb._read_state()["source_sig"] == "100:1"


def test_other_process_follows_a_promotion_without_restart(monkeypatch, tmp_path):
    """The fabric caches the path it execs; the node-client's watchdog does the
    promoting. The fabric must pick the new binary up from the shared state
    file even though the old copy is still on disk (kept for rollback)."""
    state, _laid, _ = _setup(monkeypatch, tmp_path)
    old = cb.ensure_healthy_claude(force=True)
    assert cb.claude_binary() == old  # this process has `old` cached
    # Another process promotes a new build: only the state file changes.
    new = tmp_path / "claude-bin" / "claude-g0-other"
    new.write_text("fake-binary")
    saved = cb._read_state()
    saved.update({"managed_path": str(new), "previous_path": old})
    cb._write_state(saved)
    os.utime(cb._STATE_FILE, ns=(1, 1))  # a distinct mtime, whatever the clock resolution
    assert cb.claude_binary() == str(new)


def test_managed_copy_is_laid_without_quarantine(monkeypatch, tmp_path):
    """Homebrew quarantines the cask and the attribute rides along on a copy;
    a quarantined first exec hangs on a Gatekeeper prompt nobody can click."""
    _setup(monkeypatch, tmp_path)
    monkeypatch.undo()
    monkeypatch.setattr(cb, "_MANAGED_DIR", tmp_path / "claude-bin")
    monkeypatch.setattr(cb.sys, "platform", "darwin")
    src = tmp_path / "src-claude"
    src.write_text("x")
    runs: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cb.subprocess, "run", fake_run)
    laid = cb._lay_copy(src, 0)
    assert runs == [["/usr/bin/xattr", "-d", "com.apple.quarantine", laid]]
    # Elsewhere there is no such attribute and nothing is spawned.
    monkeypatch.setattr(cb.sys, "platform", "linux")
    cb._lay_copy(src, 0)
    assert len(runs) == 1


def test_upgrade_command_follows_the_installed_cask(monkeypatch, tmp_path):
    prefix = tmp_path / "homebrew"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "brew").write_text("#!/bin/sh\n")
    stable = prefix / "Caskroom" / "claude-code" / "2.1.145" / "claude"
    latest = prefix / "Caskroom" / "claude-code@latest" / "2.1.322" / "claude"
    brew = str(prefix / "bin" / "brew")
    assert cb._upgrade_command(stable) == [brew, "upgrade", "--cask", "claude-code"]
    # Never switches channel: the latest cask upgrades as the latest cask.
    assert cb._upgrade_command(latest) == [brew, "upgrade", "--cask", "claude-code@latest"]
    # Not a Homebrew install → nothing we know how to upgrade.
    assert cb._upgrade_command(Path("/usr/local/lib/node_modules/x/claude")) is None


def _upgrade_setup(monkeypatch, tmp_path):
    """Real upgrade_claude_source against a fake brew; returns the run log."""
    _setup(monkeypatch, tmp_path)
    monkeypatch.undo()
    managed = tmp_path / "claude-bin"
    monkeypatch.setattr(cb, "_MANAGED_DIR", managed)
    monkeypatch.setattr(cb, "_STATE_FILE", managed / "state.json")
    monkeypatch.setattr(cb, "_resolve_source", lambda: Path("/hb/Caskroom/claude-code/1/claude"))
    monkeypatch.setattr(cb, "_upgrade_command", lambda src: ["brew", "upgrade", "--cask", "x"])
    monkeypatch.delenv(cb._AUTO_UPGRADE_ENV, raising=False)
    runs: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cb.subprocess, "run", fake_run)
    return runs


def test_source_upgrade_runs_then_is_throttled(monkeypatch, tmp_path):
    runs = _upgrade_setup(monkeypatch, tmp_path)
    assert cb.upgrade_claude_source() is True
    assert runs == [["brew", "upgrade", "--cask", "x"]]
    assert cb.upgrade_claude_source() is False  # checked moments ago
    assert len(runs) == 1
    # Once the interval has passed it asks again.
    saved = cb._read_state()
    saved["upgrade_checked_at"] -= cb._UPGRADE_CHECK_S + 1
    cb._write_state(saved)
    assert cb.upgrade_claude_source() is True
    assert len(runs) == 2


def test_source_upgrade_can_be_switched_off(monkeypatch, tmp_path):
    runs = _upgrade_setup(monkeypatch, tmp_path)
    monkeypatch.setenv(cb._AUTO_UPGRADE_ENV, "0")
    assert cb.upgrade_claude_source() is False
    assert runs == []
    assert cb.upgrade_claude_source(force=True) is True  # an operator still can


def test_failed_source_upgrade_never_breaks_the_health_check(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def boom(force=False):
        raise RuntimeError("brew exploded")

    monkeypatch.setattr(cb, "upgrade_claude_source", boom)
    assert cb.ensure_healthy_claude(force=True)  # still heals


def test_headless_probe_requires_a_real_successful_reply(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.undo()
    monkeypatch.setattr(cb, "_MANAGED_DIR", tmp_path / "claude-bin")
    seen: dict = {}

    def run_returning(returncode, stdout):
        def fake_run(cmd, **kwargs):
            seen.update(cmd=cmd, env=kwargs["env"])
            return subprocess.CompletedProcess(cmd, returncode, stdout, "")

        monkeypatch.setattr(cb.subprocess, "run", fake_run)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    run_returning(0, '{"is_error": false, "result": "OK"}')
    assert cb._probe_headless("/x/claude") is True
    assert seen["cmd"][:2] == ["/x/claude", "-p"]
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"  # the agents' auth path
    # Isolated config: an untested build never touches a real Claude config dir.
    assert seen["env"]["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path))

    run_returning(0, '{"is_error": true, "result": "Invalid API key"}')
    assert cb._probe_headless("/x/claude") is False
    run_returning(0, '{"is_error": false, "result": ""}')
    assert cb._probe_headless("/x/claude") is False
    run_returning(1, "")
    assert cb._probe_headless("/x/claude") is False
    run_returning(0, "not json")
    assert cb._probe_headless("/x/claude") is False

    def timing_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(cb.subprocess, "run", timing_out)
    assert cb._probe_headless("/x/claude") is False


def pytest_fail():
    raise AssertionError("claude_binary() must not spawn a probe on the hot path")
