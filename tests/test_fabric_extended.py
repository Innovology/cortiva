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
            agent,
            identity,
            "ctx",
            "Make a checklist plan",
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
            agent,
            identity,
            "ctx",
            "Make a checklist plan",
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
            agent,
            identity,
            "ctx",
            "Make a checklist plan",
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
            "__org_shared__",
            limit=5,
            min_importance=6.0,
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
            id="t1",
            description="Deploy to staging",
            status="pending_approval",
            priority=0,
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
        _write_deploy(
            agents_dir,
            "ceo",
            name="Maren",
            department="executive",
            reports_to="human-founder",
            authority_level=5,
        )
        _write_deploy(
            agents_dir,
            "cpo",
            name="Astrid",
            department="product",
            reports_to="ceo",
            authority_level=5,
        )
        _write_deploy(agents_dir, "po", name="Yuki", department="product", reports_to="cpo")
        _write_deploy(agents_dir, "dev", name="Amara", department="engineering", reports_to="po")

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
        _write_deploy(
            agents_dir, "ceo", name="Maren", department="executive", reports_to="human-founder"
        )
        _write_deploy(agents_dir, "cpo", name="Astrid", department="product", reports_to="ceo")
        _write_deploy(agents_dir, "po", name="Yuki", department="product", reports_to="cpo")

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
        _write_deploy(agents_dir, "y", name="Y", department="d", reports_to="human")

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
        _write_deploy(agents_dir, "coo", name="Marcus", department="operations", reports_to="ceo")
        _write_deploy(
            agents_dir, "ceo", name="Maren", department="executive", reports_to="human-founder"
        )
        for i in range(6):
            _write_deploy(
                agents_dir, f"dev-{i}", name=f"Dev{i}", department="engineering", reports_to="coo"
            )
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
        note = tmp_path / "agents" / "coo" / "today" / "schedule_optimization.md"
        assert note.exists()
        assert "Applied:** True" in note.read_text()

    @pytest.mark.asyncio
    async def test_unauthorised_agent_is_ignored(self, tmp_path: Path) -> None:
        fabric = self._org_on_disk(tmp_path)
        dev = fabric.get_agent("dev-0")  # an IC, no scheduling authority

        await fabric._run_schedule_optimization(dev, {"capacity_ceiling": 200})

        # Nothing applied — no persisted schedule, no artifact.
        assert not (tmp_path / "agents" / ".schedules.json").exists()
        assert not (tmp_path / "agents" / "dev-0" / "today" / "schedule_optimization.md").exists()

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
                    tool_calls=[
                        {
                            "name": "optimize_schedule",
                            "arguments": {"capacity_ceiling": 200, "apply": True},
                        }
                    ],
                )

            async def reflect(self, agent_id, context, day_summary):
                return ConsciousResponse(content="", model="m")

        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=_ToolMock(),
        )
        agents_dir = tmp_path / "agents"
        _write_deploy(agents_dir, "coo", name="Marcus", department="operations", reports_to="ceo")
        _write_deploy(
            agents_dir, "ceo", name="Maren", department="executive", reports_to="human-founder"
        )
        for i in range(4):
            _write_deploy(
                agents_dir, f"dev-{i}", name=f"D{i}", department="engineering", reports_to="coo"
            )
        fabric.discover_agents()

        coo = fabric.get_agent("coo")
        coo.task_queue = TaskQueue(
            tasks=[
                Task(
                    id="t1", description="Optimise the workforce rota", status="pending", priority=2
                ),
            ]
        )
        coo.transition(AgentState.WAKING)
        coo.transition(AgentState.PLANNING)
        coo.transition(AgentState.EXECUTING)

        await fabric.cycle("coo")

        assert (tmp_path / "agents" / ".schedules.json").exists(), (
            "tool_call did not apply the rota"
        )
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
        agent.task_queue = TaskQueue(
            tasks=[
                Task(id="t", description="did work", status="done", outcome="ok"),
            ]
        )
        agent.transition(AgentState.WAKING)
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)
        await fabric.sleep(agent.id)

    @pytest.mark.asyncio
    async def test_sleep_writes_timestamped_entry_with_feelings(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("ritual-1", consciousness_budget=50)
        agent.write_today(
            "emotions.json",
            '{"satisfaction":0.6,"frustration":0.1,"curiosity":0.7,"confidence":0.6,"caution":0.1}',
        )
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
        task = Task(
            id="t1",
            description="Send the cadence template to the team",
            status="pending",
            priority=1,
        )
        agent.task_queue = TaskQueue(tasks=[task])
        agent.transition(AgentState.WAKING)
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        await fabric.cycle("d-1")

        # The task reached consciousness rather than being binned: it's either
        # done, or 'acknowledged' (worked on — this one names a deliverable
        # ("send") that the mock didn't actually emit, so done=delivered (#269)
        # holds it at acknowledged). Critically, it is NOT an exception.
        assert task.status in ("done", "acknowledged"), "defer killed the task instead of doing it"
        assert task not in agent.task_queue.exceptions


class TestSleepGapCatchUp:
    def test_in_sleep_gap_detects_overrun(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        fabric = _make_fabric(tmp_path)
        fabric.scheduler.register("g-1", {"wake": "08:00", "sleep": "16:00"})

        def at(h, m=0):
            return datetime(2026, 6, 7, h, m, tzinfo=UTC)

        assert fabric._in_sleep_gap("g-1", at(17)) is True  # past sleep
        assert fabric._in_sleep_gap("g-1", at(12)) is False  # working
        assert fabric._in_sleep_gap("g-1", at(16, 5)) is False  # within grace

    def test_multi_window_gap(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        fabric = _make_fabric(tmp_path)
        fabric.scheduler.register(
            "g-2",
            {"wake": "08:00,12:00", "sleep": "10:00,14:00"},
        )

        def at(h, m=0):
            return datetime(2026, 6, 7, h, m, tzinfo=UTC)

        assert fabric._in_sleep_gap("g-2", at(11)) is True  # gap between windows
        assert fabric._in_sleep_gap("g-2", at(9)) is False  # in window 1
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
        (inbox / "m1.json").write_text(
            json.dumps(
                {
                    "from": "alexander.browne@innovology.io",
                    "subject": "Welcome to the team",
                    "text": "Glad to have you, Maren.",
                }
            ),
            encoding="utf-8",
        )

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
    async def test_backfills_soul_without_convictions(self, tmp_path: Path, monkeypatch) -> None:
        fabric = _make_fabric(tmp_path)
        soul = self._seed_soul(
            fabric,
            "maren",
            "---\nagent_id: maren\n---\n\n# Maren — Persona\n\n"
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
            "cortiva.skills.claude_code_deep_think.wrapper.deep_think",
            _spy,
        )

        await fabric._backfill_convictions()

        assert called["n"] == 0  # model never invoked for an already-done soul
        assert soul.read_text(encoding="utf-8") == body  # untouched

    @pytest.mark.asyncio
    async def test_idempotent_across_two_runs(self, tmp_path: Path, monkeypatch) -> None:
        fabric = _make_fabric(tmp_path)
        soul = self._seed_soul(
            fabric,
            "noor",
            "# Noor — Persona\n\nAmbition: warm mentor.\n",
        )
        fabric.register_agent("noor", consciousness_budget=10)

        calls = {"n": 0}

        def _spy(*a, **k):
            calls["n"] += 1
            return _FakeDeepThink("Conviction body that is plenty long. " * 8)

        monkeypatch.setattr(
            "cortiva.skills.claude_code_deep_think.wrapper.deep_think",
            _spy,
        )

        await fabric._backfill_convictions()
        await fabric._backfill_convictions()

        assert calls["n"] == 1  # second run is a no-op
        assert soul.read_text(encoding="utf-8").count(fabric._CONVICTIONS_HEADING) == 1

    @pytest.mark.asyncio
    async def test_too_short_reply_leaves_soul_untouched(self, tmp_path: Path, monkeypatch) -> None:
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


# ---------------------------------------------------------------------------
# Idle proactive reassess — a cleared queue must look for work, not sit inert
# ---------------------------------------------------------------------------


class _CountingConsciousness:
    """Counts think() calls and returns a configurable plan body."""

    def __init__(self, plan_body: str = "") -> None:
        self.calls = 0
        self.plan_body = plan_body

    async def think(self, agent_id, context, prompt, **kwargs):
        self.calls += 1
        return ConsciousResponse(
            content=self.plan_body,
            tokens_in=10,
            tokens_out=5,
            model="mock",
        )

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(content="", model="mock")


def _fabric_with(tmp_path: Path, cons) -> Fabric:
    return Fabric(
        agents_dir=tmp_path / "agents",
        memory=InMemoryAdapter(),
        consciousness=cons,
    )


def _executing_agent_empty_queue(fabric: Fabric, aid: str):
    from cortiva.core.agent import TaskQueue

    agent = fabric.register_agent(aid, consciousness_budget=100)
    agent.transition(AgentState.WAKING)
    agent.transition(AgentState.PLANNING)
    agent.transition(AgentState.EXECUTING)
    agent.task_queue = TaskQueue(tasks=[])
    return agent


class TestIdleProactiveReassess:
    @pytest.mark.asyncio
    async def test_empty_queue_triggers_proactive_reassess(self, tmp_path):
        cons = _CountingConsciousness(plan_body="")  # finds nothing this time
        fabric = _fabric_with(tmp_path, cons)
        _executing_agent_empty_queue(fabric, "idle-1")

        res = await fabric.cycle("idle-1")
        assert res["action"] == "reassessed_idle"
        assert res["conscious_call"] is True
        assert cons.calls == 1  # it actually thought, not sat inert

    @pytest.mark.asyncio
    async def test_reassess_is_throttled(self, tmp_path):
        cons = _CountingConsciousness(plan_body="")
        fabric = _fabric_with(tmp_path, cons)
        _executing_agent_empty_queue(fabric, "idle-2")

        first = await fabric.cycle("idle-2")
        assert first["action"] == "reassessed_idle"
        # Immediately again — inside the throttle window → quiet idle, no call.
        second = await fabric.cycle("idle-2")
        assert second["action"] == "idle"
        assert second["conscious_call"] is False
        assert cons.calls == 1  # still just the one

    @pytest.mark.asyncio
    async def test_reassess_runs_again_after_interval(self, tmp_path):
        from datetime import UTC, datetime, timedelta

        cons = _CountingConsciousness(plan_body="")
        fabric = _fabric_with(tmp_path, cons)
        agent = _executing_agent_empty_queue(fabric, "idle-3")

        await fabric.cycle("idle-3")
        assert cons.calls == 1
        # Age the throttle stamp past the interval.
        agent._last_idle_reassess = datetime.now(UTC) - timedelta(
            seconds=fabric._IDLE_REASSESS_INTERVAL_S + 1
        )
        await fabric.cycle("idle-3")
        assert cons.calls == 2  # looked for work again

    @pytest.mark.asyncio
    async def test_proactive_reassess_can_pull_in_work(self, tmp_path):
        # When there IS something worth doing, the reassess fills the queue.
        cons = _CountingConsciousness(
            plan_body="- [ ] **[HIGH]** Follow up on the pending PR review\n"
        )
        fabric = _fabric_with(tmp_path, cons)
        agent = _executing_agent_empty_queue(fabric, "idle-4")

        await fabric.cycle("idle-4")
        # The proactive reassess generated a real next task.
        assert agent.task_queue is not None
        assert any("PR review" in t.description for t in agent.task_queue.tasks)


# ---------------------------------------------------------------------------
# Manager-wakes-reports — authority-gated rally / call to arms
# ---------------------------------------------------------------------------


class TestManagerWake:
    @pytest.mark.asyncio
    async def test_manager_wakes_only_direct_reports(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock as _AM  # noqa: N814

        fabric = _make_fabric(tmp_path, channel=_AM())
        agents_dir = tmp_path / "agents"
        _write_deploy(agents_dir, "cpo", name="Astrid", department="product", reports_to="ceo")
        _write_deploy(
            agents_dir, "po", name="Yuki", department="product", reports_to="cpo"
        )  # cpo's report
        _write_deploy(
            agents_dir, "other", name="Z", department="eng", reports_to="ceo"
        )  # NOT cpo's report
        fabric.discover_agents()
        assert set(fabric.org.subordinates_of("cpo")) == {"po"}

        woken: list[str] = []

        async def _spy_wake(aid, **_kw):
            woken.append(aid)
            return fabric.agents[aid]

        fabric.wake = _spy_wake  # type: ignore[assignment]

        cpo = fabric.agents["cpo"]
        task = Task(id="t1", description="rally", status="done")
        suffix = ReflectionSuffix(
            wake={"agents": ["po", "other"], "reason": "all hands — prod is down"}
        )
        await fabric._process_reflection(cpo, task, suffix)

        # Only the actual report was woken; the non-report was ignored.
        assert woken == ["po"]
        # The report received the call-to-arms reason.
        fabric.channel.send.assert_awaited()
        kwargs = fabric.channel.send.await_args.kwargs
        assert kwargs.get("recipient") == "po"
        assert "all hands" in kwargs.get("content", "")

    def test_org_context_advertises_wake_to_managers(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agents_dir = tmp_path / "agents"
        _write_deploy(agents_dir, "cpo", name="Astrid", department="product", reports_to="ceo")
        _write_deploy(agents_dir, "po", name="Yuki", department="product", reports_to="cpo")
        fabric.discover_agents()
        ctx = fabric.org.org_context_for("cpo")
        assert '"wake"' in ctx and "call to arms" in ctx
        # An IC with no reports doesn't get the wake capability.
        ic_ctx = fabric.org.org_context_for("po")
        assert '"wake"' not in ic_ctx


class TestForcedWakeOverride:
    @pytest.mark.asyncio
    async def test_forced_wake_sets_override_window(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("fw-1", consciousness_budget=10)
        await fabric.wake("fw-1", override_minutes=45)
        assert fabric._wake_override_active(agent, datetime.now(UTC)) is True

    @pytest.mark.asyncio
    async def test_normal_wake_has_no_override(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("fw-2", consciousness_budget=10)
        await fabric.wake("fw-2")  # rota wake, no override
        assert fabric._wake_override_active(agent, datetime.now(UTC)) is False

    @pytest.mark.asyncio
    async def test_override_expires(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime, timedelta

        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("fw-3", consciousness_budget=10)
        await fabric.wake("fw-3", override_minutes=45)
        # A moment after the window has passed → no longer active.
        future = datetime.now(UTC) + timedelta(minutes=46)
        assert fabric._wake_override_active(agent, future) is False


# ---------------------------------------------------------------------------
# Directive salience — authority/mission work persists top-of-mind
# ---------------------------------------------------------------------------


class TestDirectiveSalience:
    def _setup(self, tmp_path):
        import json as _json

        fabric = _make_fabric(tmp_path)
        # Email config: a founder contact (authority).
        (fabric.agents_dir).mkdir(parents=True, exist_ok=True)
        (fabric.agents_dir / ".email_meta.json").write_text(
            _json.dumps(
                {
                    "domain": "workforce.innovology.io",
                    "contacts": [{"address": "alex@innovology.io", "scope": "founder"}],
                }
            ),
            encoding="utf-8",
        )
        agent = fabric.register_agent("ceo", consciousness_budget=10)
        inbox = agent.directory / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "m1.json").write_text(
            _json.dumps(
                {
                    "from": "alex@innovology.io",
                    "subject": "Sailcoach update?",
                    "text": "Please send me the sailcoach plan.",
                }
            ),
            encoding="utf-8",
        )
        return fabric, agent

    def test_founder_mail_becomes_persistent_directive(self, tmp_path):
        fabric, agent = self._setup(tmp_path)
        # Reading the inbox records it as a directive (and moves the mail to read/).
        fabric._email_inbox_context(agent)
        # It now surfaces as top-of-mind...
        ctx = fabric._directive_salience_context(agent)
        assert "Top of mind" in ctx
        assert "Sailcoach" in ctx
        assert "outrank" in ctx.lower()
        # ...and PERSISTS on a second cycle even though the inbox is now empty
        # (this is the fix for read-once burial).
        assert fabric._email_inbox_context(agent) == ""  # inbox drained
        ctx2 = fabric._directive_salience_context(agent)
        assert "Sailcoach" in ctx2

    def test_directive_clears_only_when_its_commitment_is_delivered(self, tmp_path):
        import json as _json
        import os
        import time

        fabric, agent = self._setup(tmp_path)
        fabric._email_inbox_context(agent)
        assert fabric._directive_salience_context(agent) != ""

        # A bare reply to the originator must NOT clear it any more — that was
        # the laundering bug (an unrelated reply dismissed the directive).
        sent = agent.directory / "outbox" / "email" / "sent"
        sent.mkdir(parents=True, exist_ok=True)
        f = sent / "reply.json"
        f.write_text(
            _json.dumps({"to": "alex@innovology.io", "subject": "Re: something unrelated"}),
            encoding="utf-8",
        )
        os.utime(f, (time.time() + 5, time.time() + 5))
        assert fabric._directive_salience_context(agent) != ""  # still owed

        # Recording the directive registered a linked commitment; delivering
        # THAT (the work actually done) is what clears the directive.
        directives = _json.loads((agent.directory / "directives.json").read_text())
        cid = directives[0]["commitment_id"]
        assert cid
        comm_path = agent.directory / "commitments.json"
        comms = _json.loads(comm_path.read_text())
        assert any(c["id"] == cid for c in comms)
        for c in comms:
            if c["id"] == cid:
                c["status"] = "delivered"
        comm_path.write_text(_json.dumps(comms))
        assert fabric._directive_salience_context(agent) == ""

    def test_non_authority_mail_is_not_a_directive(self, tmp_path):
        import json as _json

        fabric, agent = self._setup(tmp_path)
        (agent.directory / "inbox" / "m2.json").write_text(
            _json.dumps({"from": "random@elsewhere.com", "subject": "newsletter", "text": "hi"}),
            encoding="utf-8",
        )
        fabric._email_inbox_context(agent)
        ctx = fabric._directive_salience_context(agent)
        # the founder one is a directive; the newsletter is not
        assert "Sailcoach" in ctx
        assert "newsletter" not in ctx.lower()


# ---------------------------------------------------------------------------
# Repeated-blocker tripwire (#271)
# ---------------------------------------------------------------------------


class TestBlockerTripwire:
    def test_trips_once_at_threshold_then_stays_quiet(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("blk-01")
        calls: list[tuple[str, str]] = []
        fabric._route_escalation = (  # type: ignore[method-assign]
            lambda ag, desc, esc: calls.append((desc, esc))
        )

        task = Task(
            id="t1",
            description="Run the GitHub sweep",
            status="exception",
            error="gh: command not found",
        )

        # Below threshold: no escalation.
        fabric._check_blocker_tripwire(agent, task)
        fabric._check_blocker_tripwire(agent, task)
        assert calls == []

        # Third hit (default threshold 3) trips exactly once.
        fabric._check_blocker_tripwire(agent, task)
        assert len(calls) == 1
        assert "3 times" in calls[0][1]

        # Further hits of the same signature do NOT re-fire (no spam).
        fabric._check_blocker_tripwire(agent, task)
        fabric._check_blocker_tripwire(agent, task)
        assert len(calls) == 1

    def test_distinct_blockers_counted_separately(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        agent = fabric.register_agent("blk-02")
        calls: list[tuple[str, str]] = []
        fabric._route_escalation = (  # type: ignore[method-assign]
            lambda ag, desc, esc: calls.append((desc, esc))
        )

        a = Task(id="a", description="Task A", status="exception", error="error one")
        b = Task(id="b", description="Task B", status="exception", error="error two")
        for _ in range(2):
            fabric._check_blocker_tripwire(agent, a)
            fabric._check_blocker_tripwire(agent, b)
        # Two hits each — neither has tripped yet.
        assert calls == []
        fabric._check_blocker_tripwire(agent, a)
        assert len(calls) == 1  # only A crossed the threshold

    def test_signature_collapses_noise(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        t1 = Task(id="1", description="x", error="gh: command not found!")
        t2 = Task(id="2", description="x", error="gh command not found")
        assert fabric._blocker_signature(t1) == fabric._blocker_signature(t2)


# ---------------------------------------------------------------------------
# High-stakes deliberation pass (#270)
# ---------------------------------------------------------------------------


class TestDeliberationPass:
    def test_low_stakes_task_skips_deliberation(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        task = Task(id="t", description="Tidy up the meeting notes", priority=0)
        assert fabric._deliberation_context(task) == ""

    def test_critical_priority_triggers_deliberation(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        task = Task(id="t", description="Draft the agenda", priority=2)
        ctx = fabric._deliberation_context(task)
        assert "Deliberate before you act" in ctx
        assert "Reversibility" in ctx

    def test_high_stakes_keyword_triggers_even_at_low_priority(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        task = Task(id="t", description="Delete the stale production database", priority=0)
        ctx = fabric._deliberation_context(task)
        assert "high-stakes task" in ctx


# ---------------------------------------------------------------------------
# Commit attribution — agent name + email, no co-author (#273)
# ---------------------------------------------------------------------------


class TestCommitAttribution:
    def _agent_with_deploy(self, fabric, tmp_path: Path, name: str):
        agent = fabric.register_agent("dev-1")
        (agent.directory).mkdir(parents=True, exist_ok=True)
        (agent.directory / "deploy.yaml").write_text(
            f"agent:\n  name: {name}\n",
            encoding="utf-8",
        )
        return agent

    def test_identity_uses_name_and_workforce_email(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        fabric._email_meta = lambda: {"domain": "workforce.example.io"}  # type: ignore
        agent = self._agent_with_deploy(fabric, tmp_path, "Maren Holt")
        name, email = fabric._agent_git_identity(agent)
        assert name == "Maren Holt"
        assert email == "maren@workforce.example.io"

    def test_identity_falls_back_without_domain(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        fabric._email_meta = lambda: {}  # type: ignore
        agent = self._agent_with_deploy(fabric, tmp_path, "Vera Lin")
        name, email = fabric._agent_git_identity(agent)
        assert name == "Vera Lin"
        assert email.startswith("vera@")

    def test_attribution_sets_hook_and_claude_note(self, tmp_path: Path) -> None:
        fabric = _make_fabric(tmp_path)
        fabric._email_meta = lambda: {"domain": "x.io"}  # type: ignore
        agent = self._agent_with_deploy(fabric, tmp_path, "Sam Doe")
        cwd = agent.directory / "workspace"
        cwd.mkdir(parents=True, exist_ok=True)

        env = fabric._ensure_git_attribution(agent, cwd)

        hook = cwd / ".githooks" / "commit-msg"
        assert hook.exists()
        assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
        assert env["GIT_CONFIG_VALUE_0"] == str(cwd / ".githooks")
        claude_md = (cwd / "CLAUDE.md").read_text(encoding="utf-8")
        assert "Co-Authored-By" in claude_md
        # Idempotent: a second call doesn't duplicate the note.
        fabric._ensure_git_attribution(agent, cwd)
        assert (cwd / "CLAUDE.md").read_text(encoding="utf-8").count("Commit attribution") == 1

    def test_hook_strips_coauthor_trailer(self, tmp_path: Path) -> None:
        """The installed commit-msg hook actually removes a co-author line."""
        import subprocess

        fabric = _make_fabric(tmp_path)
        fabric._email_meta = lambda: {"domain": "x.io"}  # type: ignore
        agent = self._agent_with_deploy(fabric, tmp_path, "Sam Doe")
        cwd = agent.directory / "workspace"
        cwd.mkdir(parents=True, exist_ok=True)
        fabric._ensure_git_attribution(agent, cwd)

        msg = cwd / "MSG"
        msg.write_text(
            "Fix the thing\n\nBody line\n\n"
            "Co-Authored-By: Claude <noreply@anthropic.com>\n"
            "🤖 Generated with Claude Code\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["sh", str(cwd / ".githooks" / "commit-msg"), str(msg)],
            check=True,
        )
        out = msg.read_text(encoding="utf-8")
        assert "Fix the thing" in out
        assert "Co-Authored-By" not in out
        assert "Generated with" not in out


# ---------------------------------------------------------------------------
# Procedure reconciliation against tested reality (#282)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Escalation reality veto (#282) — phantom blockers never reach a human
# ---------------------------------------------------------------------------


class TestEscalationRealityVeto:
    def _fab(self, tmp_path: Path, caps: dict):
        import json

        fabric = _make_fabric(tmp_path)
        (fabric.agents_dir / ".capability_status.json").write_text(
            json.dumps({"capabilities": caps}),
            encoding="utf-8",
        )
        return fabric

    def test_vetoes_channel_down_when_email_live(self, tmp_path: Path) -> None:
        fabric = self._fab(tmp_path, {"email": {"status": "live"}})
        esc = (
            "Outbound email channel to founder is currently unavailable per "
            "operator notice. Holding until adapter is configured."
        )
        assert fabric._escalation_contradicts_reality(esc) is True

    def test_allows_genuine_blocker(self, tmp_path: Path) -> None:
        fabric = self._fab(tmp_path, {"email": {"status": "live"}})
        esc = "I need your decision on the SailCoach pricing before I can launch."
        assert fabric._escalation_contradicts_reality(esc) is False

    def test_allows_when_capability_actually_down(self, tmp_path: Path) -> None:
        # If email really is down, the escalation is legitimate — don't veto.
        fabric = self._fab(tmp_path, {"email": {"status": "down"}})
        esc = "Outbound email channel is unavailable; can't reach the founder."
        assert fabric._escalation_contradicts_reality(esc) is False

    def test_route_escalation_suppresses_phantom(self, tmp_path: Path) -> None:
        fabric = self._fab(tmp_path, {"email": {"status": "live"}})
        agent = fabric.register_agent("esc-1")
        sent = []
        fabric._queue_outbound_email = lambda ag, spec: sent.append(spec)  # type: ignore
        fabric._route_escalation(
            agent,
            "Reply to founder",
            "Outbound email channel unavailable until the adapter is configured.",
        )
        assert sent == []  # phantom escalation never emailed


# ---------------------------------------------------------------------------
# refocus_agent — the org's executable re-task lever (authority-gated)
# ---------------------------------------------------------------------------


class TestRefocusAgent:
    def _fab(self, tmp_path: Path):
        fabric = _make_fabric(tmp_path)
        ad = tmp_path / "agents"
        _write_deploy(ad, "ceo", name="Maren", department="exec", reports_to="")
        _write_deploy(ad, "ar-lead", name="Noor", department="ar", reports_to="ceo")
        _write_deploy(ad, "eng", name="Jess", department="product", reports_to="ceo")
        fabric.discover_agents()
        return fabric

    def test_ar_refocus_lands_owed_commitment(self, tmp_path: Path) -> None:
        import json

        fabric = self._fab(tmp_path)
        ar = fabric.agents["ar-lead"]
        fabric._handle_refocus_agent(
            ar,
            {"agent_id": "eng", "focus": "SailCoach", "reason": "founder priority"},
        )
        d = json.loads((fabric.agents_dir / "eng" / "directives.json").read_text())
        assert any("SailCoach" in x["subject"] for x in d)
        cid = d[0]["commitment_id"]
        assert cid
        comms = json.loads((fabric.agents_dir / "eng" / "commitments.json").read_text())
        assert any(c["id"] == cid for c in comms)

    def test_manager_can_refocus_report(self, tmp_path: Path) -> None:
        import json

        fabric = self._fab(tmp_path)
        ceo = fabric.agents["ceo"]
        fabric._handle_refocus_agent(ceo, {"agent_id": "eng", "focus": "SailCoach"})
        d = json.loads((fabric.agents_dir / "eng" / "directives.json").read_text())
        assert any("SailCoach" in x["subject"] for x in d)

    def test_unauthorised_refocus_rejected(self, tmp_path: Path) -> None:
        fabric = self._fab(tmp_path)
        eng = fabric.agents["eng"]  # product agent: not AR, doesn't manage ar-lead
        fabric._handle_refocus_agent(eng, {"agent_id": "ar-lead", "focus": "X"})
        assert not (fabric.agents_dir / "ar-lead" / "directives.json").exists()

    def test_refocus_writes_structural_relay(self, tmp_path: Path) -> None:
        import json

        fabric = self._fab(tmp_path)
        ar = fabric.agents["ar-lead"]
        fabric._handle_refocus_agent(
            ar,
            {"agent_id": "eng", "focus": "SailCoach", "products": ["sailcoach"]},
        )
        relays = list((fabric.agents_dir / "ar-lead" / "outbox" / "refocus").glob("*.json"))
        assert len(relays) == 1
        spec = json.loads(relays[0].read_text())
        assert spec["agent_id"] == "eng"
        assert spec["products"] == ["sailcoach"]

    def test_cross_node_refocus_relays_without_local_directive(self, tmp_path: Path) -> None:
        # Target isn't on this node — no local directive possible, but the
        # structural relay to HQ still fires (HQ re-pins it wherever it runs).
        fabric = self._fab(tmp_path)
        ar = fabric.agents["ar-lead"]
        fabric._handle_refocus_agent(
            ar,
            {"agent_id": "remote-agent", "focus": "SailCoach", "products": ["sailcoach"]},
        )
        assert not (fabric.agents_dir / "remote-agent").is_dir()
        relays = list((fabric.agents_dir / "ar-lead" / "outbox" / "refocus").glob("*.json"))
        assert len(relays) == 1
