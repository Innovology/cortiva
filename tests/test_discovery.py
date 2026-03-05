"""Tests for node capability auto-discovery."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cortiva.core.discovery import (
    EndpointInfo,
    LocalModelInfo,
    NodeCapabilities,
    ResourceSnapshot,
    TerminalAgentInfo,
    _discover_ollama_models,
    _discover_resources,
    _discover_terminal_agents,
    _check_endpoint,
    _run_cmd,
)


# ---------------------------------------------------------------------------
# Data class serialisation
# ---------------------------------------------------------------------------


class TestDataClasses:
    def test_terminal_agent_info_to_dict(self) -> None:
        info = TerminalAgentInfo(
            name="claude-code", binary="/usr/bin/claude",
            version="1.0.0", available=True, auth_ok=True,
        )
        d = info.to_dict()
        assert d["name"] == "claude-code"
        assert d["available"] is True
        assert d["auth_ok"] is True

    def test_local_model_info_to_dict(self) -> None:
        info = LocalModelInfo(
            name="qwen3.5:35b", size_bytes=20_000_000_000,
            family="qwen", parameter_size="35B",
        )
        d = info.to_dict()
        assert d["name"] == "qwen3.5:35b"
        assert d["size_bytes"] == 20_000_000_000

    def test_endpoint_info_to_dict(self) -> None:
        info = EndpointInfo(
            name="vllm-local", url="http://localhost:8000",
            provider="vllm", models=["llama-3"], healthy=True,
        )
        d = info.to_dict()
        assert d["healthy"] is True
        assert d["models"] == ["llama-3"]

    def test_resource_snapshot_to_dict(self) -> None:
        snap = ResourceSnapshot(
            cpu_cores=8, ram_total_gb=32.0, ram_available_gb=16.5,
            disk_total_gb=500.0, disk_free_gb=200.0,
            platform="Darwin", python_version="3.13.0",
        )
        d = snap.to_dict()
        assert d["cpu_cores"] == 8
        assert d["ram_available_gb"] == 16.5

    def test_node_capabilities_to_dict(self) -> None:
        caps = NodeCapabilities(
            node_id="test-node",
            terminal_agents=[TerminalAgentInfo(name="claude-code", binary="/usr/bin/claude")],
            local_models=[LocalModelInfo(name="qwen")],
            resources=ResourceSnapshot(cpu_cores=4),
        )
        d = caps.to_dict()
        assert d["node_id"] == "test-node"
        assert len(d["terminal_agents"]) == 1
        assert len(d["local_models"]) == 1

    def test_node_capabilities_summary(self) -> None:
        caps = NodeCapabilities(
            node_id="test-node",
            terminal_agents=[
                TerminalAgentInfo(name="claude-code", binary="/usr/bin/claude", available=True),
                TerminalAgentInfo(name="codex", binary="", available=False),
            ],
            local_models=[LocalModelInfo(name="m1"), LocalModelInfo(name="m2")],
            resources=ResourceSnapshot(cpu_cores=8, ram_available_gb=16.0),
        )
        s = caps.summary
        assert "claude-code" in s
        assert "codex" not in s  # not available
        assert "models: 2" in s
        assert "8 cores" in s


# ---------------------------------------------------------------------------
# _run_cmd
# ---------------------------------------------------------------------------


class TestRunCmd:
    @pytest.mark.asyncio
    async def test_successful_command(self) -> None:
        code, output = await _run_cmd(["echo", "hello"])
        assert code == 0
        assert "hello" in output

    @pytest.mark.asyncio
    async def test_missing_binary(self) -> None:
        code, output = await _run_cmd(["nonexistent_binary_xyz"])
        assert code == -1
        assert output == ""

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        code, output = await _run_cmd(["sleep", "60"], timeout=0.1)
        assert code == -2


# ---------------------------------------------------------------------------
# Terminal agent discovery
# ---------------------------------------------------------------------------


class TestTerminalAgentDiscovery:
    @pytest.mark.asyncio
    async def test_discovers_available_agents(self) -> None:
        with patch("cortiva.core.discovery.shutil.which") as mock_which:
            mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name == "claude" else None
            with patch("cortiva.core.discovery._run_cmd", return_value=(0, "claude 1.2.3")):
                with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
                    agents = await _discover_terminal_agents()

        claude = next(a for a in agents if a.name == "claude-code")
        assert claude.available is True
        assert claude.version == "claude 1.2.3"
        assert claude.auth_ok is True

        codex = next(a for a in agents if a.name == "codex")
        assert codex.available is False

    @pytest.mark.asyncio
    async def test_no_auth_key(self) -> None:
        with patch("cortiva.core.discovery.shutil.which", return_value="/usr/bin/claude"):
            with patch("cortiva.core.discovery._run_cmd", return_value=(0, "v1")):
                with patch.dict("os.environ", {}, clear=True):
                    agents = await _discover_terminal_agents()

        claude = next(a for a in agents if a.name == "claude-code")
        assert claude.available is True
        assert claude.auth_ok is False


# ---------------------------------------------------------------------------
# Ollama model discovery
# ---------------------------------------------------------------------------


class TestOllamaDiscovery:
    @pytest.mark.asyncio
    async def test_discovers_models(self) -> None:
        mock_data = {
            "models": [
                {
                    "name": "qwen3.5:35b-a3b",
                    "size": 20_000_000_000,
                    "details": {
                        "family": "qwen",
                        "parameter_size": "35B",
                        "quantization_level": "Q4_K_M",
                    },
                },
                {
                    "name": "llama3:8b",
                    "size": 5_000_000_000,
                    "details": {"family": "llama", "parameter_size": "8B"},
                },
            ]
        }

        with patch("cortiva.core.discovery._fetch_ollama_tags", return_value=mock_data):
            models = await _discover_ollama_models()

        assert len(models) == 2
        assert models[0].name == "qwen3.5:35b-a3b"
        assert models[0].family == "qwen"
        assert models[0].quantization == "Q4_K_M"
        assert models[1].name == "llama3:8b"

    @pytest.mark.asyncio
    async def test_ollama_not_running(self) -> None:
        with patch("cortiva.core.discovery._fetch_ollama_tags", side_effect=Exception("Connection refused")):
            models = await _discover_ollama_models()

        assert models == []


# ---------------------------------------------------------------------------
# Custom endpoint checks
# ---------------------------------------------------------------------------


class TestEndpointCheck:
    @pytest.mark.asyncio
    async def test_healthy_endpoint(self) -> None:
        with patch("cortiva.core.discovery._ping_endpoint", return_value=True):
            info = await _check_endpoint({
                "name": "vllm-local",
                "url": "http://localhost:8000",
                "provider": "vllm",
                "models": ["llama-3"],
            })

        assert info.name == "vllm-local"
        assert info.healthy is True
        assert info.models == ["llama-3"]

    @pytest.mark.asyncio
    async def test_unreachable_endpoint(self) -> None:
        with patch("cortiva.core.discovery._ping_endpoint", return_value=False):
            info = await _check_endpoint({
                "name": "remote",
                "url": "http://unreachable:9999",
            })

        assert info.healthy is False

    @pytest.mark.asyncio
    async def test_empty_url(self) -> None:
        info = await _check_endpoint({"name": "empty"})
        assert info.healthy is False


# ---------------------------------------------------------------------------
# System resource discovery
# ---------------------------------------------------------------------------


class TestResourceDiscovery:
    def test_discovers_resources(self) -> None:
        snap = _discover_resources()
        assert snap.cpu_cores > 0
        assert snap.platform != ""
        assert snap.python_version != ""
        assert snap.disk_total_gb > 0

    def test_ram_detected(self) -> None:
        snap = _discover_resources()
        # At least some RAM should be detected (either via psutil or fallback)
        assert snap.ram_total_gb > 0


# ---------------------------------------------------------------------------
# Full discovery
# ---------------------------------------------------------------------------


class TestNodeCapabilitiesDiscover:
    @pytest.mark.asyncio
    async def test_full_discovery(self) -> None:
        with patch("cortiva.core.discovery._discover_terminal_agents", return_value=[
            TerminalAgentInfo(name="claude-code", binary="/usr/bin/claude", available=True),
        ]):
            with patch("cortiva.core.discovery._discover_ollama_models", return_value=[
                LocalModelInfo(name="qwen3.5:35b"),
            ]):
                caps = await NodeCapabilities.discover("test-node")

        assert caps.node_id == "test-node"
        assert len(caps.terminal_agents) == 1
        assert len(caps.local_models) == 1
        assert caps.resources.cpu_cores > 0

    @pytest.mark.asyncio
    async def test_discovery_with_custom_endpoints(self) -> None:
        with patch("cortiva.core.discovery._discover_terminal_agents", return_value=[]):
            with patch("cortiva.core.discovery._discover_ollama_models", return_value=[]):
                with patch("cortiva.core.discovery._ping_endpoint", return_value=True):
                    caps = await NodeCapabilities.discover(
                        "test-node",
                        custom_endpoints=[
                            {"name": "vllm", "url": "http://localhost:8000"},
                        ],
                    )

        assert len(caps.custom_endpoints) == 1
        assert caps.custom_endpoints[0].healthy is True


# ---------------------------------------------------------------------------
# Fabric integration
# ---------------------------------------------------------------------------


class TestFabricDiscoveryIntegration:
    def _make_fabric(self, tmp_path):
        from cortiva.adapters.memory.inmemory import InMemoryAdapter
        from cortiva.core.fabric import Fabric

        class StubConsciousness:
            async def think(self, **kw):
                from cortiva.adapters.protocols import ConsciousResponse
                return ConsciousResponse(content="ok", model="stub")
            async def reflect(self, **kw):
                from cortiva.adapters.protocols import ConsciousResponse
                return ConsciousResponse(content="ok", model="stub")

        return Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=StubConsciousness(),
        )

    @pytest.mark.asyncio
    async def test_start_runs_discovery(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)

        with patch("cortiva.core.discovery._discover_terminal_agents", return_value=[]):
            with patch("cortiva.core.discovery._discover_ollama_models", return_value=[]):
                await fabric.start()
                assert fabric.capabilities is not None
                assert fabric.capabilities.resources.cpu_cores > 0
                await fabric.stop()

    @pytest.mark.asyncio
    async def test_capabilities_in_status(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)

        with patch("cortiva.core.discovery._discover_terminal_agents", return_value=[]):
            with patch("cortiva.core.discovery._discover_ollama_models", return_value=[]):
                await fabric.start()
                status = fabric.status()
                assert "capabilities" in status
                assert "node_id" in status["capabilities"]
                await fabric.stop()

    @pytest.mark.asyncio
    async def test_custom_endpoints_from_config(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        fabric._custom_endpoints = [
            {"name": "vllm", "url": "http://localhost:8000"},
        ]

        with patch("cortiva.core.discovery._discover_terminal_agents", return_value=[]):
            with patch("cortiva.core.discovery._discover_ollama_models", return_value=[]):
                with patch("cortiva.core.discovery._ping_endpoint", return_value=True):
                    await fabric.start()
                    assert len(fabric.capabilities.custom_endpoints) == 1
                    assert fabric.capabilities.custom_endpoints[0].healthy is True
                    await fabric.stop()

    @pytest.mark.asyncio
    async def test_discover_ipc_handler(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        fabric.capabilities = NodeCapabilities(
            node_id="test",
            resources=ResourceSnapshot(cpu_cores=4),
        )

        server = MagicMock()
        handlers = {}

        def capture_register(name, handler):
            handlers[name] = handler

        server.register = capture_register
        fabric._register_ipc_handlers(server)

        assert "discover" in handlers
        result = await handlers["discover"]()
        assert result["ok"] is True
        assert result["node_id"] == "test"


# ---------------------------------------------------------------------------
# Config integration — cluster.endpoints
# ---------------------------------------------------------------------------


class TestConfigDiscoveryIntegration:
    def test_build_fabric_with_custom_endpoints(self, tmp_path) -> None:
        from unittest.mock import patch as _patch

        from cortiva.core.config import build_fabric

        config = {
            "fabric": {"name": "test"},
            "memory": {"adapter": "inmemory"},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
            "cluster": {
                "endpoints": [
                    {"name": "vllm", "url": "http://localhost:8000", "provider": "vllm"},
                ],
            },
        }

        def _mock_import(registry, name, kind):
            if kind == "memory":
                from cortiva.adapters.memory.inmemory import InMemoryAdapter
                return InMemoryAdapter
            class MockCls:
                def __init__(self, **kw): pass
            return MockCls

        with _patch("cortiva.core.config._import_adapter", side_effect=_mock_import):
            fabric = build_fabric(config)

        assert fabric._custom_endpoints == [
            {"name": "vllm", "url": "http://localhost:8000", "provider": "vllm"},
        ]

    def test_build_fabric_without_cluster_section(self, tmp_path) -> None:
        from unittest.mock import patch as _patch

        from cortiva.core.config import build_fabric

        config = {
            "fabric": {"name": "test"},
            "memory": {"adapter": "inmemory"},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
        }

        def _mock_import(registry, name, kind):
            if kind == "memory":
                from cortiva.adapters.memory.inmemory import InMemoryAdapter
                return InMemoryAdapter
            class MockCls:
                def __init__(self, **kw): pass
            return MockCls

        with _patch("cortiva.core.config._import_adapter", side_effect=_mock_import):
            fabric = build_fabric(config)

        assert fabric._custom_endpoints == []


# ---------------------------------------------------------------------------
# CLI discover command
# ---------------------------------------------------------------------------


class TestDiscoverCLI:
    def test_discover_command_runs(self, capsys) -> None:
        from cortiva.cli.main import cmd_discover

        args = MagicMock()

        with patch("cortiva.core.discovery._discover_terminal_agents", return_value=[
            TerminalAgentInfo(name="claude-code", binary="/usr/bin/claude", available=True, auth_ok=True, version="1.0"),
            TerminalAgentInfo(name="codex", binary="", available=False),
        ]):
            with patch("cortiva.core.discovery._discover_ollama_models", return_value=[
                LocalModelInfo(name="qwen3.5:35b", parameter_size="35B", size_bytes=20_000_000_000),
            ]):
                cmd_discover(args)

        captured = capsys.readouterr()
        assert "claude-code" in captured.out
        assert "available" in captured.out
        assert "qwen3.5:35b" in captured.out
        assert "CPU" in captured.out
