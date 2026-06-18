"""Claude Code terminal agent adapter."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from cortiva.adapters.protocols import AgentResponse, ToolCapabilities
from cortiva.core.claude_auth import claude_oauth_env
from cortiva.core.claude_binary import claude_binary


class ClaudeCodeAdapter:
    """Runs ``claude -p <prompt> --output-format json`` as a subprocess."""

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        model: str | None = None,
    ) -> None:
        self._timeout = timeout
        self._model = model
        self._which_cache: bool | None = None

    async def invoke(
        self,
        prompt: str,
        cwd: Path,
        *,
        output_format: str = "json",
        allowed_tools: list[str] | None = None,
        max_turns: int | None = None,
        env: dict[str, str] | None = None,
        resume_session: str | None = None,
    ) -> AgentResponse:
        """Invoke Claude Code CLI with a prompt.

        ``resume_session`` continues a prior session id (captured from a
        previous invoke's ``session_id``) so the agent keeps a single, growing
        Claude Code conversation about its own work — its persistent dev
        session — instead of a cold context every task.
        """
        # claude_binary() returns a node-managed copy at a launchable path,
        # never the brew/Caskroom path that is prone to the startup wedge.
        cmd: list[str] = [claude_binary(), "-p", prompt, "--output-format", output_format]
        if resume_session:
            cmd.extend(["--resume", resume_session])
        if self._model:
            cmd.extend(["--model", self._model])
        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allowedTools", tool])
        else:
            # Policy semantics: allowed_tools=None means "no
            # restrictions" (ToolPolicy.effective_allowed). Headless
            # claude is the OPPOSITE — with no flags every tool call
            # returns "This command requires approval" and the agent
            # can't touch the world (this blocked the CPO's first
            # GitHub audit, 2026-06-07). Translate faithfully: an
            # unrestricted policy grants full permission inside the
            # agent's own workspace.
            cmd.append("--dangerously-skip-permissions")
        if max_turns is not None:
            cmd.extend(["--max-turns", str(max_turns)])

        # If the agent's workspace has a provisioned MCP config (e.g. the
        # Playwright browser server the dev-env provisioner writes), load it so
        # the session gains browser_navigate/screenshot tools and can drive the
        # live product. Project-scoped, picked up only when the file exists.
        mcp_config = cwd / ".mcp.json"
        if mcp_config.exists():
            cmd.extend(["--mcp-config", str(mcp_config)])

        # Wire the subscription OAuth token into the subprocess env. The
        # fabric is a background LaunchAgent and cannot read claude's token
        # from the macOS Keychain — without this the call hangs and times out
        # (0 terminal completions / 54 timeouts observed before this fix). The
        # deep_think wrapper already does this; the terminal path did not, so
        # every agent's hands-on execution silently died on the keychain.
        env = claude_oauth_env(env)

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,  # non-interactive: never block on stdin
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except FileNotFoundError:
            return AgentResponse(
                content="claude CLI not found",
                is_error=True,
            )
        except TimeoutError:
            proc.kill()
            return AgentResponse(
                content="claude CLI timed out",
                is_error=True,
                duration_seconds=self._timeout,
            )

        duration = time.monotonic() - start
        raw = stdout.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            return AgentResponse(
                content=raw or stderr.decode("utf-8", errors="replace"),
                is_error=True,
                duration_seconds=duration,
            )

        # Try to parse JSON output
        metadata: dict[str, Any] = {}
        content = raw
        if output_format == "json":
            try:
                parsed = json.loads(raw)
                content = parsed.get("result", raw)
                metadata = {k: v for k, v in parsed.items() if k != "result"}
            except (json.JSONDecodeError, TypeError):
                pass

        return AgentResponse(
            content=content,
            output_format=output_format,
            duration_seconds=duration,
            session_id=metadata.get("session_id"),
            cost_usd=metadata.get("cost_usd"),
            metadata=metadata,
        )

    async def is_available(self) -> bool:
        """Check if the ``claude`` binary is installed (PATH or known location)."""
        if self._which_cache is None:
            from cortiva.core.claude_binary import _resolve_source

            self._which_cache = _resolve_source() is not None
        return self._which_cache

    async def capabilities(self) -> ToolCapabilities:
        return ToolCapabilities(
            can_edit_files=True,
            can_run_bash=True,
            can_use_mcp=True,
            supported_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            max_turns=None,
        )
