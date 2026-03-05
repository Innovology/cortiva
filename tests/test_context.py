"""Tests for the ContextBuilder and context helpers."""

from pathlib import Path

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.core.agent import Agent, AgentState, Task, TaskQueue
from cortiva.core.context import ContextBuilder, _estimate_tokens, _identity_to_context

# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_basic_arithmetic(self) -> None:
        assert _estimate_tokens("a" * 100) == 25

    def test_empty_string(self) -> None:
        assert _estimate_tokens("") == 0


class TestIdentityToContext:
    def test_renders_sections(self) -> None:
        identity = {
            "identity": "# Agent\n\nI am helpful.",
            "skills": "# Skills\n\nPython, SQL.",
        }
        result = _identity_to_context(identity)
        assert "## Identity" in result
        assert "## Skills" in result
        assert "---" in result

    def test_skips_empty(self) -> None:
        identity = {
            "identity": "# Agent\n\nI am helpful.",
            "skills": "   ",
            "procedures": "",
        }
        result = _identity_to_context(identity)
        assert "## Identity" in result
        assert "## Skills" not in result
        assert "## Procedures" not in result


# ---------------------------------------------------------------------------
# ContextBuilder phase tests
# ---------------------------------------------------------------------------


def _make_agent(tmp_path: Path, agent_id: str = "ctx-01") -> Agent:
    agent_dir = tmp_path / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(id=agent_id, directory=agent_dir, state=AgentState.EXECUTING)
    agent.ensure_workspace()
    agent.write_identity("identity", "# ctx-01\n\nTest agent.")
    agent.write_identity("skills", "# Skills\n\nPython.")
    agent.write_identity("responsibilities", "# Responsibilities\n\nCode review.")
    agent.write_identity("procedures", "# Procedures\n\nRun tests first.")
    agent.write_identity("plan", "- [ ] Task A\n- [x] Task B\n")
    return agent


class TestContextBuilderPlan:
    @pytest.mark.asyncio
    async def test_includes_identity_date_procedures(self, tmp_path: Path) -> None:
        memory = InMemoryAdapter()
        builder = ContextBuilder(memory=memory)
        agent = _make_agent(tmp_path)
        identity = agent.read_all_identity()

        result = await builder.build_plan_context(agent, identity, [])
        assert "## Identity" in result
        assert "## Procedures" in result
        # Date section present
        assert "## Date" in result

    @pytest.mark.asyncio
    async def test_includes_recalled_memories(self, tmp_path: Path) -> None:
        memory = InMemoryAdapter()
        await memory.store("ctx-01", "Important past event", importance=9.0)
        builder = ContextBuilder(memory=memory)
        agent = _make_agent(tmp_path)
        identity = agent.read_all_identity()

        result = await builder.build_plan_context(agent, identity, [])
        assert "Important past event" in result
        assert "Recent Memories" in result


class TestContextBuilderExecution:
    @pytest.mark.asyncio
    async def test_includes_task_memories_and_skills(self, tmp_path: Path) -> None:
        memory = InMemoryAdapter()
        await memory.store("ctx-01", "Previous code review notes", importance=7.0, tags=["review"])
        builder = ContextBuilder(memory=memory)
        agent = _make_agent(tmp_path)
        agent.task_queue = TaskQueue(tasks=[
            Task(id="t1", description="Review PR", status="in_progress"),
        ])
        identity = agent.read_all_identity()

        result = await builder.build_execution_context(
            agent, identity, [], "code review",
        )
        assert "## Skills" in result
        assert "## Responsibilities" in result
        assert "Previous code review notes" in result


class TestContextBuilderReplan:
    @pytest.mark.asyncio
    async def test_includes_exceptions_and_completion(self, tmp_path: Path) -> None:
        memory = InMemoryAdapter()
        builder = ContextBuilder(memory=memory)
        agent = _make_agent(tmp_path)
        agent.task_queue = TaskQueue(
            tasks=[
                Task(id="t1", description="Task A", status="done"),
                Task(id="t2", description="Task B", status="pending"),
            ],
            exceptions=[
                Task(id="t3", description="Task C", status="exception", error="timeout"),
            ],
        )
        identity = agent.read_all_identity()

        result = await builder.build_replan_context(agent, identity, [])
        assert "## Exceptions" in result
        assert "timeout" in result
        assert "## Plan Completion" in result
        assert "50%" in result  # 1 done out of 2


class TestContextBuilderReflection:
    @pytest.mark.asyncio
    async def test_includes_day_summary(self, tmp_path: Path) -> None:
        memory = InMemoryAdapter()
        builder = ContextBuilder(memory=memory)
        agent = _make_agent(tmp_path)
        identity = agent.read_all_identity()

        result = await builder.build_reflection_context(
            agent, identity, "Tasks completed: 5\nGood day overall.",
        )
        assert "## Day Summary" in result
        assert "Tasks completed: 5" in result
        assert "## Identity" in result


# ---------------------------------------------------------------------------
# Truncation tests
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_low_priority_dropped_under_tight_budget(self) -> None:
        memory = InMemoryAdapter()
        builder = ContextBuilder(memory=memory, max_tokens=50)
        # 50 tokens = 200 chars budget

        high_section = "A" * 150  # fits
        low_section = "B" * 150  # won't fit

        result = builder._truncate([
            (100, high_section),
            (10, low_section),
        ])
        assert "A" * 150 in result
        assert "B" * 150 not in result
