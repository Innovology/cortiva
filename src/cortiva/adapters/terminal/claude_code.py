"""Claude Code terminal agent adapter."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from cortiva.adapters.protocols import AgentResponse, ToolCapabilities


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
    ) -> AgentResponse:
        """Invoke Claude Code CLI with a prompt."""
        cmd: list[str] = ["claude", "-p", prompt, "--output-format", output_format]
        if self._model:
            cmd.extend(["--model", self._model])
        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allowedTools", tool])
        if max_turns is not None:
            cmd.extend(["--max-turns", str(max_turns)])

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
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
                metadata = {
                    k: v for k, v in parsed.items() if k != "result"
                }
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
        """Check if the ``claude`` binary is on PATH."""
        if self._which_cache is None:
            self._which_cache = shutil.which("claude") is not None
        return self._which_cache

    async def capabilities(self) -> ToolCapabilities:
        return ToolCapabilities(
            can_edit_files=True,
            can_run_bash=True,
            can_use_mcp=True,
            supported_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            max_turns=None,
        )
