"""Tests for the three-tier agent isolation system."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cortiva.core.isolation import (
    ContainerConfig,
    ContainerIsolation,
    IsolationConfig,
    IsolationTier,
    NoIsolation,
    OSIsolation,
    SoftIsolation,
    SubprocessEnvelope,
    build_enforcer,
)
from cortiva.core.memory_guard import GuardedMemoryAdapter


# ---------------------------------------------------------------------------
# IsolationConfig
# ---------------------------------------------------------------------------


class TestIsolationConfig:
    def test_from_dict_defaults(self) -> None:
        config = IsolationConfig.from_dict({})
        assert config.tier == IsolationTier.NONE
        assert "PATH" in config.allowed_env

    def test_from_dict_soft(self) -> None:
        config = IsolationConfig.from_dict({"tier": "soft"})
        assert config.tier == IsolationTier.SOFT

    def test_from_dict_container(self) -> None:
        config = IsolationConfig.from_dict({
            "tier": "container",
            "container": {
                "runtime": "podman",
                "cpu_limit": "2.0",
                "memory_limit": "1g",
                "network": "bridge",
                "image": "ubuntu:24.04",
            },
        })
        assert config.tier == IsolationTier.CONTAINER
        assert config.container.runtime == "podman"
        assert config.container.cpu_limit == "2.0"
        assert config.container.memory_limit == "1g"
        assert config.container.network == "bridge"
        assert config.container.image == "ubuntu:24.04"

    def test_from_dict_custom_allowed_env(self) -> None:
        config = IsolationConfig.from_dict({
            "tier": "os",
            "allowed_env": ["PATH", "CUSTOM_VAR"],
        })
        assert config.allowed_env == ["PATH", "CUSTOM_VAR"]


# ---------------------------------------------------------------------------
# build_enforcer factory
# ---------------------------------------------------------------------------


class TestBuildEnforcer:
    def test_none_tier(self, tmp_path: Path) -> None:
        enforcer = build_enforcer(tmp_path)
        assert isinstance(enforcer, NoIsolation)
        assert enforcer.tier == IsolationTier.NONE

    def test_soft_tier(self, tmp_path: Path) -> None:
        config = IsolationConfig(tier=IsolationTier.SOFT)
        enforcer = build_enforcer(tmp_path, config)
        assert isinstance(enforcer, SoftIsolation)

    def test_os_tier(self, tmp_path: Path) -> None:
        config = IsolationConfig(tier=IsolationTier.OS)
        enforcer = build_enforcer(tmp_path, config)
        assert isinstance(enforcer, OSIsolation)

    def test_container_tier(self, tmp_path: Path) -> None:
        config = IsolationConfig(tier=IsolationTier.CONTAINER)
        enforcer = build_enforcer(tmp_path, config)
        assert isinstance(enforcer, ContainerIsolation)


# ---------------------------------------------------------------------------
# NoIsolation (Tier 0)
# ---------------------------------------------------------------------------


class TestNoIsolation:
    def test_validate_path_passes_through(self, tmp_path: Path) -> None:
        enforcer = NoIsolation(agents_dir=tmp_path)
        path = tmp_path / "some" / "path"
        # NoIsolation just resolves
        result = enforcer.validate_path("agent-1", path)
        assert isinstance(result, Path)

    def test_validate_memory_access_always_true(self, tmp_path: Path) -> None:
        enforcer = NoIsolation(agents_dir=tmp_path)
        assert enforcer.validate_memory_access("agent-1", "agent-2") is True

    def test_prepare_terminal_env_passthrough(self, tmp_path: Path) -> None:
        enforcer = NoIsolation(agents_dir=tmp_path)
        envelope = enforcer.prepare_terminal_env("agent-1", ["echo", "hi"], tmp_path)
        assert envelope.cmd == ["echo", "hi"]
        assert envelope.env is None

    def test_cleanup_noop(self, tmp_path: Path) -> None:
        enforcer = NoIsolation(agents_dir=tmp_path)
        enforcer.cleanup("agent-1")  # should not raise


# ---------------------------------------------------------------------------
# SoftIsolation (Tier 1)
# ---------------------------------------------------------------------------


class TestSoftIsolation:
    def test_validate_path_inside_agent_dir(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = SoftIsolation(agents_dir=tmp_path)
        result = enforcer.validate_path("agent-1", agent_dir / "workspace" / "file.py")
        assert str(result).startswith(str(agent_dir.resolve()))

    def test_validate_path_rejects_traversal(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = SoftIsolation(agents_dir=tmp_path)
        with pytest.raises(PermissionError, match="outside its workspace"):
            enforcer.validate_path("agent-1", agent_dir / ".." / "agent-2" / "secrets")

    def test_validate_path_rejects_absolute_escape(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = SoftIsolation(agents_dir=tmp_path)
        with pytest.raises(PermissionError):
            enforcer.validate_path("agent-1", Path("/etc/passwd"))

    def test_validate_memory_access_blocks_cross_agent(self, tmp_path: Path) -> None:
        enforcer = SoftIsolation(agents_dir=tmp_path)
        assert enforcer.validate_memory_access("agent-1", "agent-1") is True
        assert enforcer.validate_memory_access("agent-1", "agent-2") is False

    def test_prepare_terminal_env_resets_bad_cwd(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = SoftIsolation(agents_dir=tmp_path)
        # Try to use another agent's directory as cwd
        bad_cwd = tmp_path / "agent-2" / "workspace"
        bad_cwd.mkdir(parents=True)
        envelope = enforcer.prepare_terminal_env("agent-1", ["echo"], bad_cwd)
        # Should be reset to agent-1's workspace
        assert "agent-1" in str(envelope.cwd)
        assert "workspace" in str(envelope.cwd)

    def test_prepare_terminal_env_accepts_good_cwd(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        workspace = agent_dir / "workspace"
        workspace.mkdir(parents=True)
        enforcer = SoftIsolation(agents_dir=tmp_path)
        envelope = enforcer.prepare_terminal_env("agent-1", ["echo"], workspace)
        assert envelope.cwd == workspace.resolve()


# ---------------------------------------------------------------------------
# OSIsolation (Tier 2)
# ---------------------------------------------------------------------------


class TestOSIsolation:
    def test_inherits_soft_protections(self, tmp_path: Path) -> None:
        """OS tier should block cross-agent memory access (inherited from Soft)."""
        enforcer = OSIsolation(agents_dir=tmp_path)
        assert enforcer.validate_memory_access("a", "b") is False

    def test_env_filtering(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        config = IsolationConfig(
            tier=IsolationTier.OS,
            allowed_env=["PATH", "HOME"],
        )
        enforcer = OSIsolation(agents_dir=tmp_path, config=config)
        envelope = enforcer.prepare_terminal_env("agent-1", ["echo"], agent_dir)
        assert envelope.env is not None
        # Only allowed vars should be present (plus TMPDIR, TEMP, TMP, CORTIVA_AGENT_ID)
        for key in envelope.env:
            assert key in {"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "CORTIVA_AGENT_ID"}
        assert envelope.env["CORTIVA_AGENT_ID"] == "agent-1"

    def test_per_agent_tmpdir(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = OSIsolation(agents_dir=tmp_path)
        envelope = enforcer.prepare_terminal_env("agent-1", ["echo"], agent_dir)
        assert envelope.tmpdir is not None
        assert envelope.tmpdir.exists()
        assert "agent-1" in str(envelope.tmpdir)

    def test_agent_socket_path(self, tmp_path: Path) -> None:
        enforcer = OSIsolation(agents_dir=tmp_path)
        sock = enforcer.agent_socket_path("agent-1")
        assert "agent-1" in str(sock)
        assert sock.name == "agent.sock"

    def test_cleanup_removes_tmpdir(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = OSIsolation(agents_dir=tmp_path)
        enforcer.prepare_terminal_env("agent-1", ["echo"], agent_dir)
        tmpdir = enforcer._tmpdirs.get("agent-1")
        assert tmpdir is not None and tmpdir.exists()
        enforcer.cleanup("agent-1")
        assert not tmpdir.exists()


# ---------------------------------------------------------------------------
# ContainerIsolation (Tier 3)
# ---------------------------------------------------------------------------


class TestContainerIsolation:
    def test_inherits_os_protections(self, tmp_path: Path) -> None:
        enforcer = ContainerIsolation(agents_dir=tmp_path)
        assert enforcer.validate_memory_access("a", "b") is False

    @patch("shutil.which", return_value="/usr/bin/docker")
    def test_prepare_terminal_env_wraps_with_docker(
        self, mock_which: object, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        config = IsolationConfig(
            tier=IsolationTier.CONTAINER,
            allowed_env=["PATH"],
            container=ContainerConfig(
                runtime="docker",
                cpu_limit="0.5",
                memory_limit="256m",
                network="none",
                image="python:3.13-slim",
            ),
        )
        enforcer = ContainerIsolation(agents_dir=tmp_path, config=config)
        envelope = enforcer.prepare_terminal_env(
            "agent-1", ["claude", "-p", "hello"], agent_dir,
        )
        # Command should start with docker run
        assert envelope.cmd[0] == "docker"
        assert envelope.cmd[1] == "run"
        assert "--rm" in envelope.cmd
        assert "--cpus" in envelope.cmd
        assert "0.5" in envelope.cmd
        assert "--memory" in envelope.cmd
        assert "256m" in envelope.cmd
        assert "--network=none" in envelope.cmd
        assert "--user" in envelope.cmd
        assert "1000:1000" in envelope.cmd
        assert "python:3.13-slim" in envelope.cmd
        # Original command should be at the end
        assert envelope.cmd[-3:] == ["claude", "-p", "hello"]
        # Container ID should be set
        assert envelope.container_id == "cortiva-agent-agent-1"

    @patch("shutil.which", return_value=None)
    def test_fallback_when_docker_unavailable(
        self, mock_which: object, tmp_path: Path
    ) -> None:
        """When docker is not on PATH, falls back to OS isolation."""
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = ContainerIsolation(agents_dir=tmp_path)
        envelope = enforcer.prepare_terminal_env(
            "agent-1", ["echo", "hi"], agent_dir,
        )
        # Should NOT wrap with docker
        assert envelope.cmd == ["echo", "hi"]
        assert envelope.container_id is None

    @patch("shutil.which", return_value="/usr/bin/docker")
    def test_container_volume_mount(
        self, mock_which: object, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = ContainerIsolation(agents_dir=tmp_path)
        envelope = enforcer.prepare_terminal_env(
            "agent-1", ["echo"], agent_dir,
        )
        # Volume mount should map agent dir to /agent
        volume_flag_idx = envelope.cmd.index("-v")
        volume_arg = envelope.cmd[volume_flag_idx + 1]
        assert volume_arg.endswith(":/agent:rw")
        assert str(agent_dir.resolve()) in volume_arg

    @patch("shutil.which", return_value="/usr/bin/docker")
    def test_default_network_is_bridge(
        self, mock_which: object, tmp_path: Path
    ) -> None:
        """Default container network should be bridge for API access."""
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = ContainerIsolation(agents_dir=tmp_path)
        envelope = enforcer.prepare_terminal_env("agent-1", ["echo"], agent_dir)
        assert "--network=bridge" in envelope.cmd

    @patch("shutil.which", return_value="/usr/bin/docker")
    def test_shm_size_flag(
        self, mock_which: object, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        config = IsolationConfig(
            tier=IsolationTier.CONTAINER,
            container=ContainerConfig(shm_size="512m"),
        )
        enforcer = ContainerIsolation(agents_dir=tmp_path, config=config)
        envelope = enforcer.prepare_terminal_env("agent-1", ["echo"], agent_dir)
        assert "--shm-size=512m" in envelope.cmd

    @patch("shutil.which", return_value="/usr/bin/docker")
    def test_browser_endpoint_injected(
        self, mock_which: object, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        config = IsolationConfig(
            tier=IsolationTier.CONTAINER,
            container=ContainerConfig(
                browser_endpoint="ws://browserless:3000",
            ),
        )
        enforcer = ContainerIsolation(agents_dir=tmp_path, config=config)
        envelope = enforcer.prepare_terminal_env("agent-1", ["echo"], agent_dir)
        assert "-e" in envelope.cmd
        idx = len(envelope.cmd) - 1
        found = False
        for i, arg in enumerate(envelope.cmd):
            if arg == "-e" and i + 1 < len(envelope.cmd):
                if envelope.cmd[i + 1] == "BROWSER_WS_ENDPOINT=ws://browserless:3000":
                    found = True
                    break
        assert found, "BROWSER_WS_ENDPOINT not found in container command"

    @patch("shutil.which", return_value="/usr/bin/docker")
    def test_no_browser_endpoint_when_empty(
        self, mock_which: object, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = ContainerIsolation(agents_dir=tmp_path)
        envelope = enforcer.prepare_terminal_env("agent-1", ["echo"], agent_dir)
        for i, arg in enumerate(envelope.cmd):
            if arg == "-e" and i + 1 < len(envelope.cmd):
                assert "BROWSER_WS_ENDPOINT" not in envelope.cmd[i + 1]

    @patch("shutil.which", return_value="/usr/bin/podman")
    def test_podman_runtime(self, mock_which: object, tmp_path: Path) -> None:
        config = IsolationConfig(
            tier=IsolationTier.CONTAINER,
            container=ContainerConfig(runtime="podman"),
        )
        agent_dir = tmp_path / "agent-1"
        agent_dir.mkdir()
        enforcer = ContainerIsolation(agents_dir=tmp_path, config=config)
        envelope = enforcer.prepare_terminal_env("agent-1", ["echo"], agent_dir)
        assert envelope.cmd[0] == "podman"


# ---------------------------------------------------------------------------
# GuardedMemoryAdapter
# ---------------------------------------------------------------------------


class TestGuardedMemoryAdapter:
    @pytest.mark.asyncio
    async def test_store_always_allowed(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.store.return_value = "record"
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)
        result = await guard.store("agent-1", "hello", tags=["test"])
        inner.store.assert_called_once()
        assert result == "record"

    @pytest.mark.asyncio
    async def test_search_blocked_cross_agent(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)
        result = await guard.search("agent-2", "query", _caller_id="agent-1")
        assert result == []
        inner.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_allowed_same_agent(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.search.return_value = ["record"]
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)
        result = await guard.search("agent-1", "query", _caller_id="agent-1")
        assert result == ["record"]
        inner.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_recall_blocked_cross_agent(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)
        result = await guard.recall("agent-2", _caller_id="agent-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_delete_blocked_cross_agent(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        enforcer = SoftIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)
        result = await guard.delete("agent-2", "mem-1", _caller_id="agent-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_isolation_allows_cross_agent(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.search.return_value = ["record"]
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)
        result = await guard.search("agent-2", "query", _caller_id="agent-1")
        assert result == ["record"]

    def test_getattr_proxies_to_inner(self, tmp_path: Path) -> None:
        inner = AsyncMock()
        inner.custom_method = "custom_value"
        enforcer = NoIsolation(agents_dir=tmp_path)
        guard = GuardedMemoryAdapter(inner=inner, enforcer=enforcer)
        assert guard.custom_method == "custom_value"


# ---------------------------------------------------------------------------
# Agent path validation
# ---------------------------------------------------------------------------


class TestAgentFilenameValidation:
    def test_safe_filenames(self) -> None:
        from cortiva.core.agent import Agent

        assert Agent._validate_filename("plan.md") == "plan.md"
        assert Agent._validate_filename("task_queue.json") == "task_queue.json"

    def test_rejects_path_traversal(self) -> None:
        from cortiva.core.agent import Agent

        with pytest.raises(ValueError, match="Unsafe filename"):
            Agent._validate_filename("../../../etc/passwd")

    def test_rejects_slash(self) -> None:
        from cortiva.core.agent import Agent

        with pytest.raises(ValueError, match="Unsafe filename"):
            Agent._validate_filename("subdir/file.txt")

    def test_rejects_backslash(self) -> None:
        from cortiva.core.agent import Agent

        with pytest.raises(ValueError, match="Unsafe filename"):
            Agent._validate_filename("subdir\\file.txt")

    def test_rejects_dot_dot(self) -> None:
        from cortiva.core.agent import Agent

        with pytest.raises(ValueError):
            Agent._validate_filename("..")

    def test_rejects_empty(self) -> None:
        from cortiva.core.agent import Agent

        with pytest.raises(ValueError):
            Agent._validate_filename("")

    def test_today_path_validates(self, tmp_path: Path) -> None:
        from cortiva.core.agent import Agent

        agent = Agent(id="test", directory=tmp_path)
        with pytest.raises(ValueError):
            agent.today_path("../../etc/passwd")

    def test_outbox_path_validates(self, tmp_path: Path) -> None:
        from cortiva.core.agent import Agent

        agent = Agent(id="test", directory=tmp_path)
        with pytest.raises(ValueError):
            agent.outbox_path("../secret")

    def test_workspace_path_validates(self, tmp_path: Path) -> None:
        from cortiva.core.agent import Agent

        agent = Agent(id="test", directory=tmp_path)
        with pytest.raises(ValueError):
            agent.workspace_path("../secret")
