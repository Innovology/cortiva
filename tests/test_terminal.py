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
                "test",
                tmp_path,
                allowed_tools=["Read", "Write"],
                max_turns=5,
            )

        cmd = calls[0]
        assert "--allowedTools" in cmd
        assert "--max-turns" in cmd
        assert "5" in cmd

    @pytest.mark.asyncio
    async def test_invoke_resumes_session(self, tmp_path: Path) -> None:
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
            await adapter.invoke("test", tmp_path, resume_session="sess-123")

        cmd = calls[0]
        assert "--resume" in cmd
        assert "sess-123" in cmd

    @pytest.mark.asyncio
    async def test_invoke_injects_oauth_token(self, tmp_path: Path) -> None:
        # Regression: the terminal path must carry CLAUDE_CODE_OAUTH_TOKEN so
        # the background fabric never hangs on the macOS keychain (0 terminal
        # completions / 54 timeouts before this was wired in).
        adapter = ClaudeCodeAdapter()
        result_json = json.dumps({"result": "ok"})

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
        mock_proc.returncode = 0
        seen_env: dict = {}

        async def capture_exec(*args, **kwargs):
            seen_env.update(kwargs.get("env") or {})
            return mock_proc

        with (
            patch("asyncio.create_subprocess_exec", side_effect=capture_exec),
            patch(
                "cortiva.core.claude_auth.claude_oauth_token",
                return_value="tok-abc",
            ),
        ):
            await adapter.invoke("test", tmp_path, env={"PATH": "/usr/bin"})

        assert seen_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-abc"
        assert seen_env.get("PATH") == "/usr/bin"  # base env preserved

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
    async def test_invoke_binary_not_found(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        adapter._which_cache = None
        result = await adapter.invoke("test", tmp_path)
        # If codex isn't installed, we get an error response
        assert result.is_error is True
        assert "not found" in result.content or "timed out" in result.content

    @pytest.mark.asyncio
    async def test_capabilities(self) -> None:
        adapter = CodexAdapter()
        caps = await adapter.capabilities()
        assert isinstance(caps, ToolCapabilities)
        assert caps.can_edit_files is True
        assert caps.can_run_bash is True

    def test_init_params(self) -> None:
        adapter = CodexAdapter(timeout=60.0, model="o3", approval_mode="full-auto")
        assert adapter._timeout == 60.0
        assert adapter._model == "o3"
        assert adapter._approval_mode == "full-auto"


class TestAiderAdapter:
    @pytest.mark.asyncio
    async def test_invoke_binary_not_found(self, tmp_path: Path) -> None:
        adapter = AiderAdapter()
        adapter._which_cache = None
        result = await adapter.invoke("test", tmp_path)
        assert result.is_error is True
        assert "not found" in result.content or "timed out" in result.content

    @pytest.mark.asyncio
    async def test_capabilities(self) -> None:
        adapter = AiderAdapter()
        caps = await adapter.capabilities()
        assert isinstance(caps, ToolCapabilities)
        assert caps.can_edit_files is True
        assert caps.can_run_bash is False

    def test_init_params(self) -> None:
        adapter = AiderAdapter(timeout=120.0, model="gpt-4o", auto_commits=True)
        assert adapter._timeout == 120.0
        assert adapter._model == "gpt-4o"
        assert adapter._auto_commits is True


class TestPermissionTranslation:
    """ToolPolicy 'None = unrestricted' must reach headless claude as
    actual permission — with no flags, claude -p denies every tool
    ('This command requires approval') and agents can't act."""

    @pytest.mark.asyncio
    async def test_unrestricted_policy_skips_permission_gate(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = ClaudeCodeAdapter()
        captured: dict = {}

        async def fake_exec(*cmd, **kwargs):
            captured["cmd"] = list(cmd)

            class _P:
                returncode = 0

                async def communicate(self):
                    return (b'{"result": "ok"}', b"")

            return _P()

        with patch("asyncio.create_subprocess_exec", fake_exec):
            await adapter.invoke("do work", tmp_path, allowed_tools=None)
        assert "--dangerously-skip-permissions" in captured["cmd"]
        assert "--allowedTools" not in captured["cmd"]

    @pytest.mark.asyncio
    async def test_restricted_policy_uses_allowed_tools(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = ClaudeCodeAdapter()
        captured: dict = {}

        async def fake_exec(*cmd, **kwargs):
            captured["cmd"] = list(cmd)

            class _P:
                returncode = 0

                async def communicate(self):
                    return (b'{"result": "ok"}', b"")

            return _P()

        with patch("asyncio.create_subprocess_exec", fake_exec):
            await adapter.invoke(
                "do work",
                tmp_path,
                allowed_tools=["Read", "Grep"],
            )
        assert "--dangerously-skip-permissions" not in captured["cmd"]
        assert captured["cmd"].count("--allowedTools") == 2
