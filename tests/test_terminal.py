"""Tests for the terminal agent adapters."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cortiva.adapters.protocols import ToolCapabilities
from cortiva.adapters.terminal.aider import AiderAdapter
from cortiva.adapters.terminal.claude_code import ClaudeCodeAdapter
from cortiva.adapters.terminal.codex import CodexAdapter


class TestClaudeCodeAdapter:
    def test_init_defaults(self) -> None:
        adapter = ClaudeCodeAdapter()
        assert adapter._timeout == 300.0
        assert adapter._model is None

    def test_init_with_options(self) -> None:
        adapter = ClaudeCodeAdapter(timeout=60.0, model="opus")
        assert adapter._timeout == 60.0
        assert adapter._model == "opus"

    @pytest.mark.asyncio
    async def test_invoke_success(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()
        result_json = json.dumps({"result": "Hello from Claude"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await adapter.invoke("say hello", tmp_path)

        assert resp.content == "Hello from Claude"
        assert resp.is_error is False
        assert resp.duration_seconds is not None

    @pytest.mark.asyncio
    async def test_invoke_non_zero_exit(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error occurred"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await adapter.invoke("bad command", tmp_path)

        assert resp.is_error is True

    @pytest.mark.asyncio
    async def test_invoke_not_found(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError,
        ):
            resp = await adapter.invoke("hello", tmp_path)

        assert resp.is_error is True
        assert "not found" in resp.content

    @pytest.mark.asyncio
    async def test_invoke_timeout(self, tmp_path: Path) -> None:
        import asyncio

        adapter = ClaudeCodeAdapter(timeout=0.1)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.kill = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await adapter.invoke("slow command", tmp_path)

        assert resp.is_error is True
        assert "timed out" in resp.content

    @pytest.mark.asyncio
    async def test_invoke_builds_command_with_model(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter(model="opus")
        result_json = json.dumps({"result": "ok"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
        mock_proc.returncode = 0
        calls = []

        async def capture_exec(*args, **kwargs):
            calls.append(args)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await adapter.invoke("test", tmp_path)

        cmd = calls[0]
        assert "--model" in cmd
        assert "opus" in cmd

    @pytest.mark.asyncio
    async def test_invoke_with_allowed_tools_and_max_turns(self, tmp_path: Path) -> None:
        adapter = ClaudeCodeAdapter()
        result_json = json.dumps({"result": "ok"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
        mock_proc.returncode = 0
        calls = []

        async def capture_exec(*args, **kwargs):
            calls.append(args)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await adapter.invoke(
                "test", tmp_path,
                allowed_tools=["Read", "Write"],
                max_turns=5,
            )

        cmd = calls[0]
        assert "--allowedTools" in cmd
        assert "--max-turns" in cmd
        assert "5" in cmd

    @pytest.mark.asyncio
    async def test_is_available_true(self) -> None:
        adapter = ClaudeCodeAdapter()
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            result = await adapter.is_available()
        assert result is True

    @pytest.mark.asyncio
    async def test_is_available_false(self) -> None:
        adapter = ClaudeCodeAdapter()
        with patch("shutil.which", return_value=None):
            result = await adapter.is_available()
        assert result is False

    @pytest.mark.asyncio
    async def test_capabilities(self) -> None:
        adapter = ClaudeCodeAdapter()
        caps = await adapter.capabilities()
        assert isinstance(caps, ToolCapabilities)
        assert caps.can_edit_files is True
        assert caps.can_run_bash is True
        assert caps.can_use_mcp is True
        assert len(caps.supported_tools) > 0


class TestCodexAdapter:
    @pytest.mark.asyncio
    async def test_invoke_raises_not_implemented(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.invoke("test", tmp_path)

    @pytest.mark.asyncio
    async def test_is_available_false(self) -> None:
        adapter = CodexAdapter()
        assert await adapter.is_available() is False

    @pytest.mark.asyncio
    async def test_capabilities(self) -> None:
        adapter = CodexAdapter()
        caps = await adapter.capabilities()
        assert isinstance(caps, ToolCapabilities)


class TestAiderAdapter:
    @pytest.mark.asyncio
    async def test_invoke_raises_not_implemented(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.invoke("test", tmp_path)

    @pytest.mark.asyncio
    async def test_is_available_false(self) -> None:
        adapter = AiderAdapter()
        assert await adapter.is_available() is False

    @pytest.mark.asyncio
    async def test_capabilities(self) -> None:
        adapter = AiderAdapter()
        caps = await adapter.capabilities()
        assert isinstance(caps, ToolCapabilities)
