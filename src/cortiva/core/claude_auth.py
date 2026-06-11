"""Shared Claude Code OAuth-token wiring — one source of truth.

A background process (the fabric is a launchd LaunchAgent) cannot read Claude
Code's OAuth token from the macOS login Keychain: the read blocks on an
interactive unlock that never comes, so the ``claude`` call hangs and times
out. Every ``claude`` subprocess we spawn — ``deep_think``, the terminal
adapter, per-agent dev sessions — must therefore carry the long-lived
subscription token (from ``claude setup-token``) explicitly via
``CLAUDE_CODE_OAUTH_TOKEN``, which bypasses the keychain entirely.

This module is the single place that knows (a) where to find that token —
the process env first, else the file the node writes on connect — and (b) that
it must be stripped of ALL whitespace. An embedded newline (e.g. from a token
pasted out of a wrapped terminal) produces an invalid ``Authorization`` header
and a confusing 401, so we collapse every run of whitespace away.

Keep deep_think, the terminal adapter, and any future claude caller pointed at
``claude_oauth_env`` so the token-finding logic can never drift between them.
"""

from __future__ import annotations

import os
from pathlib import Path

_TOKEN_FILE = Path.home() / ".cortiva" / ".claude_oauth_token"


def _clean(value: str | None) -> str:
    """Collapse away ALL whitespace — tokens contain none, but a wrapped-paste
    newline would corrupt the Authorization header."""
    return "".join((value or "").split())


def claude_oauth_token() -> str:
    """Return the subscription OAuth token, or '' if none is configured.

    Env wins (HQ sets it on the node process; callers may set it per-call),
    falling back to the 0600 file the node writes from ``claude.config``.
    """
    from_env = _clean(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    if from_env:
        return from_env
    try:
        return _clean(_TOKEN_FILE.read_text(encoding="utf-8"))
    except OSError:
        return ""


def claude_oauth_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment dict with ``CLAUDE_CODE_OAUTH_TOKEN`` wired in.

    ``base`` is the environment to start from (defaults to the current
    process env). If no token can be found the base is returned unchanged —
    ``claude`` then falls back to its own auth (and may hang on the keychain),
    so a configured token is what makes headless, unattended claude work.
    """
    env = dict(base if base is not None else os.environ)
    token = claude_oauth_token()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env
