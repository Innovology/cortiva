"""Tests for the Codex terminal agent adapter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cortiva.adapters.protocols import ToolCapabilities
from cortiva.adapters.terminal.codex import CodexAdapter


class TestCodexAdapterInvoke:
    @pytest.mark.asyncio
    async def test_invoke_success_with_result_key(self, tmp_path: Path) -> None:
        """JSON output with 'result' key is extracted as content."""
        adapter = CodexAdapter()
        payload = json.dumps({"result": "task done", "files_changed": 3})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(payload.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await adapter.invoke("do task", tmp_path)

        assert resp.content == "task done"
        assert resp.is_error is False
        assert resp.metadata == {"files_changed": 3}
        assert resp.duration_seconds is not None

    @pytest.mark.asyncio
    async def test_invoke_success_with_output_key(self, tmp_path: Path) -> None:
        """JSON output with 'output' key (no 'result') is extracted as content."""
        adapter = CodexAdapter()
        payload = json.dumps({"output": "finished", "status": "ok"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(payload.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await adapter.invoke("do task", tmp_path)

        assert resp.content == "finished"
        assert resp.metadata == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_invoke_success_plain_text(self, tmp_path: Path) -> None:
        """Non-JSON output is returned as-is."""
        adapter = CodexAdapter()
        raw = "plain text output"

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(raw.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await adapter.invoke("do task", tmp_path)

        assert resp.content == raw
        assert resp.is_error is False
        assert resp.metadata == {}

    @pytest.mark.asyncio
    async def test_invoke_non_zero_exit_code(self, tmp_path: Path) -> None:
        """Non-zero exit code returns error response with stdout or stderr."""
        adapter = CodexAdapter()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"something went wrong"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await adapter.invoke("bad command", tmp_path)

        assert resp.is_error is True
        assert "something went wrong" in resp.content
        assert resp.duration_seconds is not None

    @pytest.mark.asyncio
    async def test_invoke_non_zero_exit_prefers_stdout(self, tmp_path: Path) -> None:
        """When both stdout and stderr exist on error, stdout is preferred."""
        adapter = CodexAdapter()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"stdout error info", b"stderr error info")
        )
        mock_proc.returncode = 2

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await adapter.invoke("bad command", tmp_path)

        assert resp.is_error is True
        assert resp.content == "stdout error info"

    @pytest.mark.asyncio
    async def test_invoke_file_not_found(self, tmp_path: Path) -> None:
        """FileNotFoundError gives a clear error."""
        adapter = CodexAdapter()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError,
        ):
            resp = await adapter.invoke("test", tmp_path)

        assert resp.is_error is True
        assert "not found" in resp.content

    @pytest.mark.asyncio
    async def test_invoke_timeout(self, tmp_path: Path) -> None:
        """Timeout kills process and returns error."""
        adapter = CodexAdapter(timeout=0.1)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=TimeoutError)
        mock_proc.kill = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await adapter.invoke("slow task", tmp_path)

        assert resp.is_error is True
        assert "timed out" in resp.content
        assert resp.duration_seconds == 0.1
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_invoke_env_passthrough(self, tmp_path: Path) -> None:
        """The env parameter is passed to subprocess."""
        adapter = CodexAdapter()
        payload = json.dumps({"result": "ok"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(payload.encode(), b""))
        mock_proc.returncode = 0
        captured_kwargs: list[dict] = []

        async def capture_exec(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return mock_proc

        custom_env = {"OPENAI_API_KEY": "sk-test", "PATH": "/usr/bin"}
        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await adapter.invoke("test", tmp_path, env=custom_env)

        assert captured_kwargs[0]["env"] is custom_env

    @pytest.mark.asyncio
    async def test_invoke_env_none_by_default(self, tmp_path: Path) -> None:
        """Without env param, None is passed (inherit parent env)."""
        adapter = CodexAdapter()
        payload = json.dumps({"result": "ok"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(payload.encode(), b""))
        mock_proc.returncode = 0
        captured_kwargs: list[dict] = []

        async def capture_exec(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await adapter.invoke("test", tmp_path)

        assert captured_kwargs[0]["env"] is None

    @pytest.mark.asyncio
    async def test_invoke_builds_command_with_model(self, tmp_path: Path) -> None:
        """Model flag is included when specified."""
        adapter = CodexAdapter(model="o3")
        payload = json.dumps({"result": "ok"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(payload.encode(), b""))
        mock_proc.returncode = 0
        captured_args: list[tuple] = []

        async def capture_exec(*args, **kwargs):
            captured_args.append(args)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await adapter.invoke("test", tmp_path)

        cmd = captured_args[0]
        assert "--model" in cmd
        assert "o3" in cmd

    @pytest.mark.asyncio
    async def test_invoke_builds_command_with_approval_mode(self, tmp_path: Path) -> None:
        """Approval mode is included in command."""
        adapter = CodexAdapter(approval_mode="full-auto")
        payload = json.dumps({"result": "ok"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(payload.encode(), b""))
        mock_proc.returncode = 0
        captured_args: list[tuple] = []

        async def capture_exec(*args, **kwargs):
            captured_args.append(args)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await adapter.invoke("test prompt", tmp_path)

        cmd = captured_args[0]
        assert "--approval-mode" in cmd
        assert "full-auto" in cmd
        # Prompt should be the last argument
        assert cmd[-1] == "test prompt"

    @pytest.mark.asyncio
    async def test_invoke_json_dict_without_result_or_output(self, tmp_path: Path) -> None:
        """JSON dict without result/output keys falls back to raw string."""
        adapter = CodexAdapter()
        payload = json.dumps({"status": "complete", "count": 42})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(payload.encode(), b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await adapter.invoke("test", tmp_path)

        assert resp.content == payload


class TestCodexAdapterAvailability:
    @pytest.mark.asyncio
    async def test_is_available_true(self) -> None:
        adapter = CodexAdapter()
        adapter._which_cache = None
        with patch("shutil.which", return_value="/usr/local/bin/codex"):
            result = await adapter.is_available()
        assert result is True

    @pytest.mark.asyncio
    async def test_is_available_false(self) -> None:
        adapter = CodexAdapter()
        adapter._which_cache = None
        with patch("shutil.which", return_value=None):
            result = await adapter.is_available()
        assert result is False

    @pytest.mark.asyncio
    async def test_is_available_caches(self) -> None:
        adapter = CodexAdapter()
        adapter._which_cache = None
        with patch("shutil.which", return_value="/usr/local/bin/codex") as mock_which:
            await adapter.is_available()
            await adapter.is_available()
        # Should only call which once due to caching
        mock_which.assert_called_once()

    @pytest.mark.asyncio
    async def test_capabilities(self) -> None:
        adapter = CodexAdapter()
        caps = await adapter.capabilities()
        assert isinstance(caps, ToolCapabilities)
        assert caps.can_edit_files is True
        assert caps.can_run_bash is True
        assert caps.can_use_mcp is False
        assert "file_edit" in caps.supported_tools
        assert "shell" in caps.supported_tools
