"""Aider terminal agent adapter."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

from cortiva.adapters.protocols import AgentResponse, ToolCapabilities


class AiderAdapter:
    """Runs ``aider --message <prompt> --yes`` as a subprocess.

    Aider is a CLI tool for AI-assisted code editing.  The ``--message``
    flag provides a non-interactive prompt and ``--yes`` auto-accepts changes.
    """

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        model: str | None = None,
        auto_commits: bool = False,
    ) -> None:
        self._timeout = timeout
        self._model = model
        self._auto_commits = auto_commits
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
        """Invoke Aider CLI with a prompt."""
        cmd: list[str] = [
            "aider",
            "--message", prompt,
            "--yes",
        ]
        if self._model:
            cmd.extend(["--model", self._model])
        if not self._auto_commits:
            cmd.append("--no-auto-commits")

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
                content="aider CLI not found",
                is_error=True,
            )
        except TimeoutError:
            proc.kill()
            return AgentResponse(
                content="aider CLI timed out",
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

        return AgentResponse(
            content=raw,
            output_format="text",
            duration_seconds=duration,
        )

    async def is_available(self) -> bool:
        """Check if the ``aider`` binary is on PATH."""
        if self._which_cache is None:
            self._which_cache = shutil.which("aider") is not None
        return self._which_cache

    async def capabilities(self) -> ToolCapabilities:
        return ToolCapabilities(
            can_edit_files=True,
            can_run_bash=False,
            can_use_mcp=False,
            supported_tools=["file_edit"],
            max_turns=None,
        )
