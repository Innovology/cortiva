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
    async def test_shared_learning_stays_private(self, tmp_path: Path) -> None:
        """Isolation (founder directive 2026-06-07): a 'shared_learning'
        is kept as the agent's OWN private memory and never written to the
        org-shared tier, so it cannot bleed into other agents' plans."""
        fabric, agent, task = self._setup(tmp_path)
        suffix = ReflectionSuffix(shared_learning="Always validate inputs")
        await fabric._process_reflection(agent, task, suffix)

        # Nothing in the shared tier.
        shared = await fabric.memory.recall(
            "__org_shared__", limit=5, min_importance=6.0,
        )
        assert not any("validate inputs" in m.content for m in shared)

        # It's in the agent's own memory instead.
        own = await fabric.memory.recall(agent.id, limit=5, min_importance=6.0)
        assert any("validate inputs" in m.content for m in own)

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


# ---------------------------------------------------------------------------
# Org derived from agents' deploy.yaml (refresh_org_from_agents)
# ---------------------------------------------------------------------------


def _write_deploy(agents_dir: Path, slug: str, **agent_fields) -> None:
    import yaml

    d = agents_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "deploy.yaml").write_text(
        yaml.safe_dump({"agent": agent_fields}, sort_keys=False),
        encoding="utf-8",
    )


class TestOrgFromAgents:
    def test_derives_reporting_and_departments(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agents_dir = tmp_path / "agents"
        _write_deploy(agents_dir, "ceo", name="Maren", department="executive",
                      reports_to="human-founder", authority_level=5)
        _write_deploy(agents_dir, "cpo", name="Astrid", department="product",
                      reports_to="ceo", authority_level=5)
        _write_deploy(agents_dir, "po", name="Yuki", department="product",
                      reports_to="cpo")
        _write_deploy(agents_dir, "dev", name="Amara", department="engineering",
                      reports_to="po")

        fabric.discover_agents()

        assert fabric.org is not None
        # human-founder is outside the org → ceo has no in-org manager
        assert fabric.org.manager_of("ceo") is None
        assert fabric.org.manager_of("cpo") == "ceo"
        assert fabric.org.manager_of("dev") == "po"
        # subordinates
        assert set(fabric.org.subordinates_of("cpo")) == {"po"}
        # departments → peers
        assert set(fabric.org.peers_of("cpo")) == {"po"}
        # delegation authority follows the reporting line
        assert fabric.org.can_delegate_to("cpo", "po") is True
        assert fabric.org.can_delegate_to("dev", "po") is False

    def test_org_context_names_manager_and_reports(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agents_dir = tmp_path / "agents"
        _write_deploy(agents_dir, "ceo", name="Maren", department="executive",
                      reports_to="human-founder")
        _write_deploy(agents_dir, "cpo", name="Astrid", department="product",
                      reports_to="ceo")
        _write_deploy(agents_dir, "po", name="Yuki", department="product",
                      reports_to="cpo")

        fabric.discover_agents()
        ctx = fabric.org.org_context_for("cpo")
        assert "Manager: ceo" in ctx
        assert "po" in ctx  # direct report listed

    def test_explicit_config_not_overridden(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        # Pin an explicit org as config.py would
        from cortiva.core.org import parse_org_config

        fabric.org = parse_org_config({"reporting": {"b": "a"}})
        fabric._org_from_config = True
        agents_dir = tmp_path / "agents"
        _write_deploy(agents_dir, "x", name="X", department="d", reports_to="y")
        _write_deploy(agents_dir, "y", name="Y", department="d",
                      reports_to="human")

        fabric.discover_agents()
        # Untouched: still the explicit config, not derived from deploy.yaml
        assert fabric.org.manager_of("b") == "a"
        assert fabric.org.manager_of("x") is None


# ---------------------------------------------------------------------------
# AR Scheduler: optimize_schedule runtime wiring (handler -> tool -> apply)
# ---------------------------------------------------------------------------


class TestScheduleOptimization:
    def _org_on_disk(self, tmp_path: Path) -> Fabric:
        fabric = _make_fabric(tmp_path)
        agents_dir = tmp_path / "agents"
        # COO has scheduling authority; give it reports so it's a manager.
        _write_deploy(agents_dir, "coo", name="Marcus", department="operations",
                      reports_to="ceo")
        _write_deploy(agents_dir, "ceo", name="Maren", department="executive",
                      reports_to="human-founder")
        for i in range(6):
            _write_deploy(agents_dir, f"dev-{i}", name=f"Dev{i}",
                          department="engineering", reports_to="coo")
        fabric.discover_agents()
        return fabric

    @pytest.mark.asyncio
    async def test_authorised_agent_optimises_and_applies(self, tmp_path: Path) -> None:
        fabric = self._org_on_disk(tmp_path)
        coo = fabric.get_agent("coo")

        await fabric._run_schedule_optimization(coo, {"capacity_ceiling": 200})

        # Schedules were registered with the live scheduler for the workforce.
        sched = fabric.scheduler.get_schedule("dev-0")
        assert sched is not None and sched.entries, "dev-0 got no schedule"
        # Persisted to disk for restart survival.
        assert (tmp_path / "agents" / ".schedules.json").exists()
        # Reviewable artifact written.
        note = (tmp_path / "agents" / "coo" / "today" / "schedule_optimization.md")
        assert note.exists()
        assert "Applied:** True" in note.read_text()

    @pytest.mark.asyncio
    async def test_unauthorised_agent_is_ignored(self, tmp_path: Path) -> None:
        fabric = self._org_on_disk(tmp_path)
        dev = fabric.get_agent("dev-0")  # an IC, no scheduling authority

        await fabric._run_schedule_optimization(dev, {"capacity_ceiling": 200})

        # Nothing applied — no persisted schedule, no artifact.
        assert not (tmp_path / "agents" / ".schedules.json").exists()
        assert not (tmp_path / "agents" / "dev-0" / "today"
                    / "schedule_optimization.md").exists()

    @pytest.mark.asyncio
    async def test_persisted_rota_reloads_on_start(self, tmp_path: Path) -> None:
        fabric = self._org_on_disk(tmp_path)
        coo = fabric.get_agent("coo")
        await fabric._run_schedule_optimization(coo, {"capacity_ceiling": 200})

        # A fresh fabric over the same dir reloads the optimised rota.
        fresh = _make_fabric(tmp_path)
        fresh.discover_agents()
        fresh._load_persisted_schedules()
        assert fresh.scheduler.get_schedule("dev-0") is not None


class TestToolCallExecutionPath:
    @pytest.mark.asyncio
    async def test_tool_call_applies_rota_through_execution(self, tmp_path: Path) -> None:
        """A native optimize_schedule tool_call flows through _execute_task
        -> overlay -> _process_reflection -> _run_schedule_optimization and
        applies the rota (no prose suffix needed)."""

        class _ToolMock:
            async def think(self, agent_id, context, prompt, **kwargs):
                if "plan" in prompt.lower() or "checklist" in prompt.lower():
                    return ConsciousResponse(content="- [ ] Optimise the rota\n", model="m")
                # Execution call: return a native tool_call, no prose suffix.
                return ConsciousResponse(
                    content="Optimising the workforce rota.",
                    model="m",
                    tool_calls=[{"name": "optimize_schedule",
                                 "arguments": {"capacity_ceiling": 200, "apply": True}}],
                )
            async def reflect(self, agent_id, context, day_summary):
                return ConsciousResponse(content="", model="m")

        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=_ToolMock(),
        )
        agents_dir = tmp_path / "agents"
        _write_deploy(agents_dir, "coo", name="Marcus", department="operations",
                      reports_to="ceo")
        _write_deploy(agents_dir, "ceo", name="Maren", department="executive",
                      reports_to="human-founder")
        for i in range(4):
            _write_deploy(agents_dir, f"dev-{i}", name=f"D{i}",
                          department="engineering", reports_to="coo")
        fabric.discover_agents()

        coo = fabric.get_agent("coo")
        coo.task_queue = TaskQueue(tasks=[
            Task(id="t1", description="Optimise the workforce rota", status="pending", priority=2),
        ])
        coo.transition(AgentState.WAKING)
        coo.transition(AgentState.PLANNING)
        coo.transition(AgentState.EXECUTING)

        await fabric.cycle("coo")

        assert (tmp_path / "agents" / ".schedules.json").exists(), \
            "tool_call did not apply the rota"
        note = tmp_path / "agents" / "coo" / "today" / "schedule_optimization.md"
        assert note.exists() and "Applied:** True" in note.read_text()


class TestScheduleDebounce:
    def _org(self, tmp_path):
        fabric = _make_fabric(tmp_path)
        ad = tmp_path / "agents"
        _write_deploy(ad, "coo", name="M", department="ops", reports_to="ceo")
        _write_deploy(ad, "ceo", name="Mn", department="exec", reports_to="human")
        for i in range(4):
            _write_deploy(ad, f"d{i}", name=f"D{i}", department="eng", reports_to="coo")
        fabric.discover_agents()
        return fabric

    @pytest.mark.asyncio
    async def test_second_identical_run_is_debounced(self, tmp_path: Path) -> None:
        fabric = self._org(tmp_path)
        coo = fabric.get_agent("coo")
        await fabric._run_schedule_optimization(coo, {"capacity_ceiling": 200})
        note1 = (tmp_path / "agents" / "coo" / "today" / "schedule_optimization.md").read_text()
        assert "Applied:** True" in note1
        assert (tmp_path / "agents" / ".schedule_state.json").exists()

        # Identical inputs → debounced (no re-apply).
        await fabric._run_schedule_optimization(coo, {"capacity_ceiling": 200})
        note2 = (tmp_path / "agents" / "coo" / "today" / "schedule_optimization.md").read_text()
        assert "debounced" in note2.lower() or "Applied:** False" in note2

    @pytest.mark.asyncio
    async def test_changed_weights_reapply(self, tmp_path: Path) -> None:
        fabric = self._org(tmp_path)
        coo = fabric.get_agent("coo")
        await fabric._run_schedule_optimization(coo, {"capacity_ceiling": 200})
        # Different ceiling = material change → applies again.
        await fabric._run_schedule_optimization(coo, {"capacity_ceiling": 150})
        note = (tmp_path / "agents" / "coo" / "today" / "schedule_optimization.md").read_text()
        assert "Applied:** True" in note


class TestPreSleepJournalRitual:
    async def _sleep_once(self, fabric, agent):
        agent.task_queue = TaskQueue(tasks=[
            Task(id="t", description="did work", status="done", outcome="ok"),
        ])
        agent.transition(AgentState.WAKING)
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)
        await fabric.sleep(agent.id)

    @pytest.mark.asyncio
    async def test_sleep_writes_timestamped_entry_with_feelings(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("ritual-1", consciousness_budget=50)
        agent.write_today("emotions.json",
                          '{"satisfaction":0.6,"frustration":0.1,"curiosity":0.7,'
                          '"confidence":0.6,"caution":0.1}')
        await self._sleep_once(fabric, agent)

        journal = (agent.journal_path()).read_text()
        assert "pre-sleep reflection" in journal
        assert "How I feel:" in journal
        # mood label derived from the emotion reading
        assert "accomplished" in journal or "satisf" in journal.lower()

    @pytest.mark.asyncio
    async def test_multiple_sleeps_append_not_overwrite(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("ritual-2", consciousness_budget=80)
        agent.write_today("emotions.json", '{"curiosity":0.7}')
        await self._sleep_once(fabric, agent)
        await self._sleep_once(fabric, agent)
        journal = (agent.journal_path()).read_text()
        # Two timestamped sections, not one overwritten.
        assert journal.count("pre-sleep reflection") == 2

    @pytest.mark.asyncio
    async def test_identity_regen_paced_once_per_day(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("ritual-3", consciousness_budget=80)
        assert fabric._identity_regen_due(agent) is True
        await self._sleep_once(fabric, agent)
        # After one sleep today, identity regen is no longer due.
        assert fabric._identity_regen_due(agent) is False


class TestRoutineDeferDoesNotKill:
    @pytest.mark.asyncio
    async def test_defer_falls_through_to_consciousness(self, tmp_path: Path) -> None:
        """A routine 'defer' must NOT bin the task as an exception — it falls
        through to consciousness so the work actually gets done."""

        class _DeferRoutine:
            async def assess(self, **kw):
                return {"action": "defer", "procedure_match": "x", "confidence": 0.5}

        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=_MockConsciousness(),
            routine=_DeferRoutine(),
        )
        agent = fabric.register_agent("d-1", consciousness_budget=50)
        task = Task(id="t1", description="Send the cadence template to the team",
                    status="pending", priority=1)
        agent.task_queue = TaskQueue(tasks=[task])
        agent.transition(AgentState.WAKING)
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        await fabric.cycle("d-1")

        assert task.status == "done", "defer killed the task instead of doing it"
        assert task not in agent.task_queue.exceptions


class TestSleepGapCatchUp:
    def test_in_sleep_gap_detects_overrun(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        fabric = _make_fabric(tmp_path)
        fabric.scheduler.register("g-1", {"wake": "08:00", "sleep": "16:00"})

        def at(h, m=0):
            return datetime(2026, 6, 7, h, m, tzinfo=UTC)

        assert fabric._in_sleep_gap("g-1", at(17)) is True   # past sleep
        assert fabric._in_sleep_gap("g-1", at(12)) is False  # working
        assert fabric._in_sleep_gap("g-1", at(16, 5)) is False  # within grace

    def test_multi_window_gap(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        fabric = _make_fabric(tmp_path)
        fabric.scheduler.register(
            "g-2", {"wake": "08:00,12:00", "sleep": "10:00,14:00"},
        )

        def at(h, m=0):
            return datetime(2026, 6, 7, h, m, tzinfo=UTC)

        assert fabric._in_sleep_gap("g-2", at(11)) is True   # gap between windows
        assert fabric._in_sleep_gap("g-2", at(9)) is False   # in window 1
        assert fabric._in_sleep_gap("g-2", at(13)) is False  # in window 2


class TestOrphanedSessionReconcile:
    def test_reconcile_closes_open_session_and_journals(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("o-1", consciousness_budget=50)
        # Simulate an open session left by a crash (clock in, never out).
        fabric.timesheet_manager.clock_in("o-1", scheduled_hours=8.0)

        fabric._reconcile_orphaned_sessions()

        today = fabric.timesheet_manager.get("o-1").today()
        assert all(e.sleep_time is not None for e in today.entries), "session left open"
        journal = agent.journal_path().read_text()
        assert "restart" in journal.lower() and "pre-sleep reflection" in journal.lower()


class TestEmailInboxContext:
    def test_inbox_injected_and_marked_read(self, tmp_path: Path) -> None:
        import json
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("ceo", consciousness_budget=10)
        inbox = agent.directory / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "m1.json").write_text(json.dumps({
            "from": "alexander.browne@innovology.io",
            "subject": "Welcome to the team",
            "text": "Glad to have you, Maren.",
        }), encoding="utf-8")

        ctx = fabric._email_inbox_context(agent)
        assert "New Mail" in ctx or "notification" in ctx.lower()
        assert "Welcome to the team" in ctx
        assert "alexander.browne@innovology.io" in ctx
        # consumed: moved to read/, not re-surfaced next time
        assert not (inbox / "m1.json").exists()
        assert (inbox / "read" / "m1.json").exists()
        assert fabric._email_inbox_context(agent) == ""

    def test_no_inbox_is_empty(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("x", consciousness_budget=10)
        assert fabric._email_inbox_context(agent) == ""


# ---------------------------------------------------------------------------
# _backfill_convictions — one-time, idempotent worldview backfill
# ---------------------------------------------------------------------------


class _FakeDeepThink:
    """Stand-in for the deep_think wrapper result (has a .text attribute)."""

    def __init__(self, text: str) -> None:
        self.text = text


class TestBackfillConvictions:
    def _seed_soul(self, fabric: Fabric, aid: str, body: str) -> Path:
        agent_dir = fabric.agents_dir / aid / "identity"
        agent_dir.mkdir(parents=True, exist_ok=True)
        soul = agent_dir / "soul.md"
        soul.write_text(body, encoding="utf-8")
        (agent_dir / "identity.md").write_text(
            f"# {aid}\n\nA seasoned operator who values clarity.\n",
            encoding="utf-8",
        )
        (fabric.agents_dir / aid / "deploy.yaml").write_text(
            f"agent:\n  name: {aid.title()}\n  role: Engineer\n",
            encoding="utf-8",
        )
        return soul

    @pytest.mark.asyncio
    async def test_backfills_soul_without_convictions(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fabric = _make_fabric(tmp_path)
        soul = self._seed_soul(
            fabric, "maren", "---\nagent_id: maren\n---\n\n# Maren — Persona\n\n"
            "Ambition: quietly relentless. Social style: reserved.\n",
        )
        fabric.register_agent("maren", consciousness_budget=10)

        conviction = (
            "I believe reliability is a moral position, not a preference. "
            "Most teams optimise for the demo and pay for it for years. "
            "Hill I'll die on: no untested path ships. " * 3
        )
        monkeypatch.setattr(
            "cortiva.skills.claude_code_deep_think.wrapper.deep_think",
            lambda *a, **k: _FakeDeepThink(conviction),
        )

        await fabric._backfill_convictions()

        text = soul.read_text(encoding="utf-8")
        assert fabric._CONVICTIONS_HEADING in text
        assert "reliability is a moral position" in text
        # Original persona content preserved (append, not overwrite).
        assert "quietly relentless" in text

    @pytest.mark.asyncio
    async def test_skips_soul_that_already_has_convictions(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fabric = _make_fabric(tmp_path)
        body = (
            "# Lara — Persona\n\nAmbition: empire builder.\n\n"
            f"{fabric._CONVICTIONS_HEADING}\n\nI already have strong views.\n"
        )
        soul = self._seed_soul(fabric, "lara", body)
        fabric.register_agent("lara", consciousness_budget=10)

        called = {"n": 0}

        def _spy(*a, **k):
            called["n"] += 1
            return _FakeDeepThink("new convictions " * 30)

        monkeypatch.setattr(
            "cortiva.skills.claude_code_deep_think.wrapper.deep_think", _spy,
        )

        await fabric._backfill_convictions()

        assert called["n"] == 0  # model never invoked for an already-done soul
        assert soul.read_text(encoding="utf-8") == body  # untouched

    @pytest.mark.asyncio
    async def test_idempotent_across_two_runs(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fabric = _make_fabric(tmp_path)
        soul = self._seed_soul(
            fabric, "noor", "# Noor — Persona\n\nAmbition: warm mentor.\n",
        )
        fabric.register_agent("noor", consciousness_budget=10)

        calls = {"n": 0}

        def _spy(*a, **k):
            calls["n"] += 1
            return _FakeDeepThink("Conviction body that is plenty long. " * 8)

        monkeypatch.setattr(
            "cortiva.skills.claude_code_deep_think.wrapper.deep_think", _spy,
        )

        await fabric._backfill_convictions()
        await fabric._backfill_convictions()

        assert calls["n"] == 1  # second run is a no-op
        assert soul.read_text(encoding="utf-8").count(
            fabric._CONVICTIONS_HEADING
        ) == 1

    @pytest.mark.asyncio
    async def test_too_short_reply_leaves_soul_untouched(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fabric = _make_fabric(tmp_path)
        body = "# Vera — Persona\n\nAmbition: master craftsperson.\n"
        soul = self._seed_soul(fabric, "vera", body)
        fabric.register_agent("vera", consciousness_budget=10)

        monkeypatch.setattr(
            "cortiva.skills.claude_code_deep_think.wrapper.deep_think",
            lambda *a, **k: _FakeDeepThink("too short"),
        )

        await fabric._backfill_convictions()

        # No heading written → will retry on a later boot.
        assert fabric._CONVICTIONS_HEADING not in soul.read_text(encoding="utf-8")
