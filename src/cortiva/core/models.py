"""
Cluster Model Registry — unified view of available models.

Aggregates node capabilities from cluster heartbeats and provides
resolution logic: local-first, then least-loaded remote node.
Consciousness calls are always local (terminal agent auth is per-node).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("cortiva.models")


@dataclass
class ModelEndpoint:
    """Resolved endpoint for a model request."""
    node_id: str
    model_name: str
    provider: str        # "ollama", "vllm", "llamacpp", "terminal"
    url: str = ""        # API URL for remote models
    is_local: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "model_name": self.model_name,
            "provider": self.provider,
            "url": self.url,
            "is_local": self.is_local,
        }


@dataclass
class NodeModels:
    """Models available on a single node."""
    node_id: str
    host: str = "localhost"
    port: int = 11434
    models: list[dict[str, Any]] = field(default_factory=list)
    terminal_agents: list[str] = field(default_factory=list)
    custom_endpoints: list[dict[str, Any]] = field(default_factory=list)
    agent_count: int = 0  # for load-aware routing


class ClusterModels:
    """Unified view of all models across the cluster.

    Resolution order:
    1. Local node (if capable and prefer_local)
    2. Least-loaded node with capability
    3. Fail with clear error

    Consciousness calls (terminal agents) are always local.
    """

    def __init__(self, local_node_id: str = "") -> None:
        self.local_node_id = local_node_id
        self._nodes: dict[str, NodeModels] = {}

    def update_node(
        self,
        node_id: str,
        *,
        host: str = "localhost",
        port: int = 11434,
        models: list[dict[str, Any]] | None = None,
        terminal_agents: list[str] | None = None,
        custom_endpoints: list[dict[str, Any]] | None = None,
        agent_count: int = 0,
    ) -> None:
        """Update or add a node's model inventory."""
        self._nodes[node_id] = NodeModels(
            node_id=node_id,
            host=host,
            port=port,
            models=models or [],
            terminal_agents=terminal_agents or [],
            custom_endpoints=custom_endpoints or [],
            agent_count=agent_count,
        )

    def remove_node(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)

    def resolve(
        self,
        need: str,
        *,
        model_name: str | None = None,
        prefer_local: bool = True,
        node_id: str | None = None,
    ) -> ModelEndpoint | None:
        """Find the best endpoint for a need.

        *need*: "consciousness", "routine", "embedding", or a model name
        *model_name*: specific model name to look for
        *node_id*: requesting node (defaults to local)

        Consciousness is always local — never routed remotely.
        """
        requesting_node = node_id or self.local_node_id

        # Consciousness is always local
        if need == "consciousness":
            local = self._nodes.get(requesting_node)
            if local and local.terminal_agents:
                return ModelEndpoint(
                    node_id=requesting_node,
                    model_name=local.terminal_agents[0],
                    provider="terminal",
                    is_local=True,
                )
            return None

        # For routine/embedding, try local first
        if prefer_local:
            endpoint = self._find_on_node(
                requesting_node, need, model_name,
            )
            if endpoint:
                return endpoint

        # Search all nodes, prefer least loaded
        candidates: list[tuple[int, ModelEndpoint]] = []
        for nid, node_models in self._nodes.items():
            if nid == requesting_node and prefer_local:
                continue  # Already checked
            endpoint = self._find_on_node(nid, need, model_name)
            if endpoint:
                endpoint.is_local = (nid == requesting_node)
                candidates.append((node_models.agent_count, endpoint))

        if not candidates:
            return None

        # Sort by load (agent_count), pick least loaded
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def _find_on_node(
        self,
        node_id: str,
        need: str,
        model_name: str | None,
    ) -> ModelEndpoint | None:
        """Check if a specific node can serve this need."""
        node = self._nodes.get(node_id)
        if not node:
            return None

        # Check local Ollama models — model_name match takes priority
        if model_name:
            for model in node.models:
                name = model.get("name", "")
                if model_name in name:
                    return ModelEndpoint(
                        node_id=node_id,
                        model_name=name,
                        provider="ollama",
                        url=f"http://{node.host}:{node.port}",
                        is_local=(node_id == self.local_node_id),
                    )

        # Fallback: match by need type
        for model in node.models:
            name = model.get("name", "")
            family = model.get("family", "").lower()

            if need == "embedding" and ("embed" in name.lower() or "embed" in family):
                return ModelEndpoint(
                    node_id=node_id,
                    model_name=name,
                    provider="ollama",
                    url=f"http://{node.host}:{node.port}",
                    is_local=(node_id == self.local_node_id),
                )
            if need == "routine" and "embed" not in name.lower() and not model_name:
                return ModelEndpoint(
                    node_id=node_id,
                    model_name=name,
                    provider="ollama",
                    url=f"http://{node.host}:{node.port}",
                    is_local=(node_id == self.local_node_id),
                )

        # Check custom endpoints
        for ep in node.custom_endpoints:
            ep_models = ep.get("models", [])
            if model_name and model_name in ep_models:
                return ModelEndpoint(
                    node_id=node_id,
                    model_name=model_name,
                    provider=ep.get("provider", "custom"),
                    url=ep.get("url", ""),
                    is_local=(node_id == self.local_node_id),
                )

        return None

    def available_models(self) -> dict[str, list[dict[str, Any]]]:
        """All models across all nodes, grouped by node."""
        result: dict[str, list[dict[str, Any]]] = {}
        for node_id, node in self._nodes.items():
            entries: list[dict[str, Any]] = []
            for model in node.models:
                entries.append({
                    "name": model.get("name", ""),
                    "provider": "ollama",
                    "family": model.get("family", ""),
                    "size_bytes": model.get("size_bytes", 0),
                })
            for agent_name in node.terminal_agents:
                entries.append({
                    "name": agent_name,
                    "provider": "terminal",
                })
            for ep in node.custom_endpoints:
                for m in ep.get("models", []):
                    entries.append({
                        "name": m,
                        "provider": ep.get("provider", "custom"),
                        "url": ep.get("url", ""),
                    })
            result[node_id] = entries
        return result

    def all_model_names(self) -> list[str]:
        """Flat list of all unique model names across the cluster."""
        names: set[str] = set()
        for node in self._nodes.values():
            for m in node.models:
                names.add(m.get("name", ""))
            names.update(node.terminal_agents)
            for ep in node.custom_endpoints:
                names.update(ep.get("models", []))
        names.discard("")
        return sorted(names)
