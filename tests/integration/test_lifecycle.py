"""Full agent lifecycle integration test.

Verifies: create agent → wake → plan → execute → reflect → sleep
using real filesystem but mocked consciousness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortiva.core.agent import AgentState
from cortiva.core.fabric import Fabric

from .conftest import MockConsciousness, create_test_agent


pytestmark = pytest.mark.integration


class TestFullLifecycle:
    """Test a complete agent wake-sleep cycle."""

    async def _run_full_cycle(self, fabric: Fabric, agent_id: str) -> None:
        """Wake → cycle until done → sleep."""
        await fabric.wake(agent_id)
        # Run cycles until all tasks are done (max 10 to prevent infinite loop)
        for _ in range(10):
            result = await fabric.cycle(agent_id)
            if result.get("all_tasks_complete"):
                break
        await fabric.sleep(agent_id)

    @pytest.mark.asyncio
    async def test_single_agent_cycle(
        self,
        fabric: Fabric,
        agents_dir: Path,
        mock_consciousness: MockConsciousness,
    ) -> None:
        """Agent goes through wake → plan → execute → reflect → sleep."""
        create_test_agent(agents_dir)
        fabric.discover_agents()
        agent = fabric.agents["test-agent-01"]

        assert agent.state == AgentState.SLEEPING

        await self._run_full_cycle(fabric, agent.id)

        assert agent.state == AgentState.SLEEPING
        assert len(mock_consciousness.calls) >= 2

        plan_calls = [c for c in mock_consciousness.calls if c.get("metadata", {}).get("call_type") == "plan"]
        assert len(plan_calls) >= 1

        reflect_calls = [c for c in mock_consciousness.calls if c["method"] == "reflect"]
        assert len(reflect_calls) >= 1

    @pytest.mark.asyncio
    async def test_agent_identity_persists(
        self,
        fabric: Fabric,
        agents_dir: Path,
    ) -> None:
        """Agent identity files should exist after a cycle."""
        create_test_agent(agents_dir)
        fabric.discover_agents()
        agent = fabric.agents["test-agent-01"]

        await self._run_full_cycle(fabric, agent.id)

        identity = agent.read_identity("identity")
        assert len(identity) > 0

    @pytest.mark.asyncio
    async def test_journal_written(
        self,
        fabric: Fabric,
        agents_dir: Path,
    ) -> None:
        """Agent should have a journal entry after reflection."""
        create_test_agent(agents_dir)
        fabric.discover_agents()
        agent = fabric.agents["test-agent-01"]

        await self._run_full_cycle(fabric, agent.id)

        journal_dir = agent.directory / "journal"
        journal_files = list(journal_dir.iterdir())
        assert len(journal_files) >= 1

    @pytest.mark.asyncio
    async def test_runtime_state_persisted(
        self,
        fabric: Fabric,
        agents_dir: Path,
    ) -> None:
        """Task queue and metrics should be written to today/."""
        create_test_agent(agents_dir)
        fabric.discover_agents()
        agent = fabric.agents["test-agent-01"]

        await self._run_full_cycle(fabric, agent.id)

        tq_path = agent.directory / "today" / "task_queue.json"
        assert tq_path.exists()
        tq_data = json.loads(tq_path.read_text())
        assert "tasks" in tq_data
        assert "summary" in tq_data

    @pytest.mark.asyncio
    async def test_event_bus_receives_events(
        self,
        fabric: Fabric,
        agents_dir: Path,
    ) -> None:
        """EventBus should capture events during a cycle."""
        create_test_agent(agents_dir)
        fabric.discover_agents()
        agent = fabric.agents["test-agent-01"]

        await self._run_full_cycle(fabric, agent.id)

        recent = fabric.event_bus.recent(limit=50)
        event_types = {e.event_type for e in recent}
        assert "agent.wake" in event_types
        assert "agent.sleep" in event_types


class TestMultipleAgents:
    """Test multiple agents running in the same fabric."""

    async def _run_full_cycle(self, fabric: Fabric, agent_id: str) -> None:
        await fabric.wake(agent_id)
        for _ in range(10):
            result = await fabric.cycle(agent_id)
            if result.get("all_tasks_complete"):
                break
        await fabric.sleep(agent_id)

    @pytest.mark.asyncio
    async def test_two_agents_independent(
        self,
        fabric: Fabric,
        agents_dir: Path,
    ) -> None:
        """Two agents should be able to complete cycles independently."""
        create_test_agent(agents_dir, "agent-alpha")
        create_test_agent(agents_dir, "agent-beta")
        fabric.discover_agents()

        await self._run_full_cycle(fabric, "agent-alpha")
        await self._run_full_cycle(fabric, "agent-beta")

        assert fabric.agents["agent-alpha"].state == AgentState.SLEEPING
        assert fabric.agents["agent-beta"].state == AgentState.SLEEPING

        assert (agents_dir / "agent-alpha" / "today" / "task_queue.json").exists()
        assert (agents_dir / "agent-beta" / "today" / "task_queue.json").exists()

    @pytest.mark.asyncio
    async def test_agents_have_separate_state(
        self,
        fabric: Fabric,
        agents_dir: Path,
    ) -> None:
        """Agent state should be independent."""
        create_test_agent(agents_dir, "agent-alpha")
        create_test_agent(agents_dir, "agent-beta")
        fabric.discover_agents()

        await self._run_full_cycle(fabric, "agent-alpha")

        assert fabric.agents["agent-alpha"].state == AgentState.SLEEPING
        assert fabric.agents["agent-beta"].state == AgentState.SLEEPING
        assert fabric.agents["agent-beta"].tasks_completed_today == 0
