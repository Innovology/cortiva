"""Tests for core Cortiva functionality."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from cortiva.core.agent import Agent, AgentState
from cortiva.core.fabric import Fabric
from cortiva.adapters.memory.inmemory import InMemoryAdapter


# ---------------------------------------------------------------------------
# Agent tests
# ---------------------------------------------------------------------------

class TestAgent:
    def test_create_agent(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        assert agent.id == "test-01"
        assert agent.state == AgentState.SLEEPING

    def test_lifecycle_transitions(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")

        # Valid: SLEEPING → WAKING
        agent.transition(AgentState.WAKING)
        assert agent.state == AgentState.WAKING
        assert agent.last_wake is not None

        # Valid: WAKING → PLANNING
        agent.transition(AgentState.PLANNING)
        assert agent.state == AgentState.PLANNING

        # Valid: PLANNING → EXECUTING
        agent.transition(AgentState.EXECUTING)
        assert agent.state == AgentState.EXECUTING

        # Valid: EXECUTING → REFLECTING
        agent.transition(AgentState.REFLECTING)
        assert agent.state == AgentState.REFLECTING

        # Valid: REFLECTING → SLEEPING
        agent.transition(AgentState.SLEEPING)
        assert agent.state == AgentState.SLEEPING
        assert agent.last_sleep is not None

    def test_invalid_transition(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        with pytest.raises(ValueError):
            agent.transition(AgentState.EXECUTING)  # Can't go from SLEEPING to EXECUTING

    def test_identity_files(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "test-01"
        agent_dir.mkdir()
        agent = Agent(id="test-01", directory=agent_dir)

        agent.write_identity("identity", "# Test Agent\n\nI am a test.")
        assert agent.read_identity("identity") == "# Test Agent\n\nI am a test."
        assert agent.read_identity("skills") == ""  # Not yet written

    def test_consciousness_budget(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01", consciousness_budget_limit=3)

        assert agent.consciousness_remaining == 3
        assert agent.spend_consciousness() is True
        assert agent.spend_consciousness() is True
        assert agent.spend_consciousness() is True
        assert agent.spend_consciousness() is False  # Over budget
        assert agent.consciousness_remaining == 0


# ---------------------------------------------------------------------------
# Memory adapter tests
# ---------------------------------------------------------------------------

class TestInMemoryAdapter:
    @pytest.mark.asyncio
    async def test_store_and_search(self) -> None:
        mem = InMemoryAdapter()
        await mem.store("agent-01", "User prefers dark mode", tags=["pref"], importance=8)
        await mem.store("agent-01", "API endpoint is /v2/data", tags=["tech"], importance=5)

        results = await mem.search("agent-01", "dark mode")
        assert len(results) == 1
        assert "dark mode" in results[0].content

    @pytest.mark.asyncio
    async def test_recall_by_importance(self) -> None:
        mem = InMemoryAdapter()
        await mem.store("agent-01", "Low priority note", importance=2)
        await mem.store("agent-01", "Critical finding", importance=9)
        await mem.store("agent-01", "Medium priority", importance=5)

        results = await mem.recall("agent-01", limit=2, min_importance=3)
        assert len(results) == 2
        assert results[0].importance == 9  # Highest first

    @pytest.mark.asyncio
    async def test_namespace_isolation(self) -> None:
        mem = InMemoryAdapter()
        await mem.store("agent-01", "Agent 1 memory")
        await mem.store("agent-02", "Agent 2 memory")

        results_1 = await mem.search("agent-01", "memory")
        results_2 = await mem.search("agent-02", "memory")

        assert len(results_1) == 1
        assert "Agent 1" in results_1[0].content
        assert len(results_2) == 1
        assert "Agent 2" in results_2[0].content


# ---------------------------------------------------------------------------
# Fabric tests
# ---------------------------------------------------------------------------

class TestFabric:
    def _make_fabric(self, tmp_path: Path) -> Fabric:
        """Create a fabric with in-memory adapters for testing."""
        # We need a mock consciousness adapter for fabric tests
        return Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=MockConsciousness(),
        )

    def test_register_agent(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        agent = fabric.register_agent("bookkeep-01")

        assert agent.id == "bookkeep-01"
        assert agent.state == AgentState.SLEEPING
        assert (tmp_path / "agents" / "bookkeep-01" / "identity.md").exists()
        assert (tmp_path / "agents" / "bookkeep-01" / "responsibilities.md").exists()

    def test_discover_agents(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "agent-a").mkdir()
        (agents_dir / "agent-b").mkdir()

        fabric = Fabric(
            agents_dir=agents_dir,
            memory=InMemoryAdapter(),
            consciousness=MockConsciousness(),
        )
        discovered = fabric.discover_agents()
        assert set(discovered) == {"agent-a", "agent-b"}

    def test_status(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        fabric.register_agent("test-01")
        status = fabric.status()

        assert "test-01" in status["agents"]
        assert status["agents"]["test-01"]["state"] == "sleeping"

    @pytest.mark.asyncio
    async def test_wake_and_sleep(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        fabric.register_agent("test-01")

        agent = await fabric.wake("test-01")
        assert agent.state == AgentState.EXECUTING

        agent = await fabric.sleep("test-01")
        assert agent.state == AgentState.SLEEPING


# ---------------------------------------------------------------------------
# Mock adapters for testing
# ---------------------------------------------------------------------------

class MockConsciousness:
    """Mock consciousness adapter that returns canned responses."""

    async def think(self, agent_id, context, prompt, **kwargs):
        from cortiva.adapters.protocols import ConsciousResponse
        return ConsciousResponse(
            content=f"[{agent_id}] Plan: Process tasks as they arrive.",
            tokens_in=100,
            tokens_out=50,
            model="mock",
        )

    async def reflect(self, agent_id, context, day_summary):
        from cortiva.adapters.protocols import ConsciousResponse
        return ConsciousResponse(
            content=f"# {agent_id}\n\nCompleted a productive day.",
            reflection=f"Today went well. Processed tasks efficiently.",
            tokens_in=200,
            tokens_out=100,
            model="mock",
        )
