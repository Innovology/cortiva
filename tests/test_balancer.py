"""Tests for the cluster balancing primitives."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core.agent import Agent, AgentState
from cortiva.core.balancer import (
    ClusterMetrics,
    CommunicationTracker,
    NodeLoad,
    ProposedMove,
)
from cortiva.core.budget import (
    AgentBudgetStatus,
    BackendType,
    ConsciousnessBudgetManager,
)
from cortiva.core.discovery import NodeCapabilities, ResourceSnapshot
from cortiva.core.fabric import Fabric

# ---------------------------------------------------------------------------
# CommunicationTracker tests
# ---------------------------------------------------------------------------


class TestCommunicationTracker:
    def test_record_and_pair_counts(self) -> None:
        tracker = CommunicationTracker()
        tracker.record("alice", "bob")
        tracker.record("alice", "bob")
        tracker.record("alice", "bob")
        counts = tracker.pair_counts()
        assert counts[("alice", "bob")] == 3

    def test_pair_normalization(self) -> None:
        """(A,B) and (B,A) should collapse into a single pair."""
        tracker = CommunicationTracker()
        tracker.record("bob", "alice")
        tracker.record("alice", "bob")
        counts = tracker.pair_counts()
        assert len(counts) == 1
        assert counts[("alice", "bob")] == 2

    def test_window_expiry(self) -> None:
        tracker = CommunicationTracker(window_seconds=10.0)
        # Record an event and then mock time to be past the window
        tracker.record("alice", "bob")
        assert tracker.total_messages() == 1

        # Manually set the event timestamp to be in the past
        pair = ("alice", "bob")
        tracker._events[pair] = [time.monotonic() - 20.0]
        assert tracker.total_messages() == 0
        assert tracker.pair_counts() == {}

    def test_total_messages(self) -> None:
        tracker = CommunicationTracker()
        tracker.record("alice", "bob")
        tracker.record("alice", "charlie")
        tracker.record("bob", "charlie")
        assert tracker.total_messages() == 3

    def test_empty_tracker(self) -> None:
        tracker = CommunicationTracker()
        assert tracker.pair_counts() == {}
        assert tracker.total_messages() == 0

    def test_multiple_pairs(self) -> None:
        tracker = CommunicationTracker()
        tracker.record("alice", "bob")
        tracker.record("alice", "bob")
        tracker.record("charlie", "dave")
        counts = tracker.pair_counts()
        assert counts[("alice", "bob")] == 2
        assert counts[("charlie", "dave")] == 1


# ---------------------------------------------------------------------------
# NodeLoad tests
# ---------------------------------------------------------------------------


class TestNodeLoad:
    def test_to_dict(self) -> None:
        resources = ResourceSnapshot(
            cpu_cores=8,
            ram_total_gb=32.0,
            ram_available_gb=16.0,
            disk_total_gb=500.0,
            disk_free_gb=200.0,
            platform="darwin",
            python_version="3.12.0",
        )
        node = NodeLoad(
            node_id="test-node",
            agent_count=3,
            active_agent_count=2,
            agent_ids=["a1", "a2", "a3"],
            resources=resources,
        )
        d = node.to_dict()
        assert d["node_id"] == "test-node"
        assert d["agent_count"] == 3
        assert d["active_agent_count"] == 2
        assert d["agent_ids"] == ["a1", "a2", "a3"]
        assert d["ram_usage_ratio"] == 0.5
        assert d["budget_exhaustion_ratio"] == 0.0
        assert d["resources"]["cpu_cores"] == 8

    def test_defaults(self) -> None:
        node = NodeLoad()
        assert node.node_id == ""
        assert node.agent_count == 0
        assert node.ram_usage_ratio == 0.0
        assert node.budget_exhaustion_ratio == 0.0
        d = node.to_dict()
        assert d["resources"] == {}

    def test_ram_usage_ratio(self) -> None:
        resources = ResourceSnapshot(ram_total_gb=16.0, ram_available_gb=4.0)
        node = NodeLoad(resources=resources)
        assert node.ram_usage_ratio == 0.75

    def test_budget_exhaustion_ratio(self) -> None:
        s1 = AgentBudgetStatus(agent_id="a1", exhausted=True)
        s2 = AgentBudgetStatus(agent_id="a2", exhausted=False)
        s3 = AgentBudgetStatus(agent_id="a3", exhausted=True)
        node = NodeLoad(budget_status={"a1": s1, "a2": s2, "a3": s3})
        assert abs(node.budget_exhaustion_ratio - 2 / 3) < 0.01


# ---------------------------------------------------------------------------
# ProposedMove tests
# ---------------------------------------------------------------------------


class TestProposedMove:
    def test_to_dict(self) -> None:
        move = ProposedMove(
            agent_id="worker-01",
            source_node="node-a",
            target_node="node-b",
            reason="Rebalance load",
            priority_score=0.65,
        )
        d = move.to_dict()
        assert d["agent_id"] == "worker-01"
        assert d["source_node"] == "node-a"
        assert d["target_node"] == "node-b"
        assert d["reason"] == "Rebalance load"
        assert d["priority_score"] == 0.65


# ---------------------------------------------------------------------------
# ClusterMetrics tests
# ---------------------------------------------------------------------------


def _make_capabilities(node_id: str = "test-node") -> NodeCapabilities:
    """Helper to build a NodeCapabilities with realistic resources."""
    return NodeCapabilities(
        node_id=node_id,
        terminal_agents=[],
        local_models=[],
        custom_endpoints=[],
        resources=ResourceSnapshot(
            cpu_cores=8,
            ram_total_gb=32.0,
            ram_available_gb=16.0,
            disk_total_gb=500.0,
            disk_free_gb=200.0,
            platform="darwin",
            python_version="3.12.0",
        ),
    )


def _make_agent(aid: str, state: AgentState = AgentState.EXECUTING) -> Agent:
    return Agent(id=aid, directory=Path(f"/tmp/agents/{aid}"), state=state)


class TestClusterMetrics:
    def test_snapshot_single_node(self) -> None:
        tracker = CommunicationTracker()
        metrics = ClusterMetrics(communication_tracker=tracker)
        caps = _make_capabilities()
        agents = {"a1": _make_agent("a1"), "a2": _make_agent("a2")}

        nodes = metrics.snapshot(caps, agents)
        assert len(nodes) == 1
        assert nodes[0].node_id == "test-node"
        assert nodes[0].agent_count == 2
        assert nodes[0].active_agent_count == 2

    def test_snapshot_no_capabilities(self) -> None:
        tracker = CommunicationTracker()
        metrics = ClusterMetrics(communication_tracker=tracker)

        nodes = metrics.snapshot(None, {})
        assert nodes == []

    def test_snapshot_counts_active_agents(self) -> None:
        """Sleeping agents should not count as active."""
        tracker = CommunicationTracker()
        metrics = ClusterMetrics(communication_tracker=tracker)
        caps = _make_capabilities()
        agents = {
            "a1": _make_agent("a1", AgentState.EXECUTING),
            "a2": _make_agent("a2", AgentState.SLEEPING),
            "a3": _make_agent("a3", AgentState.ONBOARDING),
        }

        nodes = metrics.snapshot(caps, agents)
        assert nodes[0].agent_count == 3
        assert nodes[0].active_agent_count == 1  # only a1

    def test_snapshot_budget_exhaustion_ratio(self) -> None:
        tracker = CommunicationTracker()
        metrics = ClusterMetrics(communication_tracker=tracker)
        caps = _make_capabilities()
        agents = {"a1": _make_agent("a1"), "a2": _make_agent("a2")}

        mgr = ConsciousnessBudgetManager(
            default_backend=BackendType.API,
            fallback_chain=[BackendType.API],
            backend_configs={BackendType.API: {"calls_limit": 5}},
        )
        mgr.register_agent("a1")
        mgr.register_agent("a2")
        # Exhaust a1's budget
        for _ in range(5):
            mgr.record_usage("a1", BackendType.API, 10, 10)

        nodes = metrics.snapshot(caps, agents, mgr)
        assert nodes[0].budget_exhaustion_ratio == 0.5

    def test_affinity_scores_normalized(self) -> None:
        tracker = CommunicationTracker()
        tracker.record("a1", "a2")
        tracker.record("a1", "a2")
        tracker.record("a1", "a2")
        tracker.record("a1", "a2")
        tracker.record("a2", "a3")
        tracker.record("a2", "a3")

        metrics = ClusterMetrics(communication_tracker=tracker)
        scores = metrics.agent_affinity_scores()
        # Most chatty pair should be 1.0
        assert scores[("a1", "a2")] == 1.0
        # Less chatty pair should be 0.5
        assert scores[("a2", "a3")] == 0.5

    def test_affinity_scores_empty(self) -> None:
        tracker = CommunicationTracker()
        metrics = ClusterMetrics(communication_tracker=tracker)
        assert metrics.agent_affinity_scores() == {}

    def test_suggest_moves_single_node_no_pressure(self) -> None:
        """No suggestions when the cluster is healthy."""
        tracker = CommunicationTracker()
        metrics = ClusterMetrics(communication_tracker=tracker)
        caps = _make_capabilities()
        agents = {"a1": _make_agent("a1")}

        metrics.snapshot(caps, agents)
        moves = metrics.suggest_moves()
        assert moves == []

    def test_suggest_moves_single_node_budget_pressure(self) -> None:
        """Should suggest moves for exhausted agents."""
        tracker = CommunicationTracker()
        metrics = ClusterMetrics(communication_tracker=tracker)
        caps = _make_capabilities()
        agents = {"a1": _make_agent("a1"), "a2": _make_agent("a2")}

        mgr = ConsciousnessBudgetManager(
            default_backend=BackendType.API,
            fallback_chain=[BackendType.API],
            backend_configs={BackendType.API: {"calls_limit": 5}},
        )
        mgr.register_agent("a1")
        mgr.register_agent("a2")
        # Exhaust a1's budget
        for _ in range(5):
            mgr.record_usage("a1", BackendType.API, 10, 10)

        metrics.snapshot(caps, agents, mgr)
        moves = metrics.suggest_moves()
        assert len(moves) >= 1
        exhausted_move = [m for m in moves if m.agent_id == "a1"]
        assert len(exhausted_move) == 1
        assert exhausted_move[0].target_node == "<new-node-needed>"
        assert exhausted_move[0].reason == "Budget exhausted"

    def test_suggest_moves_multi_node_imbalanced(self) -> None:
        """Should suggest concrete moves between nodes when imbalanced."""
        tracker = CommunicationTracker()
        metrics = ClusterMetrics(communication_tracker=tracker)

        # Simulate two nodes by constructing snapshots directly
        heavy_status = {
            "a1": AgentBudgetStatus(agent_id="a1", exhausted=True),
            "a2": AgentBudgetStatus(agent_id="a2", exhausted=True),
        }
        light_status = {
            "a3": AgentBudgetStatus(agent_id="a3", exhausted=False),
        }

        heavy_node = NodeLoad(
            node_id="node-heavy",
            agent_count=2,
            active_agent_count=2,
            agent_ids=["a1", "a2"],
            resources=ResourceSnapshot(ram_total_gb=16.0, ram_available_gb=2.0),
            budget_status=heavy_status,
        )
        light_node = NodeLoad(
            node_id="node-light",
            agent_count=1,
            active_agent_count=1,
            agent_ids=["a3"],
            resources=ResourceSnapshot(ram_total_gb=16.0, ram_available_gb=12.0),
            budget_status=light_status,
        )

        metrics._last_snapshot = [heavy_node, light_node]
        moves = metrics.suggest_moves()
        assert len(moves) == 1
        assert moves[0].source_node == "node-heavy"
        assert moves[0].target_node == "node-light"
        assert moves[0].reason == "Rebalance load"


# ---------------------------------------------------------------------------
# Fabric integration tests
# ---------------------------------------------------------------------------


class MockConsciousness:
    async def think(self, agent_id, context, prompt, **kwargs):
        if "plan" in prompt.lower() or "checklist" in prompt.lower():
            return ConsciousResponse(
                content=(
                    "# Today's Plan\n\n"
                    "- [ ] Review tickets\n"
                    "- [ ] Process messages\n"
                ),
                tokens_in=100,
                tokens_out=50,
                model="mock",
            )
        return ConsciousResponse(
            content=f"[{agent_id}] Done.",
            tokens_in=100,
            tokens_out=50,
            model="mock",
        )

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(
            content=f"# {agent_id}\n\nGood day.",
            reflection="All good.",
            tokens_in=200,
            tokens_out=100,
            model="mock",
        )


class TestFabricBalancerIntegration:
    def _make_fabric(self, tmp_path: Path) -> Fabric:
        from cortiva.core.fabric import Fabric

        return Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=MockConsciousness(),
        )

    def test_fabric_has_tracker_and_metrics(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        assert isinstance(fabric.communication_tracker, CommunicationTracker)
        assert isinstance(fabric.cluster_metrics, ClusterMetrics)
        assert fabric.cluster_metrics.tracker is fabric.communication_tracker

    def test_cluster_load_ipc_handler(self, tmp_path: Path) -> None:
        """The IPC handler should be registered and return valid data."""
        fabric = self._make_fabric(tmp_path)

        # Create a mock server to capture registered handlers
        handlers: dict[str, object] = {}

        class MockServer:
            def register(self, name, handler):
                handlers[name] = handler

        mock_server = MockServer()
        fabric._register_ipc_handlers(mock_server)  # type: ignore[arg-type]

        assert "cluster.load" in handlers

    @pytest.mark.asyncio
    async def test_communication_tracking_on_send(self, tmp_path: Path) -> None:
        """When _process_reflection sends messages, tracker should record them."""
        from cortiva.core.fabric import Fabric
        from cortiva.core.reflection import ReflectionSuffix

        mock_channel = MagicMock()
        mock_channel.send = AsyncMock()
        mock_channel.receive = AsyncMock(return_value=[])

        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=MockConsciousness(),
            channel=mock_channel,
        )
        fabric.register_agent("sender-01")
        agent = fabric.get_agent("sender-01")

        from cortiva.core.agent import Task

        task = Task(id="t1", description="test task")
        suffix = ReflectionSuffix(
            messages=[{"to": "recipient-01", "content": "hello"}],
        )
        await fabric._process_reflection(agent, task, suffix)

        # Channel send should have been called
        mock_channel.send.assert_called_once()

        # Tracker should have recorded the communication
        counts = fabric.communication_tracker.pair_counts()
        pair = ("recipient-01", "sender-01")
        assert counts.get(pair, 0) == 1
