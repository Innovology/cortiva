"""
Cluster Architecture — multi-node Cortiva.

Nodes discover each other, maintain a registry of agent placements,
exchange heartbeats, and support agent mobility. Single-node mode
works identically — the cluster is optional.

Discovery modes:
  - static: nodes listed in cortiva.yaml
  - mdns: Bonjour/mDNS on the local network
  - config: shared config file path
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("cortiva.cluster")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ClusterNode:
    """Represents a single node in the cluster."""
    node_id: str
    host: str = "localhost"
    port: int = 9400
    agents: list[str] = field(default_factory=list)
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "online"  # "online" | "degraded" | "offline"
    capabilities: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "host": self.host,
            "port": self.port,
            "agents": self.agents,
            "last_heartbeat": self.last_heartbeat.isoformat(),
            "status": self.status,
            "capabilities": self.capabilities,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterNode:
        hb = data.get("last_heartbeat")
        if isinstance(hb, str):
            hb = datetime.fromisoformat(hb)
        else:
            hb = datetime.now(timezone.utc)
        return cls(
            node_id=data.get("node_id", ""),
            host=data.get("host", "localhost"),
            port=data.get("port", 9400),
            agents=data.get("agents", []),
            last_heartbeat=hb,
            status=data.get("status", "online"),
            capabilities=data.get("capabilities", {}),
        )

    @property
    def is_online(self) -> bool:
        return self.status == "online"

    @property
    def api_url(self) -> str:
        return f"http://{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# Agent Registry
# ---------------------------------------------------------------------------


class AgentRegistry:
    """Maps agent IDs to the node they live on."""

    def __init__(self) -> None:
        self._mapping: dict[str, str] = {}  # agent_id -> node_id

    def register(self, agent_id: str, node_id: str) -> None:
        self._mapping[agent_id] = node_id

    def unregister(self, agent_id: str) -> None:
        self._mapping.pop(agent_id, None)

    def find(self, agent_id: str) -> str | None:
        return self._mapping.get(agent_id)

    def agents_on_node(self, node_id: str) -> list[str]:
        return [a for a, n in self._mapping.items() if n == node_id]

    def all_agents(self) -> dict[str, str]:
        return dict(self._mapping)

    def move(self, agent_id: str, target_node_id: str) -> None:
        """Atomically update an agent's node assignment."""
        self._mapping[agent_id] = target_node_id

    def to_dict(self) -> dict[str, str]:
        return dict(self._mapping)


# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------

# How long before a node is considered offline (seconds)
HEARTBEAT_TIMEOUT = 120


class Cluster:
    """Manages cluster state: nodes, registry, and discovery."""

    def __init__(
        self,
        local_node_id: str = "",
        discovery_mode: str = "static",
    ) -> None:
        self.local_node_id = local_node_id
        self.discovery_mode = discovery_mode
        self.nodes: dict[str, ClusterNode] = {}
        self.registry = AgentRegistry()

    # ----- Node management -----

    async def join(self, node: ClusterNode) -> None:
        """Register a node in the cluster."""
        self.nodes[node.node_id] = node
        # Register its agents
        for agent_id in node.agents:
            self.registry.register(agent_id, node.node_id)
        logger.info(f"Node joined: {node.node_id} ({len(node.agents)} agents)")

    async def leave(self, node_id: str) -> None:
        """Remove a node from the cluster."""
        node = self.nodes.pop(node_id, None)
        if node:
            for agent_id in node.agents:
                self.registry.unregister(agent_id)
            logger.info(f"Node left: {node_id}")

    async def heartbeat(self, node_id: str, status: dict[str, Any]) -> None:
        """Update a node's heartbeat and status."""
        node = self.nodes.get(node_id)
        if node is None:
            # Auto-join on first heartbeat
            node = ClusterNode(node_id=node_id)
            self.nodes[node_id] = node

        node.last_heartbeat = datetime.now(timezone.utc)
        node.status = status.get("status", "online")
        node.agents = status.get("agents", node.agents)
        node.capabilities = status.get("capabilities", node.capabilities)

        # Update registry
        for agent_id in node.agents:
            self.registry.register(agent_id, node_id)

    def check_timeouts(self) -> list[str]:
        """Mark nodes as offline if heartbeat has expired. Returns degraded node IDs."""
        now = datetime.now(timezone.utc)
        degraded: list[str] = []
        for node_id, node in self.nodes.items():
            elapsed = (now - node.last_heartbeat).total_seconds()
            if elapsed > HEARTBEAT_TIMEOUT and node.status == "online":
                node.status = "degraded"
                degraded.append(node_id)
                logger.warning(f"Node degraded (no heartbeat): {node_id}")
        return degraded

    # ----- Queries -----

    def get_registry(self) -> dict[str, str]:
        """Return agent_id → node_id mapping."""
        return self.registry.all_agents()

    def find_agent(self, agent_id: str) -> ClusterNode | None:
        """Find which node an agent is on."""
        node_id = self.registry.find(agent_id)
        if node_id:
            return self.nodes.get(node_id)
        return None

    def online_nodes(self) -> list[ClusterNode]:
        return [n for n in self.nodes.values() if n.is_online]

    def node_count(self) -> int:
        return len(self.nodes)

    def is_single_node(self) -> bool:
        return len(self.nodes) <= 1

    # ----- Discovery -----

    async def discover(
        self,
        static_nodes: list[dict[str, Any]] | None = None,
        config_path: str | None = None,
    ) -> list[ClusterNode]:
        """Discover cluster peers based on discovery mode."""
        discovered: list[ClusterNode] = []

        if self.discovery_mode == "static" and static_nodes:
            discovered = await _discover_static(static_nodes)
        elif self.discovery_mode == "config" and config_path:
            discovered = await _discover_config(config_path)
        elif self.discovery_mode == "mdns":
            discovered = await _discover_mdns()

        for node in discovered:
            if node.node_id != self.local_node_id:
                await self.join(node)

        return discovered

    # ----- Serialisation -----

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_node_id": self.local_node_id,
            "discovery_mode": self.discovery_mode,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
            "registry": self.registry.to_dict(),
        }


# ---------------------------------------------------------------------------
# Agent Mobility
# ---------------------------------------------------------------------------


@dataclass
class MoveResult:
    """Result of an agent move operation."""
    success: bool
    agent_id: str
    source_node: str
    target_node: str
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "agent_id": self.agent_id,
            "source_node": self.source_node,
            "target_node": self.target_node,
            "error": self.error,
        }


async def move_agent(
    cluster: Cluster,
    agent_id: str,
    target_node_id: str,
    *,
    sync_fn: Any | None = None,
) -> MoveResult:
    """Move an agent between nodes.

    Steps:
    1. Validate source and target nodes
    2. Sync agent directory to target (via *sync_fn* callback)
    3. Update registry atomically
    4. Update node agent lists

    The caller is responsible for sleeping the agent before calling this
    and waking it on the target after.

    *sync_fn* signature: ``async def sync(agent_id, source_node, target_node) -> bool``
    """
    source_node_id = cluster.registry.find(agent_id)
    if not source_node_id:
        return MoveResult(
            success=False, agent_id=agent_id,
            source_node="", target_node=target_node_id,
            error=f"Agent {agent_id} not found in registry",
        )

    source = cluster.nodes.get(source_node_id)
    target = cluster.nodes.get(target_node_id)

    if not target:
        return MoveResult(
            success=False, agent_id=agent_id,
            source_node=source_node_id, target_node=target_node_id,
            error=f"Target node {target_node_id} not found",
        )

    if not target.is_online:
        return MoveResult(
            success=False, agent_id=agent_id,
            source_node=source_node_id, target_node=target_node_id,
            error=f"Target node {target_node_id} is {target.status}",
        )

    # Sync agent directory
    if sync_fn:
        try:
            synced = await sync_fn(agent_id, source, target)
            if not synced:
                return MoveResult(
                    success=False, agent_id=agent_id,
                    source_node=source_node_id, target_node=target_node_id,
                    error="Directory sync failed",
                )
        except Exception as e:
            return MoveResult(
                success=False, agent_id=agent_id,
                source_node=source_node_id, target_node=target_node_id,
                error=f"Sync error: {e}",
            )

    # Atomic registry update
    cluster.registry.move(agent_id, target_node_id)

    # Update node agent lists
    if source and agent_id in source.agents:
        source.agents.remove(agent_id)
    if agent_id not in target.agents:
        target.agents.append(agent_id)

    logger.info(f"Agent {agent_id} moved: {source_node_id} -> {target_node_id}")

    return MoveResult(
        success=True, agent_id=agent_id,
        source_node=source_node_id, target_node=target_node_id,
    )


async def sync_via_rsync(
    agent_id: str,
    source: ClusterNode,
    target: ClusterNode,
    agents_dir: str = "./agents",
) -> bool:
    """Sync an agent directory via rsync over SSH."""
    src_path = f"{agents_dir}/{agent_id}/"
    dst_path = f"{target.host}:{agents_dir}/{agent_id}/"

    try:
        proc = await asyncio.create_subprocess_exec(
            "rsync", "-az", "--delete", src_path, dst_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.error(f"rsync failed: {stderr.decode()}")
            return False
        return True
    except Exception as e:
        logger.error(f"rsync error: {e}")
        return False


# ---------------------------------------------------------------------------
# Discovery implementations
# ---------------------------------------------------------------------------


async def _discover_static(
    node_configs: list[dict[str, Any]],
) -> list[ClusterNode]:
    """Discover nodes from a static list with optional health check."""
    nodes: list[ClusterNode] = []
    for cfg in node_configs:
        host = cfg.get("host", "localhost")
        port = cfg.get("port", 9400)
        node_id = cfg.get("node_id", host)

        node = ClusterNode(
            node_id=node_id,
            host=host,
            port=port,
        )

        # Try to fetch status from the node's IPC
        try:
            loop = asyncio.get_event_loop()
            status = await loop.run_in_executor(
                None, _fetch_node_status, host, port,
            )
            if status:
                node.status = "online"
                node.agents = status.get("agents", [])
                node.capabilities = status.get("capabilities", {})
            else:
                node.status = "offline"
        except Exception:
            node.status = "offline"

        nodes.append(node)
    return nodes


async def _discover_config(config_path: str) -> list[ClusterNode]:
    """Discover nodes from a shared config file (JSON/YAML)."""
    path = Path(config_path)
    if not path.exists():
        return []

    content = path.read_text()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        try:
            import yaml
            data = yaml.safe_load(content)
        except Exception:
            return []

    if not isinstance(data, dict):
        return []

    nodes_data = data.get("nodes", [])
    return [ClusterNode.from_dict(n) for n in nodes_data if isinstance(n, dict)]


async def _discover_mdns() -> list[ClusterNode]:
    """Discover nodes via mDNS/Bonjour (placeholder).

    Full implementation would use zeroconf to browse for
    ``_cortiva._tcp.local.`` services.
    """
    # Requires: pip install zeroconf
    try:
        from zeroconf import ServiceBrowser, Zeroconf

        nodes: list[ClusterNode] = []
        found: list[dict[str, Any]] = []

        class Listener:
            def add_service(self, zc: Any, type_: str, name: str) -> None:
                info = zc.get_service_info(type_, name)
                if info:
                    found.append({
                        "node_id": name.split(".")[0],
                        "host": info.server,
                        "port": info.port,
                    })

            def remove_service(self, zc: Any, type_: str, name: str) -> None:
                pass

            def update_service(self, zc: Any, type_: str, name: str) -> None:
                pass

        zc = Zeroconf()
        ServiceBrowser(zc, "_cortiva._tcp.local.", Listener())
        await asyncio.sleep(2)  # Wait for responses
        zc.close()

        for f in found:
            nodes.append(ClusterNode(
                node_id=f["node_id"],
                host=f["host"],
                port=f["port"],
            ))
        return nodes
    except ImportError:
        logger.debug("zeroconf not installed, mDNS discovery unavailable")
        return []


def _fetch_node_status(host: str, port: int) -> dict[str, Any] | None:
    """Fetch node status via HTTP (synchronous, runs in executor)."""
    try:
        url = f"http://{host}:{port}/status"
        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None
