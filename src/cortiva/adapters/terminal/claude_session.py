"""Interactive, steerable Claude Code session (stream-json).

This is the primitive the cognitive architecture sits on: instead of firing
``claude -p "<prompt>"`` and blocking until a verdict, an agent *drives* a live
session — it watches every step Claude takes (its narration, each tool call,
each result) as a stream of checkpoints, and can inject steering messages
mid-run to redirect, challenge, or stop the work. The agent (Myelin, on the
local model) holds the intent and decides *when* to intervene; the heavy work
and any verification are Claude's.

Mechanics (proven against the node CLI):

    claude -p --input-format stream-json --output-format stream-json --verbose

- stdout: newline-delimited JSON events — ``system/init``, ``rate_limit_event``,
  ``assistant`` (text and/or ``tool_use`` blocks), ``user`` (``tool_result``),
  ``result`` (final, carries ``session_id``/``is_error``).
- stdin: newline-delimited user messages — send the first to start, send more
  to steer while it runs (stdin stays open until ``close()``).

This driver classifies the raw stream into :class:`Checkpoint` s so the caller
reasons about decision points ("about to do something destructive", "hit an
error", "done") rather than parsing JSON. It does NOT decide anything itself —
the steering judgment lives in the agent's cognition.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from cortiva.core.claude_auth import claude_oauth_env

logger = logging.getLogger(__name__)

_BINARY = "claude"

# Tool calls whose inputs look like they mutate the world irreversibly — these
# are the checkpoints an agent most wants to catch before they land.
_DESTRUCTIVE_HINTS = (
    "rm -rf",
    "rm -r",
    "git push",
    "git reset --hard",
    "git clean",
    "drop table",
    "drop database",
    "truncate",
    "delete from",
    "force-push",
    "--force",
    "shutdown",
    "kill -9",
)


class Checkpoint(str, Enum):
    """A classified decision point in a session's stream."""

    INIT = "init"  # session started (tools/model known)
    NARRATION = "narration"  # assistant text — a plan, a thought, progress
    TOOL = "tool"  # about to use a tool
    DESTRUCTIVE = "destructive"  # a tool call that looks irreversible
    TOOL_RESULT = "tool_result"  # a tool returned
    RATE_LIMIT = "rate_limit"  # backoff signal (for the governor)
    ERROR = "error"  # something went wrong
    DONE = "done"  # the session finished (carries result)


@dataclass
class SessionEvent:
    checkpoint: Checkpoint
    raw: dict[str, Any]
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    is_error: bool = False


def _looks_destructive(tool_name: str, tool_input: dict[str, Any]) -> bool:
    blob = (tool_name + " " + json.dumps(tool_input, default=str)).lower()
    return any(h in blob for h in _DESTRUCTIVE_HINTS)


class ClaudeSession:
    """One live, steerable Claude Code session.

    Usage::

        s = ClaudeSession(cwd=repo, model="opus")
        await s.start("Investigate the failing CI and fix it.")
        async for ev in s.events():
            if ev.checkpoint is Checkpoint.DESTRUCTIVE:
                await s.steer("Hold on — run the tests before that.")
            if ev.checkpoint is Checkpoint.DONE:
                break
        await s.close()
    """

    def __init__(
        self,
        *,
        cwd: Path,
        model: str | None = None,
        env: dict[str, str] | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int | None = None,
        resume: str | None = None,
        mcp_config: Path | None = None,
    ) -> None:
        self._cwd = cwd
        self._model = model
        self._env = env
        self._allowed_tools = allowed_tools
        self._max_turns = max_turns
        self._resume = resume
        self._mcp_config = mcp_config
        self._proc: asyncio.subprocess.Process | None = None
        self.session_id: str = ""
        self.started_at: float = 0.0

    def _build_cmd(self) -> list[str]:
        cmd = [
            _BINARY,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self._resume:
            cmd += ["--resume", self._resume]
        if self._model:
            cmd += ["--model", self._model]
        if self._allowed_tools:
            for t in self._allowed_tools:
                cmd += ["--allowedTools", t]
        else:
            cmd.append("--dangerously-skip-permissions")
        if self._max_turns is not None:
            cmd += ["--max-turns", str(self._max_turns)]
        mcp = self._mcp_config if self._mcp_config is not None else (self._cwd / ".mcp.json")
        if mcp.exists():
            cmd += ["--mcp-config", str(mcp)]
        return cmd

    async def start(self, prompt: str) -> None:
        """Launch the session and send the opening message."""
        self.started_at = time.monotonic()
        self._proc = await asyncio.create_subprocess_exec(
            *self._build_cmd(),
            cwd=str(self._cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=claude_oauth_env(self._env),
        )
        await self._send(prompt)

    async def _send(self, text: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("session not started")
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()

    async def steer(self, text: str) -> None:
        """Inject a steering message mid-session — the agent redirecting the
        work it's watching (Slot B's critique, a course-correction, a stop)."""
        await self._send(text)
        logger.info("steered session %s: %s", self.session_id[:8], text[:80])

    async def events(self) -> AsyncIterator[SessionEvent]:
        """Yield classified checkpoints as the session works."""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                e = json.loads(s)
            except ValueError:
                continue
            for ev in self._classify(e):
                if ev.session_id:
                    self.session_id = ev.session_id
                yield ev
                if ev.checkpoint is Checkpoint.DONE:
                    return

    def _classify(self, e: dict[str, Any]) -> list[SessionEvent]:
        t = e.get("type")
        sid = e.get("session_id", "") or ""
        if t == "system":
            return [SessionEvent(Checkpoint.INIT, e, session_id=sid)]
        if t == "rate_limit_event":
            return [SessionEvent(Checkpoint.RATE_LIMIT, e, session_id=sid)]
        if t == "result":
            return [
                SessionEvent(
                    Checkpoint.DONE,
                    e,
                    text=str(e.get("result") or ""),
                    session_id=sid,
                    is_error=bool(e.get("is_error")),
                )
            ]
        if t == "assistant":
            out: list[SessionEvent] = []
            for b in e.get("message", {}).get("content", []) or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    name = b.get("name", "")
                    inp = b.get("input", {}) or {}
                    cp = (
                        Checkpoint.DESTRUCTIVE if _looks_destructive(name, inp) else Checkpoint.TOOL
                    )
                    out.append(
                        SessionEvent(
                            cp,
                            e,
                            tool_name=name,
                            tool_input=inp,
                            session_id=sid,
                        )
                    )
                elif b.get("type") == "text" and b.get("text", "").strip():
                    out.append(
                        SessionEvent(
                            Checkpoint.NARRATION,
                            e,
                            text=b["text"],
                            session_id=sid,
                        )
                    )
            return out
        if t == "user":
            for b in e.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    return [
                        SessionEvent(
                            Checkpoint.TOOL_RESULT,
                            e,
                            is_error=bool(b.get("is_error")),
                            session_id=sid,
                        )
                    ]
        return []

    async def close(self, *, kill: bool = False) -> str:
        """End the session. ``kill=True`` aborts immediately; otherwise close
        stdin and let the current turn finish. Returns stderr tail (diagnostics)."""
        if self._proc is None:
            return ""
        try:
            if kill:
                self._proc.kill()
            else:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            err = b""
            try:
                err = (
                    await asyncio.wait_for(self._proc.stderr.read(), timeout=5)
                    if self._proc.stderr
                    else b""
                )
            except (TimeoutError, Exception):
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except TimeoutError:
                self._proc.kill()
            return err.decode("utf-8", "replace")[-500:]
        finally:
            self._proc = None
