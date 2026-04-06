"""Tests for core Cortiva functionality."""

from pathlib import Path

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core.agent import Agent, AgentState, Task, TaskQueue
from cortiva.core.context import ContextBuilder
from cortiva.core.fabric import Fabric, _parse_plan

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

    def test_ensure_workspace(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "test-01"
        agent_dir.mkdir()
        agent = Agent(id="test-01", directory=agent_dir)

        agent.ensure_workspace()
        for subdir in ["identity", "today", "outbox", "journal", "workspace"]:
            assert (agent_dir / subdir).is_dir()

    def test_migrate_flat_layout(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "test-01"
        agent_dir.mkdir()
        # Create flat layout
        (agent_dir / "identity.md").write_text("# Identity")
        (agent_dir / "soul.md").write_text("# Soul")
        (agent_dir / "skills.md").write_text("# Skills")
        (agent_dir / "responsibilities.md").write_text("# Resp")
        (agent_dir / "procedures.md").write_text("# Proc")
        (agent_dir / "plan.md").write_text("# Plan")

        agent = Agent(id="test-01", directory=agent_dir)
        assert agent.migrate_flat_layout() is True

        # Files moved to subdirs
        assert (agent_dir / "identity" / "identity.md").read_text() == "# Identity"
        assert (agent_dir / "identity" / "soul.md").read_text() == "# Soul"
        assert (agent_dir / "today" / "plan.md").read_text() == "# Plan"

        # Flat files no longer exist
        assert not (agent_dir / "identity.md").exists()
        assert not (agent_dir / "plan.md").exists()

        # Second call is a no-op
        assert agent.migrate_flat_layout() is False

    def test_migrate_skips_when_already_nested(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "test-01"
        agent_dir.mkdir()
        (agent_dir / "identity").mkdir()
        (agent_dir / "identity" / "identity.md").write_text("# Nested")

        agent = Agent(id="test-01", directory=agent_dir)
        assert agent.migrate_flat_layout() is False


class TestAgentTodayOutbox:
    def test_write_and_read_today(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        agent.write_today("notes.md", "# Notes\n\nToday was productive.")
        assert agent.read_today("notes.md") == "# Notes\n\nToday was productive."

    def test_read_today_missing_file(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        assert agent.read_today("nonexistent.md") == ""

    def test_write_and_read_outbox(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        agent.ensure_workspace()
        outbox_path = agent.outbox_path("msg-01.md")
        outbox_path.write_text("Hello from agent", encoding="utf-8")
        assert agent.read_outbox("msg-01.md") == "Hello from agent"

    def test_flush_outbox(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        agent.ensure_workspace()
        agent.outbox_path("a.md").write_text("Content A", encoding="utf-8")
        agent.outbox_path("b.md").write_text("Content B", encoding="utf-8")

        result = agent.flush_outbox()
        assert result == {"a.md": "Content A", "b.md": "Content B"}
        # Files should be deleted after flush
        assert not agent.outbox_path("a.md").exists()
        assert not agent.outbox_path("b.md").exists()

    def test_flush_outbox_empty(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        assert agent.flush_outbox() == {}

    def test_write_outbox(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        agent.write_outbox("escalations.json", '{"task": "review"}')
        assert agent.read_outbox("escalations.json") == '{"task": "review"}'


class TestRuntimePersistence:
    def test_persist_runtime_state(self, tmp_path: Path) -> None:
        import json
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        agent.ensure_workspace()
        agent.task_queue = TaskQueue(
            tasks=[
                Task(id="t1", description="Write tests", status="done", outcome="All pass"),
                Task(id="t2", description="Fix bug", status="pending"),
            ],
            exceptions=[Task(id="t3", description="Deploy", error="Timeout")],
            replan_count=1,
        )
        agent.persist_runtime_state()

        tq = json.loads(agent.read_today("task_queue.json"))
        assert len(tq["tasks"]) == 2
        assert tq["tasks"][0]["status"] == "done"
        assert tq["replan_count"] == 1
        assert tq["summary"]["done"] == 1

        exc = json.loads(agent.read_today("exception_pile.json"))
        assert len(exc) == 1
        assert exc[0]["error"] == "Timeout"

    def test_persist_runtime_state_noop_without_queue(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        agent.persist_runtime_state()  # should not raise
        assert agent.read_today("task_queue.json") == ""

    def test_persist_familiarity(self, tmp_path: Path) -> None:
        import json
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        signals = [
            {"task": "Review PR", "strength": "familiar", "valence": "positive", "match_count": 3},
        ]
        agent.persist_familiarity(signals)
        loaded = json.loads(agent.read_today("familiarity_signals.json"))
        assert loaded[0]["strength"] == "familiar"

    def test_reset_today(self, tmp_path: Path) -> None:
        agent = Agent(id="test-01", directory=tmp_path / "test-01")
        agent.ensure_workspace()
        agent.write_today("plan.md", "# Plan")
        agent.write_today("task_queue.json", "{}")
        assert agent.read_today("plan.md") != ""

        agent.reset_today()
        assert agent.read_today("plan.md") == ""
        assert agent.read_today("task_queue.json") == ""
        # Directory itself still exists
        assert (agent.directory / "today").is_dir()


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
        assert (tmp_path / "agents" / "bookkeep-01" / "identity" / "identity.md").exists()
        assert (tmp_path / "agents" / "bookkeep-01" / "identity" / "responsibilities.md").exists()
        assert (tmp_path / "agents" / "bookkeep-01" / "today" / "plan.md").exists()

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
        assert agent.task_queue is not None

        agent = await fabric.sleep("test-01")
        assert agent.state == AgentState.SLEEPING
        assert agent.task_queue is None

    @pytest.mark.asyncio
    async def test_start_and_stop(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        fabric.register_agent("runner-01")

        await fabric.start()
        assert fabric._running is True
        assert "runner-01" in fabric.agents

        await fabric.stop()
        assert fabric._running is False
        assert fabric.agents["runner-01"].state == AgentState.SLEEPING


# ---------------------------------------------------------------------------
# Plan parsing tests
# ---------------------------------------------------------------------------

class TestPlanParsing:
    def test_checkbox_format(self) -> None:
        plan = (
            "# Plan\n\n"
            "- [ ] Send weekly report\n"
            "- [x] Review pull requests\n"
            "- [ ] Update documentation\n"
        )
        tq = _parse_plan(plan)
        assert len(tq.tasks) == 3
        assert tq.tasks[0].description == "Send weekly report"
        assert tq.tasks[0].status == "pending"
        assert tq.tasks[1].description == "Review pull requests"
        assert tq.tasks[1].status == "done"
        assert tq.tasks[2].description == "Update documentation"
        assert tq.tasks[2].status == "pending"

    def test_priority_markers(self) -> None:
        plan = (
            "- [ ] **[CRITICAL]** Fix production bug\n"
            "- [ ] **[HIGH]** Deploy hotfix\n"
            "- [ ] Normal task\n"
        )
        tq = _parse_plan(plan)
        assert len(tq.tasks) == 3
        assert tq.tasks[0].priority == 2
        assert tq.tasks[0].description == "Fix production bug"
        assert tq.tasks[1].priority == 1
        assert tq.tasks[1].description == "Deploy hotfix"
        assert tq.tasks[2].priority == 0

    def test_numbered_lists(self) -> None:
        plan = (
            "1. First task\n"
            "2. Second task\n"
            "3. Third task\n"
        )
        tq = _parse_plan(plan)
        assert len(tq.tasks) == 3
        assert tq.tasks[0].description == "First task"
        assert tq.tasks[2].description == "Third task"

    def test_empty_plan(self) -> None:
        plan = "# Plan\n\nNo tasks today.\n"
        tq = _parse_plan(plan)
        assert len(tq.tasks) == 0
        assert tq.all_done() is True

    def test_mixed_format(self) -> None:
        plan = (
            "# Today's Plan\n\n"
            "- [ ] **[CRITICAL]** Urgent fix\n"
            "- [x] Already done\n"
            "3. Numbered item\n"
            "- Plain bullet\n"
        )
        tq = _parse_plan(plan)
        assert len(tq.tasks) == 4
        assert tq.tasks[0].priority == 2
        assert tq.tasks[1].status == "done"
        assert tq.tasks[2].description == "Numbered item"
        assert tq.tasks[3].description == "Plain bullet"


# ---------------------------------------------------------------------------
# TaskQueue tests
# ---------------------------------------------------------------------------

class TestTaskQueue:
    def test_next_pending_priority_ordering(self) -> None:
        tq = TaskQueue(tasks=[
            Task(id="t1", description="Low", priority=0),
            Task(id="t2", description="Critical", priority=2),
            Task(id="t3", description="High", priority=1),
        ])
        nxt = tq.next_pending()
        assert nxt is not None
        assert nxt.id == "t2"  # highest priority first

    def test_all_done(self) -> None:
        tq = TaskQueue(tasks=[
            Task(id="t1", description="A", status="done"),
            Task(id="t2", description="B", status="skipped"),
        ])
        assert tq.all_done() is True

        tq.tasks.append(Task(id="t3", description="C", status="pending"))
        assert tq.all_done() is False

    def test_completion_summary(self) -> None:
        tq = TaskQueue(
            tasks=[
                Task(id="t1", description="A", status="done"),
                Task(id="t2", description="B", status="done"),
                Task(id="t3", description="C", status="pending"),
                Task(id="t4", description="D", status="exception"),
            ],
            exceptions=[Task(id="t4", description="D", status="exception", error="fail")],
        )
        summary = tq.completion_summary()
        assert summary["done"] == 2
        assert summary["pending"] == 1
        assert summary["exception"] == 1
        assert summary["exceptions"] == 1  # from exception pile


# ---------------------------------------------------------------------------
# Cycle tests
# ---------------------------------------------------------------------------

class TestCycle:
    def _make_fabric(self, tmp_path: Path, consciousness=None, budget: int = 50) -> Fabric:
        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=consciousness or MockConsciousness(),
        )
        fabric.register_agent("worker-01", consciousness_budget=budget)
        return fabric

    @pytest.mark.asyncio
    async def test_executes_task(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        agent = await fabric.wake("worker-01")

        # The mock returns a plan with tasks; cycle should execute one
        result = await fabric.cycle("worker-01")
        assert result["action"] == "executed_task"
        assert result["conscious_call"] is True
        assert result["task"] is not None
        assert agent.tasks_completed_today >= 1

    @pytest.mark.asyncio
    async def test_all_tasks_complete(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path)
        agent = await fabric.wake("worker-01")

        # Mark all tasks as done
        assert agent.task_queue is not None
        for task in agent.task_queue.tasks:
            task.status = "done"

        result = await fabric.cycle("worker-01")
        assert result["action"] == "idle"
        assert result["all_tasks_complete"] is True

    @pytest.mark.asyncio
    async def test_budget_exhaustion_defers(self, tmp_path: Path) -> None:
        # Budget of 4: 3 for wake planning (monthly+weekly+daily), 1 for
        # first task, then budget exhausted on the second task.
        fabric = self._make_fabric(tmp_path, budget=4)
        agent = await fabric.wake("worker-01")  # spends up to 3

        # First cycle should succeed (spends 1)
        result1 = await fabric.cycle("worker-01")
        assert result1["action"] == "executed_task"

        # Second cycle: budget exhausted, task deferred to exceptions
        result2 = await fabric.cycle("worker-01")
        assert result2["action"] == "executed_task"
        assert agent.task_queue is not None
        assert len(agent.task_queue.exceptions) > 0
        assert agent.task_queue.exceptions[0].error == "Budget exhausted"

    @pytest.mark.asyncio
    async def test_replan_triggered_by_exceptions(self, tmp_path: Path) -> None:
        fabric = self._make_fabric(tmp_path, budget=50)
        agent = await fabric.wake("worker-01")
        assert agent.task_queue is not None

        # Manually fill exception pile to trigger replan
        for i in range(3):
            exc_task = Task(
                id=f"exc-{i}", description=f"Failed task {i}",
                status="exception", error="test error",
            )
            agent.task_queue.exceptions.append(exc_task)

        result = await fabric.cycle("worker-01")
        assert result["action"] == "replanned"
        assert result["conscious_call"] is True


# ---------------------------------------------------------------------------
# Plan vs Reality tests
# ---------------------------------------------------------------------------

class TestPlanVsReality:
    @pytest.mark.asyncio
    async def test_day_summary_includes_completion_stats(self, tmp_path: Path) -> None:
        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=MockConsciousness(),
        )
        fabric.register_agent("stats-01")
        agent = await fabric.wake("stats-01")

        # Execute a couple of cycles
        await fabric.cycle("stats-01")

        summary = ContextBuilder.build_day_summary(agent)
        assert "Plan vs Reality" in summary
        assert "Total tasks:" in summary
        assert "Completed:" in summary
        assert "Completion rate:" in summary
        assert "Replans:" in summary


# ---------------------------------------------------------------------------
# Full cycle tests
# ---------------------------------------------------------------------------

class TestFullCycle:
    @pytest.mark.asyncio
    async def test_full_day_cycle(self, tmp_path: Path) -> None:
        """wake → execute tasks until idle → sleep; verify journal and state."""
        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=MockConsciousness(),
        )
        agent = fabric.register_agent("cycle-01")
        assert agent.state == AgentState.SLEEPING

        # Wake: plan is created, state is EXECUTING
        agent = await fabric.wake("cycle-01")
        assert agent.state == AgentState.EXECUTING
        assert agent.task_queue is not None
        initial_task_count = len(agent.task_queue.tasks)
        assert initial_task_count > 0

        # Execute cycles until all tasks are complete
        for _ in range(initial_task_count + 2):  # safety margin
            result = await fabric.cycle("cycle-01")
            if result["all_tasks_complete"]:
                break

        assert agent.task_queue.all_done()

        # Sleep: reflection written, state back to SLEEPING
        agent = await fabric.sleep("cycle-01")
        assert agent.state == AgentState.SLEEPING
        assert agent.task_queue is None

        # Journal entry should exist with plan-vs-reality content
        journal = agent.journal_path()
        assert journal.exists()
        assert journal.read_text(encoding="utf-8") != ""


# ---------------------------------------------------------------------------
# Mock adapters for testing
# ---------------------------------------------------------------------------

class MockConsciousness:
    """Mock consciousness adapter that returns canned responses."""

    async def think(self, agent_id, context, prompt, **kwargs):
        # If it's a planning prompt, return a parseable checklist
        if "plan" in prompt.lower() or "checklist" in prompt.lower():
            return ConsciousResponse(
                content=(
                    "# Today's Plan\n\n"
                    "- [ ] **[CRITICAL]** Review urgent tickets\n"
                    "- [ ] **[HIGH]** Process incoming messages\n"
                    "- [ ] Update weekly report\n"
                ),
                tokens_in=100,
                tokens_out=50,
                model="mock",
            )

        # If it's a replan prompt, return an updated checklist
        if "adjustment" in prompt.lower() or "updated plan" in prompt.lower():
            return ConsciousResponse(
                content=(
                    "# Updated Plan\n\n"
                    "- [ ] Retry failed tasks with new approach\n"
                    "- [ ] Escalate unresolvable issues\n"
                ),
                tokens_in=100,
                tokens_out=50,
                model="mock",
            )

        # Default: task execution response
        return ConsciousResponse(
            content=f"[{agent_id}] Completed task successfully.",
            tokens_in=100,
            tokens_out=50,
            model="mock",
        )

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(
            content=f"# {agent_id}\n\nCompleted a productive day.",
            reflection="Today went well. Processed tasks efficiently.",
            tokens_in=200,
            tokens_out=100,
            model="mock",
        )
