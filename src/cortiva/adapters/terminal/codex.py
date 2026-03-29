"""Codex terminal agent adapter."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from cortiva.adapters.protocols import AgentResponse, ToolCapabilities


class CodexAdapter:
    """Runs ``codex --quiet --json <prompt>`` as a subprocess.

    OpenAI Codex CLI writes files and runs commands autonomously.
    The ``--json`` flag requests structured JSON output.
    """

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        model: str | None = None,
        approval_mode: str = "auto-edit",
    ) -> None:
        self._timeout = timeout
        self._model = model
        self._approval_mode = approval_mode
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
    ) -> AgentResponse:
        """Invoke Codex CLI with a prompt."""
        cmd: list[str] = [
            "codex",
            "--quiet",
            "--approval-mode", self._approval_mode,
        ]
        if self._model:
            cmd.extend(["--model", self._model])
        cmd.append(prompt)

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except FileNotFoundError:
            return AgentResponse(
                content="codex CLI not found",
                is_error=True,
            )
        except TimeoutError:
            proc.kill()
            return AgentResponse(
                content="codex CLI timed out",
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
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                content = parsed.get("result", parsed.get("output", raw))
                metadata = {
                    k: v for k, v in parsed.items()
                    if k not in ("result", "output")
                }
        except (json.JSONDecodeError, TypeError):
            pass

        return AgentResponse(
            content=content,
            output_format=output_format,
            duration_seconds=duration,
            metadata=metadata,
        )

    async def is_available(self) -> bool:
        """Check if the ``codex`` binary is on PATH."""
        if self._which_cache is None:
            self._which_cache = shutil.which("codex") is not None
        return self._which_cache

    async def capabilities(self) -> ToolCapabilities:
        return ToolCapabilities(
            can_edit_files=True,
            can_run_bash=True,
            can_use_mcp=False,
            supported_tools=["file_edit", "shell"],
            max_turns=None,
        )
