"""Self-healing resolver for the ``claude`` binary.

Why this exists — the *path-wedge* failure mode (diagnosed on the Mac mini
nodes, 2026-06-18). After days of uptime, exec of the claude binary at its
canonical install path (e.g. ``/opt/homebrew/Caskroom/claude-code/<ver>/claude``)
wedges *forever* at process startup: every ``claude -p`` and even
``claude --version`` hangs (≈32K RSS, stuck in dyld before ``main``), so the
node's opus voice-compose subprocess times out and agent emails come out
voiceless (the un-composed local-model draft). The machine is otherwise
healthy — fds, memory, procs, daemons all fine. The wedge is bound to the exact
binary **realpath**: a byte-identical copy at *any other path* — even the same
directory under a different filename — launches instantly. The poisoned
per-path launch state is only rebuilt on reboot, which is far too coarse for a
node hosting a live workforce.

This module makes the node immune without a reboot, and hands-off:

* **Decouple from brew.** The node execs claude from a node-managed copy under
  ``~/.cortiva/claude-bin/`` with a novel path string, never the brew/Caskroom
  path directly. :func:`claude_binary` returns that path; every spawn site uses
  it.
* **Upgrades the source.** Homebrew installs of Claude Code do not update
  themselves ("Homebrew installations do not auto-update. Run ``brew upgrade
  claude-code``" — code.claude.com/docs/en/setup), and nothing on a node ever
  ran that command, so a node stayed on the build it was installed with (Mini-2
  sat on 2.1.145 from June to November 2026). :func:`upgrade_claude_source`
  runs the cask upgrade on a slow tick, driven by the same watchdog call.
* **Follows the source, safely.** The upstream binary's (size, mtime) is the
  source signature. When it changes, :func:`ensure_healthy_claude` lays the new
  build as a *candidate* and promotes it only after a timed ``--version`` AND a
  real headless ``claude -p`` call both succeed. A candidate that fails is
  deleted and the node stays on the binary it had. The binary it replaced is
  kept on disk as the rollback target (:func:`rollback_claude`).
* **Rot detection + cure.** A timed ``--version`` probe is the rot test (a
  healthy claude answers in a couple of seconds; a wedged one never returns).
  When it wedges, we *cure* by laying a fresh copy at a brand-new path — the
  wedge is path-keyed, so a never-before-used path is always healthy — and
  re-pointing to it.

The node's watchdog calls ``ensure_healthy_claude(force=True)`` on a slow tick
(proactive cure + upgrade pickup); any caller that sees claude time out
calls it too, so the *next* attempt uses a freshly-cured binary.

This is deliberately a sibling of :mod:`cortiva.core.claude_auth` — that module
is the one source of truth for *how to authenticate* a claude subprocess (the
OAuth token), this one is the source of truth for *which binary to exec so it
actually launches*. Spawn sites combine the two.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from cortiva.core.claude_auth import claude_oauth_env

logger = logging.getLogger(__name__)

_BINARY = "claude"

# Where ``claude`` is commonly installed. The fabric/node runs under launchd
# with a minimal PATH that omits /opt/homebrew/bin, so a bare ``claude`` (or
# ``shutil.which``) doesn't resolve even though it's installed — the same
# non-interactive-PATH gap that bit install.sh and the deep_think wrapper.
_SOURCE_SEARCH = (
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    os.path.expanduser("~/.claude/local/claude"),
    os.path.expanduser("~/.npm-global/bin/claude"),
    "/opt/homebrew/opt/node/bin/claude",
)

_MANAGED_DIR = Path.home() / ".cortiva" / "claude-bin"
_STATE_FILE = _MANAGED_DIR / "state.json"

# A healthy ``claude --version`` returns in ~2-3s; a path-wedged one never
# returns. Generous enough to never false-positive on a loaded box, short
# enough that a real wedge is cured within one watchdog tick.
_VERSION_TIMEOUT_S = 15.0

# How often to ask the package manager for a newer Claude Code. The stable cask
# moves about weekly; a few checks a day keeps a node current without making
# the watchdog chatty.
_UPGRADE_CHECK_S = 6 * 3600.0
# A cask upgrade downloads ~200MB.
_UPGRADE_TIMEOUT_S = 900.0
# The real headless probe: one tiny ``claude -p`` round-trip to the API.
_PROBE_TIMEOUT_S = 120.0
# A source build whose candidate failed its probes is not retried for this long
# (the failure may have been a transient API/auth outage, so do retry it).
_REJECT_RETRY_S = 24 * 3600.0
# Set to 0/false/no/off to stop the node upgrading Claude Code by itself.
_AUTO_UPGRADE_ENV = "CORTIVA_CLAUDE_AUTO_UPGRADE"

_LOCK = threading.Lock()
# Hot-path cache so claude_binary() is allocation-light and never spawns. The
# node-client (watchdog) and the fabric (spawn sites) are separate processes
# sharing one state file, so the cache is keyed on that file's mtime: when the
# other process promotes or rolls back a binary, this one notices on its next
# spawn instead of exec'ing the old copy until it restarts.
_cached_path: str | None = None
_cached_state_mtime: int = -1


# --------------------------------------------------------------------------
# source (upstream brew/install) resolution
# --------------------------------------------------------------------------
def _which() -> str | None:
    """The ``claude`` entry as found on PATH / known locations — typically the
    brew *symlink*, NOT its realpath. Used only to locate the upstream binary;
    we never modify it."""
    found = shutil.which(_BINARY)
    if found:
        return found
    for path in _SOURCE_SEARCH:
        if os.path.exists(path):
            return path
    return None


def _resolve_source() -> Path | None:
    """Realpath of the upstream claude binary (resolving the brew symlink), or
    None if claude genuinely isn't installed."""
    found = _which()
    if not found:
        return None
    return Path(os.path.realpath(found))


def _source_sig(source: Path) -> str:
    """Cheap brew-upgrade detector: the upstream binary's size + mtime. A
    ``brew upgrade`` lays down a new build (new path and/or new bytes), so this
    changes and we re-derive the managed copy."""
    st = source.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


# --------------------------------------------------------------------------
# managed-copy plumbing
# --------------------------------------------------------------------------
def _read_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    try:
        _MANAGED_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, _STATE_FILE)  # atomic
    except OSError:
        logger.debug("could not persist claude-bin state", exc_info=True)


def _state_mtime() -> int:
    try:
        return _STATE_FILE.stat().st_mtime_ns
    except OSError:
        return 0


def _remember(path: str) -> None:
    """Refresh the hot-path cache after a state write. Caller holds ``_LOCK``."""
    global _cached_path, _cached_state_mtime
    _cached_path = path
    _cached_state_mtime = _state_mtime()


def _lay_copy(source: Path, generation: int) -> str:
    """Copy the upstream binary to a brand-new path under the managed dir and
    return it. The filename carries a fresh nonce every time so the path string
    is *never reused* — the wedge is path-keyed, so each cure must land on a
    path that has never been executed before. ``shutil.copyfile`` copies bytes
    only (no xattrs), so the copy carries no ``com.apple.quarantine`` and skips
    Gatekeeper assessment."""
    _MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex[:8]
    dst = _MANAGED_DIR / f"claude-g{generation}-{nonce}"
    shutil.copyfile(source, dst)
    os.chmod(dst, 0o755)
    return str(dst)


def _prune_old(*keep: str | None) -> None:
    """Remove superseded managed copies (each is ~200MB), keeping the current
    one and the rollback target. A unix unlink of a file a process is still
    executing is safe — the inode lives until that process exits — so this
    never disturbs a claude that's mid-run."""
    try:
        for p in _MANAGED_DIR.glob("claude-g*"):
            if str(p) not in keep:
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _validate(path: str) -> bool:
    """The rot probe: a timed ``claude --version``. True iff it returns quickly
    with a version string. A path-wedged binary hangs here and trips the
    timeout — that is exactly the signal we cure on. ``--version`` needs no
    auth and no network, so a False is a genuine launch wedge, not a flaky API."""
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _version_of(path: str) -> str:
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _probe_headless(path: str) -> bool:
    """The promotion probe: one real headless ``claude -p`` call with the
    node's subscription token, exactly as agents call it. ``--version`` proves
    a build launches; only this proves it can still authenticate and complete a
    turn. Runs with a private ``CLAUDE_CONFIG_DIR`` so an untested build never
    touches (or migrates) any agent's or operator's Claude config."""
    probe_dir = _MANAGED_DIR / "probe"
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
        env = claude_oauth_env()
        env["CLAUDE_CONFIG_DIR"] = str(probe_dir / "config")
        proc = subprocess.run(
            [path, "-p", "Reply with the single word OK.", "--output-format", "json"]
            + ["--model", "haiku"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=str(probe_dir),
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        logger.warning("claude headless probe of %s did not complete", path)
        return False
    if proc.returncode != 0:
        logger.warning("claude headless probe of %s exited %d", path, proc.returncode)
        return False
    try:
        reply = json.loads(proc.stdout)
    except ValueError:
        return False
    if not isinstance(reply, dict):
        return False
    return not reply.get("is_error") and bool(str(reply.get("result") or "").strip())


def _auto_upgrade_enabled() -> bool:
    return os.environ.get(_AUTO_UPGRADE_ENV, "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _upgrade_command(source: Path) -> list[str] | None:
    """The command that moves the upstream install to its newest release, or
    None when this install isn't one we know how to upgrade. Only Homebrew
    casks today (what install.sh lays down on macOS): the source realpath is
    ``<prefix>/Caskroom/<cask>/<version>/claude``, and the cask token decides
    the channel — ``claude-code`` is stable, ``claude-code@latest`` is latest —
    so we upgrade whichever one is installed and never switch channel."""
    parts = source.parts
    if "Caskroom" not in parts:
        return None
    idx = parts.index("Caskroom")
    if idx + 1 >= len(parts) - 1:
        return None
    cask = parts[idx + 1]
    brew: str | None = str(Path(*parts[:idx]) / "bin" / "brew")
    if not os.path.exists(str(brew)):
        brew = shutil.which("brew")
    if not brew:
        return None
    return [brew, "upgrade", "--cask", cask]


def upgrade_claude_source(force: bool = False) -> bool:
    """Ask the package manager for a newer Claude Code. Returns True iff the
    upgrade command ran and succeeded (which includes "already up to date").

    This only moves the *upstream* install. The managed copy agents exec is
    untouched here; :func:`ensure_healthy_claude` sees the source signature
    change and promotes the new build only after it passes its probes.

    Throttled to once per ``_UPGRADE_CHECK_S`` across every process on the node
    (the stamp lives in the shared state file) unless ``force`` is set.
    """
    if not force and not _auto_upgrade_enabled():
        return False
    source = _resolve_source()
    if source is None:
        return False
    cmd = _upgrade_command(source)
    if cmd is None:
        return False
    with _LOCK:
        state = _read_state()
        last = float(state.get("upgrade_checked_at", 0.0))
        if not force and (time.time() - last) < _UPGRADE_CHECK_S:
            return False
        # Stamp before running so a concurrent caller doesn't start a second one.
        state["upgrade_checked_at"] = time.time()
        _write_state(state)
    env = dict(os.environ)
    env["HOMEBREW_NO_ENV_HINTS"] = "1"
    # Touch only the Claude Code cask: no `brew cleanup` sweep of the node's
    # other packages (python, neo4j) on the back of this upgrade.
    env["HOMEBREW_NO_INSTALL_CLEANUP"] = "1"
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_UPGRADE_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        logger.warning("claude source upgrade (%s) did not complete", " ".join(cmd))
        return False
    if proc.returncode != 0:
        logger.warning(
            "claude source upgrade (%s) exited %d: %s",
            " ".join(cmd),
            proc.returncode,
            (proc.stderr or proc.stdout).strip()[-400:],
        )
        return False
    return True


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def claude_binary() -> str:
    """Path to a claude binary the node should exec — cheap and spawn-free.

    Returns the node-managed copy (laying one down on first use), so callers
    never exec the brew/Caskroom path that is prone to the launch-wedge. Health
    *validation* and curing happen in :func:`ensure_healthy_claude` (driven by
    the node watchdog and failure handlers), not here, so this stays fast enough
    to call on every spawn.

    Falls back to the upstream path, then a bare ``"claude"``, if no managed
    copy can be laid (e.g. claude not installed) — preserving prior behaviour.
    """
    global _cached_path, _cached_state_mtime
    cached = _cached_path
    if cached and _cached_state_mtime == _state_mtime() and os.path.exists(cached):
        return cached
    # State moved (or first call): re-read it WITHOUT the lock, so a spawn never
    # queues behind a watchdog thread that is mid-probe on a candidate.
    mtime = _state_mtime()
    managed = _read_state().get("managed_path")
    if managed and os.path.exists(managed):
        _cached_path, _cached_state_mtime = str(managed), mtime
        return str(managed)
    with _LOCK:
        state = _read_state()
        managed = state.get("managed_path")
        if managed and os.path.exists(managed):
            _remember(managed)
            return str(managed)
        # No managed copy yet — lay one (a plain ~200MB copy, no probe).
        source = _resolve_source()
        if source is None:
            return _which() or _BINARY
        try:
            path = _lay_copy(source, 0)
        except OSError:
            logger.warning("could not lay managed claude copy; using source", exc_info=True)
            return str(source)
        _write_state(
            {
                "managed_path": path,
                "source_path": str(source),
                "source_sig": _source_sig(source),
                "generation": 0,
                "validated_at": 0.0,
                "version": "",
            }
        )
        _prune_old(path)
        _remember(path)
        return path


def ensure_healthy_claude(force: bool = False) -> str | None:
    """Make sure the node-managed claude binary actually launches; cure it if
    not. Returns the healthy managed path, or None if claude isn't installed.

    Handles, in one place:

    * **first run / missing copy** — lay a managed copy from the upstream binary;
    * **source upgrade** — on a slow tick, ask the package manager for a newer
      release (:func:`upgrade_claude_source`);
    * **new source build** — upstream (size, mtime) changed → lay it as a
      candidate, promote it only if ``--version`` and a real headless call both
      pass, keep the binary it replaces for rollback; otherwise stay put;
    * **rot** — a timed ``--version`` probe wedged → lay a fresh copy at a
      brand-new path (bumping the generation) and re-point to it.

    ``force=True`` always runs the ``--version`` probe; otherwise the probe is
    skipped on a recently-validated copy so this is cheap to call often.
    """
    # (0) keep the upstream install current. Outside the lock: a cask upgrade
    # downloads ~200MB and must never stall a spawn waiting on claude_binary().
    try:
        upgrade_claude_source()
    except Exception:
        logger.debug("claude source upgrade check failed", exc_info=True)

    with _LOCK:
        source = _resolve_source()
        if source is None:
            logger.debug("claude not installed — nothing to heal")
            return None

        state = _read_state()
        sig = _source_sig(source)
        managed = state.get("managed_path")
        generation = int(state.get("generation", 0))

        upgraded = state.get("source_sig") != sig
        missing = not (managed and os.path.exists(managed))

        # (1a) first run or a vanished copy → nothing to protect, lay from source
        if missing:
            generation = 0 if upgraded else generation
            path = _lay_copy(source, generation)
            ok = _validate(path)
            _commit(path, source, sig, generation, ok, state, keep_previous=True)
            if not ok:
                logger.error(
                    "freshly-laid claude copy %s failed --version "
                    "probe (claude itself may be broken)",
                    path,
                )
            return path

        # Past the missing branch, the managed copy exists on disk.
        assert isinstance(managed, str)

        # (1b) new source build → promote only a candidate that really works
        if upgraded and not _skipped(state, sig):
            promoted = _try_promote(source, sig, managed, state)
            if promoted:
                return promoted
            state = _read_state()  # the rejection was recorded

        # (2) recently validated and not forced → trust it, no spawn
        last = float(state.get("validated_at", 0.0))
        if not force and (time.time() - last) < _VERSION_TIMEOUT_S * 4:
            _remember(managed)
            return managed

        # (3) probe the current managed copy
        if _validate(managed):
            state["validated_at"] = time.time()
            _write_state(state)
            _remember(managed)
            return managed

        # (4) ROT — cure by laying a fresh copy at a never-used path
        logger.error(
            "claude at %s is WEDGED — `--version` did not return within %.0fs "
            "(the path-launch-wedge). Curing: laying a fresh copy at a new path "
            "(generation %d -> %d).",
            managed,
            _VERSION_TIMEOUT_S,
            generation,
            generation + 1,
        )
        generation += 1
        # Cure with the SAME build. If the source has moved on to a build we
        # declined (failed its probes, or rolled back from), copy the managed
        # binary's own bytes — the wedge is keyed on the path, not the bytes.
        same_build = state.get("source_sig") == sig
        path = _lay_copy(source if same_build else Path(managed), generation)
        ok = _validate(path)
        _commit(
            path,
            source if same_build else Path(str(state.get("source_path") or source)),
            str(state.get("source_sig") or sig),
            generation,
            ok,
            state,
            keep_previous=True,
        )
        if ok:
            logger.warning(
                "claude CURED — fresh copy at %s passes `--version`; "
                "voice-compose and dev sessions are healthy again.",
                path,
            )
        else:
            logger.error("claude STILL wedged after curing at %s — will retry next cycle.", path)
        return path


def _skipped(state: dict, sig: str) -> bool:
    """True while source build ``sig`` must not be adopted: its candidate
    failed the probes recently, or an operator rolled back from it."""
    if state.get("skip_sig") != sig:
        return False
    until = float(state.get("skip_until", 0.0))
    return until <= 0.0 or time.time() < until  # 0 = until the source changes


def _try_promote(source: Path, sig: str, managed: str, state: dict) -> str | None:
    """Lay the new source build as a candidate and promote it iff it passes
    ``--version`` and a real headless call. Returns the promoted path, or None
    (candidate deleted, current binary untouched). Caller holds ``_LOCK``."""
    logger.warning(
        "claude upstream changed: sig %s -> %s — probing a candidate copy of %s",
        state.get("source_sig"),
        sig,
        source,
    )
    candidate = _lay_copy(source, 0)
    if _validate(candidate) and _probe_headless(candidate):
        was = state.get("version", "")
        _commit(candidate, source, sig, 0, True, state, previous=managed)
        logger.warning(
            "claude UPGRADED %s -> %s — now at %s; previous binary kept at %s",
            was or "?",
            _read_state().get("version", "?"),
            candidate,
            managed,
        )
        return candidate
    try:
        os.unlink(candidate)
    except OSError:
        pass
    state["skip_sig"] = sig
    state["skip_until"] = time.time() + _REJECT_RETRY_S
    _write_state(state)
    logger.error(
        "claude candidate from %s FAILED its probes — staying on %s (%s); "
        "will retry this build in %.0fh",
        source,
        managed,
        state.get("version", "?"),
        _REJECT_RETRY_S / 3600,
    )
    return None


def rollback_claude() -> str | None:
    """Re-point the node at the binary the last upgrade replaced. Returns the
    path now in use, or None if there is no previous binary on disk.

    The build rolled back from is not re-adopted; the node moves forward again
    when the source next changes (the following release)."""
    with _LOCK:
        state = _read_state()
        current = state.get("managed_path")
        previous = state.get("previous_path")
        if not (previous and os.path.exists(previous)):
            return None
        logger.warning("claude ROLLED BACK %s -> %s", current, previous)
        _write_state(
            {
                **state,
                "managed_path": previous,
                "source_path": state.get("previous_source_path", ""),
                "source_sig": state.get("previous_source_sig", ""),
                "version": state.get("previous_version", ""),
                "generation": 0,
                "validated_at": 0.0,
                "previous_path": current,
                "previous_source_path": state.get("source_path", ""),
                "previous_source_sig": state.get("source_sig", ""),
                "previous_version": state.get("version", ""),
                "skip_sig": state.get("source_sig", ""),
                "skip_until": 0.0,
            }
        )
        _remember(previous)
        return str(previous)


def _commit(
    path: str,
    source: Path,
    sig: str,
    generation: int,
    ok: bool,
    state: dict,
    *,
    previous: str | None = None,
    keep_previous: bool = False,
) -> None:
    """Persist a new managed copy as the current one, prune the rest, refresh
    the hot-path cache. ``previous`` records the binary this one replaces as the
    rollback target; ``keep_previous`` carries the existing target over (a rot
    cure re-lays the same build, so the target doesn't change). Caller holds
    ``_LOCK``."""
    new: dict = {
        "managed_path": path,
        "source_path": str(source),
        "source_sig": sig,
        "generation": generation,
        "validated_at": time.time() if ok else 0.0,
        "version": _version_of(path) if ok else "",
    }
    for key in ("upgrade_checked_at", "skip_sig", "skip_until"):
        if key in state:
            new[key] = state[key]
    if previous:
        new["previous_path"] = previous
        new["previous_source_path"] = state.get("source_path", "")
        new["previous_source_sig"] = state.get("source_sig", "")
        new["previous_version"] = state.get("version", "")
    elif keep_previous:
        for key in (
            "previous_path",
            "previous_source_path",
            "previous_source_sig",
            "previous_version",
        ):
            if key in state:
                new[key] = state[key]
    _write_state(new)
    _prune_old(path, new.get("previous_path"))
    _remember(path)


def _main(argv: list[str]) -> int:
    """``python -m cortiva.core.claude_binary [status|upgrade|rollback]`` — the
    operator's handle on the same code path the watchdog runs."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    action = argv[0] if argv else "status"
    if action == "upgrade":
        upgrade_claude_source(force=True)
        ensure_healthy_claude(force=True)
    elif action == "rollback":
        if rollback_claude() is None:
            print("no previous claude binary to roll back to")
            return 1
    elif action != "status":
        print(_main.__doc__)
        return 2
    print(json.dumps(_read_state(), indent=2))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
