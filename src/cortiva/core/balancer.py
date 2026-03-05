"""
Cluster balancing primitives.

Provides communication tracking between agent pairs, per-node load
snapshots, and a heuristic that suggests agent migrations to balance
work across the cluster.

Single-node for now — the data model supports multiple nodes so the
COO agent (or operator) can act on suggestions once multi-node
networking lands (#22).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cortiva.core.agent import Agent, AgentState

if TYPE_CHECKING:
    from cortiva.core.budget import ConsciousnessBudgetManager
    from cortiva.core.discovery import NodeCapabilities


# ---------------------------------------------------------------------------
# CommunicationTracker
# ---------------------------------------------------------------------------


class CommunicationTracker:
    """Tracks message frequency between agent pairs over a trailing window."""

    def __init__(self, window_seconds: float = 3600.0) -> None:
        self.window_seconds = window_seconds
        # keyed by normalized (min, max) pair
        self._events: dict[tuple[str, str], list[float]] = {}

    @staticmethod
    def _normalize_pair(a: str, b: str) -> tuple[str, str]:
        return (min(a, b), max(a, b))

    def record(self, sender: str, recipient: str) -> None:
        """Record a message event between two agents."""
        pair = self._normalize_pair(sender, recipient)
        self._events.setdefault(pair, []).append(time.monotonic())

    def _prune(self) -> None:
        """Remove events older than the trailing window."""
        cutoff = time.monotonic() - self.window_seconds
        empty_keys: list[tuple[str, str]] = []
        for pair, timestamps in self._events.items():
            self._events[pair] = [t for t in timestamps if t >= cutoff]
            if not self._events[pair]:
                empty_keys.append(pair)
        for k in empty_keys:
            del self._events[k]

    def pair_counts(self) -> dict[tuple[str, str], int]:
        """Return message counts per normalized agent pair."""
        self._prune()
        return {pair: len(ts) for pair, ts in self._events.items()}

    def total_messages(self) -> int:
        """Total messages across all pairs in the current window."""
        self._prune()
        return sum(len(ts) for ts in self._events.values())


# ---------------------------------------------------------------------------
# NodeLoad
# ---------------------------------------------------------------------------


@dataclass
class NodeLoad:
    """Per-node metrics snapshot."""

    node_id: str = ""
    agent_count: int = 0
    active_agent_count: int = 0
    agent_ids: list[str] = field(default_factory=list)
    resources: Any = None  # ResourceSnapshot
    budget_status: dict[str, Any] = field(default_factory=dict)  # agent_id -> AgentBudgetStatus

    @property
    def ram_usage_ratio(self) -> float:
        """Fraction of RAM currently in use (0-1)."""
        if self.resources is None or self.resources.ram_total_gb == 0:
            return 0.0
        used = self.resources.ram_total_gb - self.resources.ram_available_gb
        return max(0.0, min(1.0, used / self.resources.ram_total_gb))

    @property
    def budget_exhaustion_ratio(self) -> float:
        """Fraction of agents whose budgets are exhausted (0-1)."""
        if not self.budget_status:
            return 0.0
        exhausted = sum(
            1 for s in self.budget_status.values()
            if (hasattr(s, "exhausted") and s.exhausted)
        )
        return exhausted / len(self.budget_status)

    @property
    def model_count(self) -> int:
        """Number of local models available on this node."""
        if self.resources is None:
            return 0
        # ResourceSnapshot doesn't track models; NodeCapabilities does.
        # This is populated externally when capabilities are available.
        return getattr(self, "_model_count", 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "agent_count": self.agent_count,
            "active_agent_count": self.active_agent_count,
            "agent_ids": self.agent_ids,
            "ram_usage_ratio": round(self.ram_usage_ratio, 3),
            "budget_exhaustion_ratio": round(self.budget_exhaustion_ratio, 3),
            "resources": self.resources.to_dict() if self.resources else {},
        }


# ---------------------------------------------------------------------------
# ProposedMove
# ---------------------------------------------------------------------------


@dataclass
class ProposedMove:
    """Suggested agent migration."""

    agent_id: str
    source_node: str
    target_node: str
    reason: str
    priority_score: float = 0.0  # 0-1

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "source_node": self.source_node,
            "target_node": self.target_node,
            "reason": self.reason,
            "priority_score": round(self.priority_score, 3),
        }


# ---------------------------------------------------------------------------
# ClusterMetrics
# ---------------------------------------------------------------------------


class ClusterMetrics:
    """Assembles per-node snapshots and runs a balancing heuristic."""

    RAM_PRESSURE_THRESHOLD = 0.85
    BUDGET_PRESSURE_THRESHOLD = 0.5

    def __init__(
        self,
        communication_tracker: CommunicationTracker,
        budget_weight: float = 0.5,
        affinity_weight: float = 0.3,
        resource_weight: float = 0.2,
    ) -> None:
        self.tracker = communication_tracker
        self.budget_weight = budget_weight
        self.affinity_weight = affinity_weight
        self.resource_weight = resource_weight
        self._last_snapshot: list[NodeLoad] = []

    def snapshot(
        self,
        capabilities: NodeCapabilities | None,
        agents: dict[str, Agent],
        budget_manager: ConsciousnessBudgetManager | None = None,
    ) -> list[NodeLoad]:
        """Build NodeLoad list from local data."""
        if capabilities is None:
            self._last_snapshot = []
            return []

        all_ids = list(agents.keys())
        active_ids = [
            aid for aid, a in agents.items()
            if a.state not in (AgentState.SLEEPING, AgentState.ONBOARDING)
        ]

        budget_status: dict[str, Any] = {}
        if budget_manager:
            budget_status = budget_manager.all_status()

        node = NodeLoad(
            node_id=capabilities.node_id,
            agent_count=len(all_ids),
            active_agent_count=len(active_ids),
            agent_ids=all_ids,
            resources=capabilities.resources,
            budget_status=budget_status,
        )
        self._last_snapshot = [node]
        return [node]

    def agent_affinity_scores(self) -> dict[tuple[str, str], float]:
        """Normalized 0-1 affinity scores from communication frequency."""
        counts = self.tracker.pair_counts()
        if not counts:
            return {}
        max_count = max(counts.values())
        if max_count == 0:
            return {}
        return {pair: count / max_count for pair, count in counts.items()}

    def suggest_moves(self) -> list[ProposedMove]:
        """Suggest agent migrations based on current snapshot."""
        nodes = self._last_snapshot
        if not nodes:
            return []
        if len(nodes) == 1:
            return self._single_node_suggestions(nodes[0])
        return self._multi_node_suggestions(nodes)

    def _single_node_suggestions(self, node: NodeLoad) -> list[ProposedMove]:
        """Flag agents under pressure on a single-node cluster."""
        moves: list[ProposedMove] = []

        # RAM pressure
        if node.ram_usage_ratio > self.RAM_PRESSURE_THRESHOLD:
            moves.append(ProposedMove(
                agent_id="*",
                source_node=node.node_id,
                target_node="<new-node-needed>",
                reason=f"RAM usage at {node.ram_usage_ratio:.0%}",
                priority_score=min(1.0, node.ram_usage_ratio),
            ))

        # Budget pressure — flag individually exhausted agents
        for agent_id, status in node.budget_status.items():
            if hasattr(status, "exhausted") and status.exhausted:
                moves.append(ProposedMove(
                    agent_id=agent_id if isinstance(agent_id, str) else str(agent_id),
                    source_node=node.node_id,
                    target_node="<new-node-needed>",
                    reason="Budget exhausted",
                    priority_score=0.7,
                ))

        return moves

    def _multi_node_suggestions(self, nodes: list[NodeLoad]) -> list[ProposedMove]:
        """Produce concrete moves between nodes."""
        # Score each node by composite load
        scored = []
        for n in nodes:
            load = (
                self.resource_weight * n.ram_usage_ratio
                + self.budget_weight * n.budget_exhaustion_ratio
            )
            scored.append((load, n))

        scored.sort(key=lambda x: x[0], reverse=True)
        most_loaded_score, most_loaded = scored[0]
        least_loaded_score, least_loaded = scored[-1]

        if most_loaded_score - least_loaded_score < 0.1:
            return []  # balanced enough

        affinities = self.agent_affinity_scores()
        moves: list[ProposedMove] = []

        # Pick agents from the most-loaded node
        for agent_id in most_loaded.agent_ids:
            # Check affinity cost: skip if this agent talks heavily
            # with others on the same node
            affinity_cost = 0.0
            for other_id in most_loaded.agent_ids:
                if other_id == agent_id:
                    continue
                pair = CommunicationTracker._normalize_pair(agent_id, other_id)
                affinity_cost += affinities.get(pair, 0.0)

            # Only suggest move if affinity cost is manageable
            if affinity_cost < 0.5:
                moves.append(ProposedMove(
                    agent_id=agent_id,
                    source_node=most_loaded.node_id,
                    target_node=least_loaded.node_id,
                    reason="Rebalance load",
                    priority_score=round(most_loaded_score - least_loaded_score, 3),
                ))
                break  # one move at a time

        return moves
