"""Extended tests for fabric.py — IPC handlers, _conscious_plan, _goals_context,
_process_reflection, _check_approved_tasks, and hook context injection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core.agent import Agent, AgentState, Task, TaskQueue
from cortiva.core.fabric import Fabric
from cortiva.core.reflection import ReflectionSuffix


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _MockConsciousness:
    """Mock consciousness returning parseable plans."""

    async def think(self, agent_id, context, prompt, **kwargs):
        if "plan" in prompt.lower() or "checklist" in prompt.lower():
            return ConsciousResponse(
                content=(
                    "# Plan\n\n"
                    "- [ ] **[CRITICAL]** Review urgent tickets\n"
                    "- [ ] **[HIGH]** Process messages\n"
                    "- [ ] Update report\n"
                ),
                tokens_in=100,
                tokens_out=50,
                model="mock",
            )
        if "adjustment" in prompt.lower() or "updated plan" in prompt.lower():
            return ConsciousResponse(
                content="- [ ] Retry\n- [ ] Escalate\n",
                tokens_in=80,
                tokens_out=40,
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
            reflection="Went well.",
            tokens_in=200,
            tokens_out=100,
            model="mock",
        )


def _make_fabric(tmp_path: Path, **kw) -> Fabric:
    return Fabric(
        agents_dir=tmp_path / "agents",
        memory=InMemoryAdapter(),
        consciousness=_MockConsciousness(),
        **kw,
    )


# ---------------------------------------------------------------------------
# _conscious_plan
# ---------------------------------------------------------------------------


class TestConsciousPlan:
    @pytest.mark.asyncio
    async def test_conscious_plan_returns_content(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("cp-01", consciousness_budget=10)
        agent.transition(AgentState.WAKING)
        agent.transition(AgentState.PLANNING)
        identity = agent.read_all_identity()

        result = await fabric._conscious_plan(
            agent, identity, "ctx", "Make a checklist plan",
            call_type="plan",
        )
        assert result is not None
        assert "Review urgent tickets" in result

    @pytest.mark.asyncio
    async def test_conscious_plan_budget_exhausted(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("cp-02", consciousness_budget=1)
        # Exhaust the budget
        agent.consciousness_budget_limit = 0
        agent.transition(AgentState.WAKING)
        agent.transition(AgentState.PLANNING)
        identity = agent.read_all_identity()

        result = await fabric._conscious_plan(
            agent, identity, "ctx", "Make a checklist plan",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_conscious_plan_on_success_callback(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("cp-03", consciousness_budget=10)
        agent.transition(AgentState.WAKING)
        agent.transition(AgentState.PLANNING)
        identity = agent.read_all_identity()

        captured: list[str] = []
        await fabric._conscious_plan(
            agent, identity, "ctx", "Make a checklist plan",
            on_success=lambda text: captured.append(text),
        )
        assert len(captured) == 1
        assert "Review urgent tickets" in captured[0]


# ---------------------------------------------------------------------------
# _goals_context
# ---------------------------------------------------------------------------


class TestGoalsContext:
    def test_goals_context_no_goals_dir(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        result = fabric._goals_context("agent-1")
        assert result == ""

    def test_goals_context_with_goals_dir(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        goals_dir = fabric.agents_dir / ".goals"
        goals_dir.mkdir(parents=True)
        # Write a minimal objectives file so GoalManager can load
        (goals_dir / "objectives.json").write_text("{}", encoding="utf-8")

        result = fabric._goals_context("agent-1")
        # Should return a string (possibly empty if no goals for agent)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _process_reflection
# ---------------------------------------------------------------------------


class TestProcessReflection:
    def _setup(self, tmp_path: Path) -> tuple[Fabric, Agent, Task]:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("refl-01", consciousness_budget=50)
        agent.transition(AgentState.WAKING)
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)
        task = Task(id="t1", description="Test task", status="done", outcome="OK")
        agent.task_queue = TaskQueue(tasks=[task])
        return fabric, agent, task

    @pytest.mark.asyncio
    async def test_learned_stored_as_memory(self, tmp_path: Path) -> None:
        fabric, agent, task = self._setup(tmp_path)
        suffix = ReflectionSuffix(learned="Never use raw SQL in prod")
        await fabric._process_reflection(agent, task, suffix)

        memories = await fabric.memory.recall(agent.id, limit=10, min_importance=7.0)
        assert any("raw SQL" in m.content for m in memories)

    @pytest.mark.asyncio
    async def test_procedure_update_appended(self, tmp_path: Path) -> None:
        fabric, agent, task = self._setup(tmp_path)
        agent.write_identity("procedures", "# Procedures\n\n- Step 1\n")
        suffix = ReflectionSuffix(procedure_update="## New procedure\n- Do X")
        await fabric._process_reflection(agent, task, suffix)

        updated = agent.read_identity("procedures")
        assert "New procedure" in updated
        assert "Step 1" in updated

    @pytest.mark.asyncio
    async def test_messages_sent_via_channel(self, tmp_path: Path) -> None:
        channel = AsyncMock()
        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=_MockConsciousness(),
            channel=channel,
        )
        agent = fabric.register_agent("refl-02", consciousness_budget=50)
        agent.transition(AgentState.WAKING)
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)
        task = Task(id="t1", description="Test", status="done")
        agent.task_queue = TaskQueue(tasks=[task])

        suffix = ReflectionSuffix(
            messages=[{"to": "agent-b", "content": "Hello from refl-02"}],
        )
        await fabric._process_reflection(agent, task, suffix)

        channel.send.assert_called_once()
        assert channel.send.call_args.kwargs["recipient"] == "agent-b"

    @pytest.mark.asyncio
    async def test_delegation_created(self, tmp_path: Path) -> None:
        fabric, agent, task = self._setup(tmp_path)
        # Register the target agent so delegation can succeed (no org constraints)
        fabric.register_agent("sub-01")

        suffix = ReflectionSuffix(
            delegate=[{"to": "sub-01", "description": "Review docs", "priority": 2}],
        )
        await fabric._process_reflection(agent, task, suffix)

        pending = fabric.delegation.get_assignments_for("sub-01")
        assert len(pending) >= 1

    @pytest.mark.asyncio
    async def test_shared_learning_stored(self, tmp_path: Path) -> None:
        fabric, agent, task = self._setup(tmp_path)
        suffix = ReflectionSuffix(shared_learning="Always validate inputs")
        await fabric._process_reflection(agent, task, suffix)

        shared = await fabric.memory.recall("__org_shared__", limit=5, min_importance=6.0)
        assert any("validate inputs" in m.content for m in shared)

    @pytest.mark.asyncio
    async def test_schedule_request(self, tmp_path: Path) -> None:
        fabric, agent, task = self._setup(tmp_path)
        suffix = ReflectionSuffix(schedule={"overtime": 2.0})
        # Should not raise
        await fabric._process_reflection(agent, task, suffix)

    @pytest.mark.asyncio
    async def test_escalation_persisted(self, tmp_path: Path) -> None:
        fabric, agent, task = self._setup(tmp_path)
        suffix = ReflectionSuffix(escalation="Need manager help")
        await fabric._process_reflection(agent, task, suffix)

        outbox_content = agent.read_outbox("escalations.json")
        assert "Need manager help" in outbox_content


# ---------------------------------------------------------------------------
# _check_approved_tasks
# ---------------------------------------------------------------------------


class TestCheckApprovedTasks:
    def test_reactivates_approved_task(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("appr-01")

        task = Task(
            id="t1", description="Deploy to staging",
            status="pending_approval", priority=0,
        )
        agent.task_queue = TaskQueue(tasks=[task])

        # Manually add an approved request
        fabric.approval_queue.submit(
            agent_id="appr-01",
            task_description="Deploy to staging",
            policy_rule="deploy-requires-approval",
            approver_id="human",
        )
        # Approve it
        req = fabric.approval_queue.all_pending()[0]
        fabric.approval_queue.approve(req.id, "human")

        fabric._check_approved_tasks(agent)

        assert task.status == "pending"
        assert task.priority >= 1  # bumped

    def test_no_task_queue(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("appr-02")
        agent.task_queue = None
        # Should not raise
        fabric._check_approved_tasks(agent)


# ---------------------------------------------------------------------------
# Hook context injection during wake
# ---------------------------------------------------------------------------


class TestWakeHookInjection:
    @pytest.mark.asyncio
    async def test_hook_context_injected(self, tmp_path: Path) -> None:
        from cortiva.core.hooks import HookEvent

        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("hook-01")

        # Directly populate the pending queue (simulating what route() does)
        hook = HookEvent(
            id="hook-1",
            source="github",
            event_type="push",
            payload={"ref": "main"},
            routed_to="hook-01",
        )
        fabric.hook_router._pending.setdefault("hook-01", []).append(hook)

        # Verify context is present before wake
        assert fabric.hook_router.pending_context("hook-01") != ""

        agent = await fabric.wake("hook-01")
        assert agent.state == AgentState.EXECUTING

        # Hooks should have been consumed by pending_for() in wake
        remaining = fabric.hook_router.pending_context("hook-01")
        assert remaining == ""


# ---------------------------------------------------------------------------
# IPC handlers (via _register_ipc_handlers)
# ---------------------------------------------------------------------------


class TestIPCHandlers:
    def _setup_fabric_with_ipc(self, tmp_path: Path) -> tuple[Fabric, dict]:
        fabric = _make_fabric(tmp_path)
        fabric.register_agent("ipc-01")

        # Capture registered handlers by monkey-patching FabricServer
        handlers: dict[str, object] = {}
        mock_server = MagicMock()

        def capture_register(cmd, handler):
            handlers[cmd] = handler

        mock_server.register.side_effect = capture_register
        fabric._register_ipc_handlers(mock_server)
        return fabric, handlers

    @pytest.mark.asyncio
    async def test_handle_status(self, tmp_path: Path) -> None:
        fabric, handlers = self._setup_fabric_with_ipc(tmp_path)
        result = await handlers["status"]()
        assert result["ok"] is True
        assert "agents" in result

    @pytest.mark.asyncio
    async def test_handle_agent_wake(self, tmp_path: Path) -> None:
        fabric, handlers = self._setup_fabric_with_ipc(tmp_path)
        result = await handlers["agent.wake"](agent_id="ipc-01")
        assert result["ok"] is True
        assert result["state"] == "executing"

    @pytest.mark.asyncio
    async def test_handle_agent_wake_missing_id(self, tmp_path: Path) -> None:
        fabric, handlers = self._setup_fabric_with_ipc(tmp_path)
        result = await handlers["agent.wake"]()
        assert result["ok"] is False
        assert "agent_id required" in result["error"]

    @pytest.mark.asyncio
    async def test_handle_agent_sleep(self, tmp_path: Path) -> None:
        fabric, handlers = self._setup_fabric_with_ipc(tmp_path)
        # Wake first
        await fabric.wake("ipc-01")
        result = await handlers["agent.sleep"](agent_id="ipc-01")
        assert result["ok"] is True
        assert result["state"] == "sleeping"

    @pytest.mark.asyncio
    async def test_handle_agent_cycle(self, tmp_path: Path) -> None:
        fabric, handlers = self._setup_fabric_with_ipc(tmp_path)
        await fabric.wake("ipc-01")
        result = await handlers["agent.cycle"](agent_id="ipc-01")
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_handle_agent_cycle_missing_id(self, tmp_path: Path) -> None:
        fabric, handlers = self._setup_fabric_with_ipc(tmp_path)
        result = await handlers["agent.cycle"]()
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_handle_budget_no_manager(self, tmp_path: Path) -> None:
        fabric, handlers = self._setup_fabric_with_ipc(tmp_path)
        result = await handlers["budget"]()
        assert result["ok"] is True
        assert result["budget"] == {}

    @pytest.mark.asyncio
    async def test_handle_discover_not_run(self, tmp_path: Path) -> None:
        fabric, handlers = self._setup_fabric_with_ipc(tmp_path)
        result = await handlers["discover"]()
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_handle_watch(self, tmp_path: Path) -> None:
        fabric, handlers = self._setup_fabric_with_ipc(tmp_path)
        result = await handlers["watch"]()
        assert result["ok"] is True
        assert "agents" in result

    @pytest.mark.asyncio
    async def test_handle_capacity(self, tmp_path: Path) -> None:
        fabric, handlers = self._setup_fabric_with_ipc(tmp_path)
        result = await handlers["capacity"]()
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Wake flow: multi-horizon planning
# ---------------------------------------------------------------------------


class TestWakeMultiHorizonPlanning:
    @pytest.mark.asyncio
    async def test_wake_creates_monthly_and_weekly_plans(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("mh-01", consciousness_budget=50)
        agent = await fabric.wake("mh-01")

        # Plans should have been created
        from cortiva.core.planner import Planner
        planner = Planner(agent.directory)
        assert planner.store.current_monthly() is not None
        assert planner.store.current_weekly() is not None

    @pytest.mark.asyncio
    async def test_wake_skips_existing_plans(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("mh-02", consciousness_budget=50)

        # Pre-create plans
        from cortiva.core.planner import Planner
        planner = Planner(agent.directory)
        planner.save_monthly("Pre-existing monthly plan")
        planner.save_weekly("Pre-existing weekly plan")

        agent = await fabric.wake("mh-02")
        assert agent.state == AgentState.EXECUTING

        # Plans should still be the pre-existing ones
        monthly = planner.store.current_monthly()
        assert monthly is not None
        assert "Pre-existing" in monthly.content
