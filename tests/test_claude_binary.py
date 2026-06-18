"""Self-healing claude binary resolver (the path-launch-wedge cure).

These tests drive the manager entirely through fakes — no real 200MB binary is
copied and no subprocess is spawned. The contract under test:

* lay a node-managed copy on first use, and never exec the brew/Caskroom path;
* re-derive the copy when the upstream binary changes (brew upgrade);
* CURE rot — when a timed ``--version`` probe wedges — by laying a fresh copy
  at a brand-new path string (the wedge is path-keyed);
* keep ``claude_binary()`` spawn-free on the hot path.
"""

from __future__ import annotations

from pathlib import Path

import cortiva.core.claude_binary as cb


def _setup(monkeypatch, tmp_path, *, source="/opt/homebrew/bin/claude",
           sig="100:1", healthy=True):
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
    monkeypatch.setattr(cb, "_version_of", lambda p: "v")
    cb._cached_path = None

    a = cb.ensure_healthy_claude(force=True)
    # Force a brew-upgrade re-derive by touching the source.
    src.write_text("xy")
    b = cb.ensure_healthy_claude(force=True)
    survivors = sorted(p.name for p in managed.glob("claude-g*"))
    assert survivors == [Path(b).name]  # old copy pruned
    assert a != b


def pytest_fail():
    raise AssertionError("claude_binary() must not spawn a probe on the hot path")
