"""Tests for cluster architecture, model registry, and agent mobility."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cortiva.core.cluster import (
    AgentRegistry,
    Cluster,
    ClusterNode,
    HEARTBEAT_TIMEOUT,
    MoveResult,
    move_agent,
    sync_via_rsync,
    _discover_static,
    _discover_config,
    _discover_mdns,
    _fetch_node_status,
)
from cortiva.core.models import (
    ClusterModels,
    ModelEndpoint,
    NodeModels,
)


# ---------------------------------------------------------------------------
# ClusterNode
# ---------------------------------------------------------------------------


class TestClusterNode:
    def test_to_dict_roundtrip(self) -> None:
        node = ClusterNode(
            node_id="n1", host="10.0.0.1", port=9400,
            agents=["a1", "a2"], status="online",
        )
        d = node.to_dict()
        assert d["node_id"] == "n1"
        assert d["host"] == "10.0.0.1"
        assert d["agents"] == ["a1", "a2"]
        assert d["status"] == "online"

        restored = ClusterNode.from_dict(d)
        assert restored.node_id == "n1"
        assert restored.host == "10.0.0.1"
        assert restored.agents == ["a1", "a2"]

    def test_from_dict_defaults(self) -> None:
        node = ClusterNode.from_dict({})
        assert node.node_id == ""
        assert node.host == "localhost"
        assert node.port == 9400
        assert node.status == "online"

    def test_from_dict_parses_heartbeat(self) -> None:
        ts = "2026-01-15T10:00:00+00:00"
        node = ClusterNode.from_dict({"node_id": "x", "last_heartbeat": ts})
        assert node.last_heartbeat.year == 2026

    def test_is_online(self) -> None:
        node = ClusterNode(node_id="n", status="online")
        assert node.is_online is True
        node.status = "degraded"
        assert node.is_online is False

    def test_api_url(self) -> None:
        node = ClusterNode(node_id="n", host="myhost", port=8080)
        assert node.api_url == "http://myhost:8080"


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------


class TestAgentRegistry:
    def test_register_and_find(self) -> None:
        reg = AgentRegistry()
        reg.register("agent-1", "node-a")
        assert reg.find("agent-1") == "node-a"
        assert reg.find("agent-2") is None

    def test_unregister(self) -> None:
        reg = AgentRegistry()
        reg.register("agent-1", "node-a")
        reg.unregister("agent-1")
        assert reg.find("agent-1") is None
        # Unregister non-existent is safe
        reg.unregister("agent-1")

    def test_agents_on_node(self) -> None:
        reg = AgentRegistry()
        reg.register("a1", "n1")
        reg.register("a2", "n1")
        reg.register("a3", "n2")
        assert sorted(reg.agents_on_node("n1")) == ["a1", "a2"]
        assert reg.agents_on_node("n2") == ["a3"]

    def test_all_agents(self) -> None:
        reg = AgentRegistry()
        reg.register("a1", "n1")
        reg.register("a2", "n2")
        assert reg.all_agents() == {"a1": "n1", "a2": "n2"}

    def test_move(self) -> None:
        reg = AgentRegistry()
        reg.register("a1", "n1")
        reg.move("a1", "n2")
        assert reg.find("a1") == "n2"

    def test_to_dict(self) -> None:
        reg = AgentRegistry()
        reg.register("a1", "n1")
        d = reg.to_dict()
        assert d == {"a1": "n1"}


# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------


class TestCluster:
    @pytest.mark.asyncio
    async def test_join_and_query(self) -> None:
        cluster = Cluster(local_node_id="local")
        node = ClusterNode(node_id="peer", agents=["a1", "a2"])
        await cluster.join(node)

        assert cluster.node_count() == 1
        assert cluster.registry.find("a1") == "peer"
        assert cluster.find_agent("a1") is not None
        assert cluster.find_agent("unknown") is None

    @pytest.mark.asyncio
    async def test_leave(self) -> None:
        cluster = Cluster()
        node = ClusterNode(node_id="n1", agents=["a1"])
        await cluster.join(node)
        await cluster.leave("n1")
        assert cluster.node_count() == 0
        assert cluster.registry.find("a1") is None

    @pytest.mark.asyncio
    async def test_heartbeat_auto_joins(self) -> None:
        cluster = Cluster()
        await cluster.heartbeat("n1", {"status": "online", "agents": ["a1"]})
        assert cluster.node_count() == 1
        assert cluster.nodes["n1"].status == "online"
        assert cluster.registry.find("a1") == "n1"

    @pytest.mark.asyncio
    async def test_heartbeat_updates_existing(self) -> None:
        cluster = Cluster()
        node = ClusterNode(node_id="n1", agents=["a1"])
        await cluster.join(node)
        await cluster.heartbeat("n1", {"status": "degraded", "agents": ["a1", "a2"]})
        assert cluster.nodes["n1"].status == "degraded"
        assert cluster.registry.find("a2") == "n1"

    def test_check_timeouts(self) -> None:
        cluster = Cluster()
        old_time = datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_TIMEOUT + 10)
        node = ClusterNode(node_id="n1", last_heartbeat=old_time, status="online")
        cluster.nodes["n1"] = node

        degraded = cluster.check_timeouts()
        assert "n1" in degraded
        assert cluster.nodes["n1"].status == "degraded"

        # Already degraded nodes aren't re-reported
        degraded2 = cluster.check_timeouts()
        assert degraded2 == []

    def test_online_nodes(self) -> None:
        cluster = Cluster()
        cluster.nodes["n1"] = ClusterNode(node_id="n1", status="online")
        cluster.nodes["n2"] = ClusterNode(node_id="n2", status="degraded")
        cluster.nodes["n3"] = ClusterNode(node_id="n3", status="online")
        online = cluster.online_nodes()
        assert len(online) == 2

    def test_is_single_node(self) -> None:
        cluster = Cluster()
        assert cluster.is_single_node() is True
        cluster.nodes["n1"] = ClusterNode(node_id="n1")
        assert cluster.is_single_node() is True
        cluster.nodes["n2"] = ClusterNode(node_id="n2")
        assert cluster.is_single_node() is False

    def test_to_dict(self) -> None:
        cluster = Cluster(local_node_id="local", discovery_mode="static")
        d = cluster.to_dict()
        assert d["local_node_id"] == "local"
        assert d["discovery_mode"] == "static"
        assert "nodes" in d
        assert "registry" in d

    def test_get_registry(self) -> None:
        cluster = Cluster()
        cluster.registry.register("a1", "n1")
        assert cluster.get_registry() == {"a1": "n1"}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_discover_static_with_health_check(self) -> None:
        configs = [
            {"host": "10.0.0.1", "port": 9400, "node_id": "peer1"},
        ]
        with patch(
            "cortiva.core.cluster._fetch_node_status",
            return_value={"agents": ["a1"], "capabilities": {"cpu": 8}},
        ):
            nodes = await _discover_static(configs)
        assert len(nodes) == 1
        assert nodes[0].node_id == "peer1"
        assert nodes[0].status == "online"
        assert nodes[0].agents == ["a1"]

    @pytest.mark.asyncio
    async def test_discover_static_offline(self) -> None:
        configs = [{"host": "10.0.0.2", "node_id": "peer2"}]
        with patch("cortiva.core.cluster._fetch_node_status", return_value=None):
            nodes = await _discover_static(configs)
        assert nodes[0].status == "offline"

    @pytest.mark.asyncio
    async def test_discover_config_json(self, tmp_path: Path) -> None:
        config_file = tmp_path / "cluster.json"
        config_file.write_text(json.dumps({
            "nodes": [
                {"node_id": "n1", "host": "h1"},
                {"node_id": "n2", "host": "h2"},
            ]
        }))
        nodes = await _discover_config(str(config_file))
        assert len(nodes) == 2
        assert nodes[0].node_id == "n1"

    @pytest.mark.asyncio
    async def test_discover_config_missing(self) -> None:
        nodes = await _discover_config("/nonexistent/path.json")
        assert nodes == []

    @pytest.mark.asyncio
    async def test_discover_mdns_no_zeroconf(self) -> None:
        """Without zeroconf installed, returns empty list."""
        nodes = await _discover_mdns()
        # May or may not have zeroconf, either way should not crash
        assert isinstance(nodes, list)

    @pytest.mark.asyncio
    async def test_cluster_discover_joins_peers(self) -> None:
        cluster = Cluster(local_node_id="local")
        with patch(
            "cortiva.core.cluster._discover_static",
            new_callable=AsyncMock,
            return_value=[
                ClusterNode(node_id="peer1", agents=["a1"]),
                ClusterNode(node_id="local"),  # Should be skipped
            ],
        ):
            peers = await cluster.discover(
                static_nodes=[{"host": "x", "node_id": "peer1"}],
            )
        # peer1 joined, local was skipped
        assert "peer1" in cluster.nodes
        assert "local" not in cluster.nodes

    def test_fetch_node_status_failure(self) -> None:
        result = _fetch_node_status("255.255.255.255", 1)
        assert result is None


# ---------------------------------------------------------------------------
# Agent Mobility (move_agent)
# ---------------------------------------------------------------------------


class TestAgentMobility:
    @pytest.mark.asyncio
    async def test_move_agent_success(self) -> None:
        cluster = Cluster(local_node_id="n1")
        source = ClusterNode(node_id="n1", agents=["a1"], status="online")
        target = ClusterNode(node_id="n2", agents=[], status="online")
        await cluster.join(source)
        await cluster.join(target)

        result = await move_agent(cluster, "a1", "n2")
        assert result.success is True
        assert result.source_node == "n1"
        assert result.target_node == "n2"
        assert cluster.registry.find("a1") == "n2"
        assert "a1" not in cluster.nodes["n1"].agents
        assert "a1" in cluster.nodes["n2"].agents

    @pytest.mark.asyncio
    async def test_move_agent_not_found(self) -> None:
        cluster = Cluster()
        result = await move_agent(cluster, "unknown", "n2")
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_move_agent_target_missing(self) -> None:
        cluster = Cluster()
        cluster.registry.register("a1", "n1")
        cluster.nodes["n1"] = ClusterNode(node_id="n1", agents=["a1"])
        result = await move_agent(cluster, "a1", "n2")
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_move_agent_target_offline(self) -> None:
        cluster = Cluster()
        cluster.registry.register("a1", "n1")
        cluster.nodes["n1"] = ClusterNode(node_id="n1", agents=["a1"])
        cluster.nodes["n2"] = ClusterNode(node_id="n2", status="degraded")
        result = await move_agent(cluster, "a1", "n2")
        assert result.success is False
        assert "degraded" in result.error

    @pytest.mark.asyncio
    async def test_move_agent_with_sync_fn(self) -> None:
        cluster = Cluster()
        source = ClusterNode(node_id="n1", agents=["a1"], status="online")
        target = ClusterNode(node_id="n2", agents=[], status="online")
        await cluster.join(source)
        await cluster.join(target)

        sync_fn = AsyncMock(return_value=True)
        result = await move_agent(cluster, "a1", "n2", sync_fn=sync_fn)
        assert result.success is True
        sync_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_move_agent_sync_fails(self) -> None:
        cluster = Cluster()
        source = ClusterNode(node_id="n1", agents=["a1"], status="online")
        target = ClusterNode(node_id="n2", agents=[], status="online")
        await cluster.join(source)
        await cluster.join(target)

        sync_fn = AsyncMock(return_value=False)
        result = await move_agent(cluster, "a1", "n2", sync_fn=sync_fn)
        assert result.success is False
        assert "sync failed" in result.error.lower()

    @pytest.mark.asyncio
    async def test_move_agent_sync_exception(self) -> None:
        cluster = Cluster()
        source = ClusterNode(node_id="n1", agents=["a1"], status="online")
        target = ClusterNode(node_id="n2", agents=[], status="online")
        await cluster.join(source)
        await cluster.join(target)

        sync_fn = AsyncMock(side_effect=OSError("disk full"))
        result = await move_agent(cluster, "a1", "n2", sync_fn=sync_fn)
        assert result.success is False
        assert "disk full" in result.error

    def test_move_result_to_dict(self) -> None:
        r = MoveResult(success=True, agent_id="a1", source_node="n1", target_node="n2")
        d = r.to_dict()
        assert d["success"] is True
        assert d["agent_id"] == "a1"


# ---------------------------------------------------------------------------
# ClusterModels / Model Registry
# ---------------------------------------------------------------------------


class TestClusterModels:
    def test_resolve_consciousness_always_local(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("local", terminal_agents=["claude-code"])
        reg.update_node("remote", terminal_agents=["codex"])

        endpoint = reg.resolve("consciousness")
        assert endpoint is not None
        assert endpoint.node_id == "local"
        assert endpoint.provider == "terminal"
        assert endpoint.is_local is True

    def test_resolve_consciousness_no_terminal(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("local", models=[])
        assert reg.resolve("consciousness") is None

    def test_resolve_routine_local_first(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("local", models=[{"name": "qwen3.5:35b", "family": "qwen"}])
        reg.update_node("remote", models=[{"name": "llama3:8b", "family": "llama"}])

        endpoint = reg.resolve("routine")
        assert endpoint is not None
        assert endpoint.node_id == "local"
        assert endpoint.model_name == "qwen3.5:35b"

    def test_resolve_routine_fallback_remote(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("local", models=[])
        reg.update_node("remote", host="10.0.0.2", models=[
            {"name": "llama3:8b", "family": "llama"},
        ])

        endpoint = reg.resolve("routine")
        assert endpoint is not None
        assert endpoint.node_id == "remote"
        assert endpoint.is_local is False

    def test_resolve_embedding(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("local", models=[
            {"name": "nomic-embed-text", "family": "nomic"},
        ])
        endpoint = reg.resolve("embedding")
        assert endpoint is not None
        assert endpoint.model_name == "nomic-embed-text"

    def test_resolve_specific_model_name(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("local", models=[
            {"name": "qwen3.5:35b", "family": "qwen"},
            {"name": "llama3:8b", "family": "llama"},
        ])
        endpoint = reg.resolve("routine", model_name="llama3")
        assert endpoint is not None
        assert "llama3" in endpoint.model_name

    def test_resolve_custom_endpoint(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("local", custom_endpoints=[
            {"provider": "vllm", "url": "http://gpu:8000", "models": ["mixtral"]},
        ])
        endpoint = reg.resolve("routine", model_name="mixtral")
        assert endpoint is not None
        assert endpoint.provider == "vllm"
        assert endpoint.model_name == "mixtral"

    def test_resolve_least_loaded(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("local", models=[])
        reg.update_node("busy", models=[{"name": "m1", "family": ""}], agent_count=10)
        reg.update_node("idle", models=[{"name": "m2", "family": ""}], agent_count=1)

        endpoint = reg.resolve("routine")
        assert endpoint is not None
        assert endpoint.node_id == "idle"

    def test_resolve_not_found(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("local", models=[])
        assert reg.resolve("routine") is None

    def test_available_models(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("local", models=[{"name": "m1", "family": "f1"}])
        reg.update_node("remote", terminal_agents=["claude-code"])
        result = reg.available_models()
        assert "local" in result
        assert "remote" in result
        assert any(e["name"] == "m1" for e in result["local"])
        assert any(e["name"] == "claude-code" for e in result["remote"])

    def test_all_model_names(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("n1", models=[{"name": "m1"}], terminal_agents=["ta"])
        reg.update_node("n2", custom_endpoints=[
            {"models": ["m2", "m3"]},
        ])
        names = reg.all_model_names()
        assert names == ["m1", "m2", "m3", "ta"]

    def test_update_and_remove_node(self) -> None:
        reg = ClusterModels(local_node_id="local")
        reg.update_node("n1", models=[{"name": "m1"}])
        assert reg.all_model_names() == ["m1"]
        reg.remove_node("n1")
        assert reg.all_model_names() == []


class TestNodeModels:
    def test_defaults(self) -> None:
        nm = NodeModels(node_id="n1")
        assert nm.models == []
        assert nm.terminal_agents == []
        assert nm.custom_endpoints == []
        assert nm.agent_count == 0


class TestModelEndpoint:
    def test_to_dict(self) -> None:
        ep = ModelEndpoint(
            node_id="n1", model_name="m1", provider="ollama",
            url="http://localhost:11434", is_local=True,
        )
        d = ep.to_dict()
        assert d["node_id"] == "n1"
        assert d["is_local"] is True


# ---------------------------------------------------------------------------
# Fabric integration (IPC handlers)
# ---------------------------------------------------------------------------


class TestFabricClusterIPC:
    """Test that cluster IPC handlers are registered and work."""

    def _make_fabric(self, tmp_path: Path) -> Any:
        from cortiva.adapters.memory.inmemory import InMemoryAdapter
        from cortiva.core.fabric import Fabric

        memory = InMemoryAdapter()
        consciousness = AsyncMock()
        return Fabric(
            agents_dir=tmp_path / "agents",
            memory=memory,
            consciousness=consciousness,
        )

    def test_ipc_handlers_registered(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        server = MagicMock()
        server.register = MagicMock()
        fabric._register_ipc_handlers(server)

        registered = {call.args[0] for call in server.register.call_args_list}
        assert "cluster.status" in registered
        assert "cluster.nodes" in registered
        assert "agent.move" in registered

    @pytest.mark.asyncio
    async def test_cluster_status_handler(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        fabric.cluster = Cluster(local_node_id="test-node")
        fabric.model_registry = ClusterModels(local_node_id="test-node")

        # Extract handler
        handlers: dict[str, Any] = {}
        server = MagicMock()
        server.register = lambda name, fn: handlers.__setitem__(name, fn)
        fabric._register_ipc_handlers(server)

        result = await handlers["cluster.status"]()
        assert result["ok"] is True
        assert result["local_node_id"] == "test-node"
        assert result["single_node"] is True

    @pytest.mark.asyncio
    async def test_cluster_nodes_handler(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        node = ClusterNode(node_id="n1", host="h1", agents=["a1"])
        await fabric.cluster.join(node)

        handlers: dict[str, Any] = {}
        server = MagicMock()
        server.register = lambda name, fn: handlers.__setitem__(name, fn)
        fabric._register_ipc_handlers(server)

        result = await handlers["cluster.nodes"]()
        assert result["ok"] is True
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["node_id"] == "n1"

    @pytest.mark.asyncio
    async def test_agent_move_handler_success(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        source = ClusterNode(node_id="n1", agents=["a1"], status="online")
        target = ClusterNode(node_id="n2", agents=[], status="online")
        await fabric.cluster.join(source)
        await fabric.cluster.join(target)

        handlers: dict[str, Any] = {}
        server = MagicMock()
        server.register = lambda name, fn: handlers.__setitem__(name, fn)
        fabric._register_ipc_handlers(server)

        result = await handlers["agent.move"](agent_id="a1", target_node="n2")
        assert result["ok"] is True
        assert result["target_node"] == "n2"

    @pytest.mark.asyncio
    async def test_agent_move_handler_missing_params(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)

        handlers: dict[str, Any] = {}
        server = MagicMock()
        server.register = lambda name, fn: handlers.__setitem__(name, fn)
        fabric._register_ipc_handlers(server)

        result = await handlers["agent.move"]()
        assert result["ok"] is False
        assert "agent_id required" in result["error"]

        result = await handlers["agent.move"](agent_id="a1")
        assert result["ok"] is False
        assert "target_node required" in result["error"]


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


class TestCLIParser:
    def test_cluster_status_parser(self) -> None:
        from cortiva.cli.main import build_parser
        parser = build_parser()
        args = parser.parse_args(["cluster", "status"])
        assert args.command == "cluster"
        assert args.cluster_command == "status"

    def test_cluster_nodes_parser(self) -> None:
        from cortiva.cli.main import build_parser
        parser = build_parser()
        args = parser.parse_args(["cluster", "nodes"])
        assert args.command == "cluster"
        assert args.cluster_command == "nodes"

    def test_agent_move_parser(self) -> None:
        from cortiva.cli.main import build_parser
        parser = build_parser()
        args = parser.parse_args(["agent", "move", "a1", "--to", "n2"])
        assert args.command == "agent"
        assert args.agent_command == "move"
        assert args.id == "a1"
        assert args.to == "n2"
