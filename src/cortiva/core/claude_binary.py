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
* **Brew upgrades.** The upstream binary's (size, mtime) is the source
  signature. When it changes — i.e. ``brew upgrade claude-code`` laid down a new
  build — :func:`ensure_healthy_claude` re-derives the managed copy from the new
  source automatically.
* **Rot detection + cure.** A timed ``--version`` probe is the rot test (a
  healthy claude answers in a couple of seconds; a wedged one never returns).
  When it wedges, we *cure* by laying a fresh copy at a brand-new path — the
  wedge is path-keyed, so a never-before-used path is always healthy — and
  re-pointing to it.

The node's watchdog calls ``ensure_healthy_claude(force=True)`` on a slow tick
(proactive cure + brew-upgrade pickup); any caller that sees claude time out
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

_LOCK = threading.Lock()
# Hot-path cache so claude_binary() is allocation-light and never spawns.
_cached_path: str | None = None


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


def _prune_old(keep: str) -> None:
    """Remove superseded managed copies (each is ~200MB). A unix unlink of a
    file a process is still executing is safe — the inode lives until that
    process exits — so this never disturbs a claude that's mid-run."""
    try:
        for p in _MANAGED_DIR.glob("claude-g*"):
            if str(p) != keep:
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
            [path, "--version"], capture_output=True, text=True,
            timeout=_VERSION_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


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
    global _cached_path
    cached = _cached_path
    if cached and os.path.exists(cached):
        return cached
    with _LOCK:
        state = _read_state()
        managed = state.get("managed_path")
        if managed and os.path.exists(managed):
            _cached_path = managed
            return managed
        # No managed copy yet — lay one (a plain ~200MB copy, no probe).
        source = _resolve_source()
        if source is None:
            return _which() or _BINARY
        try:
            path = _lay_copy(source, 0)
        except OSError:
            logger.warning("could not lay managed claude copy; using source",
                           exc_info=True)
            return str(source)
        _write_state({
            "managed_path": path,
            "source_path": str(source),
            "source_sig": _source_sig(source),
            "generation": 0,
            "validated_at": 0.0,
            "version": "",
        })
        _prune_old(path)
        _cached_path = path
        return path


def ensure_healthy_claude(force: bool = False) -> str | None:
    """Make sure the node-managed claude binary actually launches; cure it if
    not. Returns the healthy managed path, or None if claude isn't installed.

    Handles, in one place:

    * **first run / missing copy** — lay a managed copy from the upstream binary;
    * **brew upgrade** — upstream (size, mtime) changed → re-derive from the new
      source (and reset the generation, since the new source gets a new path);
    * **rot** — a timed ``--version`` probe wedged → lay a fresh copy at a
      brand-new path (bumping the generation) and re-point to it.

    ``force=True`` always runs the ``--version`` probe; otherwise the probe is
    skipped on a recently-validated copy so this is cheap to call often.
    """
    global _cached_path
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

        # (1) brew upgrade, first run, or a vanished copy → lay fresh from source
        if upgraded or missing:
            if upgraded and not missing:
                logger.warning(
                    "claude upstream changed (brew upgrade?): sig %s -> %s — "
                    "re-laying managed copy from %s",
                    state.get("source_sig"), sig, source,
                )
            generation = 0 if upgraded else generation
            path = _lay_copy(source, generation)
            ok = _validate(path)
            _commit(path, source, sig, generation, ok)
            if not ok:
                logger.error("freshly-laid claude copy %s failed --version "
                             "probe (claude itself may be broken)", path)
            return path

        # Past the missing/upgraded branch, the managed copy exists on disk.
        assert isinstance(managed, str)

        # (2) recently validated and not forced → trust it, no spawn
        last = float(state.get("validated_at", 0.0))
        if not force and (time.time() - last) < _VERSION_TIMEOUT_S * 4:
            _cached_path = managed
            return managed

        # (3) probe the current managed copy
        if _validate(managed):
            state["validated_at"] = time.time()
            _write_state(state)
            _cached_path = managed
            return managed

        # (4) ROT — cure by laying a fresh copy at a never-used path
        logger.error(
            "claude at %s is WEDGED — `--version` did not return within %.0fs "
            "(the path-launch-wedge). Curing: laying a fresh copy at a new path "
            "(generation %d -> %d).",
            managed, _VERSION_TIMEOUT_S, generation, generation + 1,
        )
        generation += 1
        path = _lay_copy(source, generation)
        ok = _validate(path)
        _commit(path, source, sig, generation, ok)
        if ok:
            logger.warning("claude CURED — fresh copy at %s passes `--version`; "
                           "voice-compose and dev sessions are healthy again.", path)
        else:
            logger.error("claude STILL wedged after curing at %s — will retry "
                         "next cycle.", path)
        return path


def _commit(path: str, source: Path, sig: str, generation: int, ok: bool) -> None:
    """Persist a new managed copy as the current one, prune the old, refresh the
    hot-path cache. Caller holds ``_LOCK``."""
    global _cached_path
    _write_state({
        "managed_path": path,
        "source_path": str(source),
        "source_sig": sig,
        "generation": generation,
        "validated_at": time.time() if ok else 0.0,
        "version": _version_of(path) if ok else "",
    })
    _prune_old(path)
    _cached_path = path
