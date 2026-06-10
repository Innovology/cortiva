"""Tests for the Fabric plugin system."""

from __future__ import annotations

import pytest

from cortiva.core.plugins import FabricPlugin, PluginManager, load_plugins_from_config


class SamplePlugin(FabricPlugin):
    name = "sample"

    def __init__(self) -> None:
        self.wake_calls: list[str] = []
        self.sleep_calls: list[str] = []
        self.heartbeat_count = 0
        self.context_calls: list[str] = []

    async def on_wake(self, agent_id, agent):
        self.wake_calls.append(agent_id)

    async def on_sleep(self, agent_id):
        self.sleep_calls.append(agent_id)

    async def on_heartbeat(self):
        self.heartbeat_count += 1

    def context_provider(self, agent_id):
        self.context_calls.append(agent_id)
        return f"## Plugin Context for {agent_id}"


class PlanModifierPlugin(FabricPlugin):
    name = "plan-modifier"

    async def on_plan(self, agent_id, plan_text):
        return plan_text + "\n- [ ] **[HIGH]** Plugin-injected task"


class TestPluginManager:
    def test_register(self) -> None:
        mgr = PluginManager()
        mgr.register(SamplePlugin())
        assert mgr.plugin_names == ["sample"]

    def test_register_duplicate(self) -> None:
        mgr = PluginManager()
        mgr.register(SamplePlugin())
        mgr.register(SamplePlugin())
        assert len(mgr.plugins) == 1

    def test_unregister(self) -> None:
        mgr = PluginManager()
        mgr.register(SamplePlugin())
        assert mgr.unregister("sample") is True
        assert mgr.plugin_names == []

    def test_unregister_nonexistent(self) -> None:
        mgr = PluginManager()
        assert mgr.unregister("nope") is False

    @pytest.mark.asyncio
    async def test_dispatch_wake(self) -> None:
        mgr = PluginManager()
        plugin = SamplePlugin()
        mgr.register(plugin)
        await mgr.dispatch_wake("agent-1", None)
        assert plugin.wake_calls == ["agent-1"]

    @pytest.mark.asyncio
    async def test_dispatch_sleep(self) -> None:
        mgr = PluginManager()
        plugin = SamplePlugin()
        mgr.register(plugin)
        await mgr.dispatch_sleep("agent-1")
        assert plugin.sleep_calls == ["agent-1"]

    @pytest.mark.asyncio
    async def test_dispatch_heartbeat(self) -> None:
        mgr = PluginManager()
        plugin = SamplePlugin()
        mgr.register(plugin)
        await mgr.dispatch_heartbeat()
        await mgr.dispatch_heartbeat()
        assert plugin.heartbeat_count == 2

    @pytest.mark.asyncio
    async def test_dispatch_plan_modifier(self) -> None:
        mgr = PluginManager()
        mgr.register(PlanModifierPlugin())
        result = await mgr.dispatch_plan("agent-1", "- [ ] Original task")
        assert "Plugin-injected task" in result
        assert "Original task" in result

    @pytest.mark.asyncio
    async def test_dispatch_plan_no_modifier(self) -> None:
        mgr = PluginManager()
        mgr.register(SamplePlugin())  # doesn't modify plans
        result = await mgr.dispatch_plan("agent-1", "original")
        assert result == "original"

    def test_collect_context(self) -> None:
        mgr = PluginManager()
        mgr.register(SamplePlugin())
        ctx = mgr.collect_context("agent-1")
        assert "Plugin Context for agent-1" in ctx

    def test_collect_context_empty(self) -> None:
        mgr = PluginManager()
        assert mgr.collect_context("agent-1") == ""

    @pytest.mark.asyncio
    async def test_dispatch_error_isolation(self) -> None:
        """Plugin errors don't crash the dispatch."""
        class BrokenPlugin(FabricPlugin):
            name = "broken"
            async def on_wake(self, agent_id, agent):
                raise RuntimeError("plugin crash")

        mgr = PluginManager()
        mgr.register(BrokenPlugin())
        mgr.register(SamplePlugin())
        # Should not raise, and SamplePlugin should still run
        await mgr.dispatch_wake("agent-1", None)

    def test_collect_ipc_handlers(self) -> None:
        class IPCPlugin(FabricPlugin):
            name = "ipc"
            def ipc_handlers(self):
                async def handler(**kw):
                    return {"ok": True}
                return {"myPlugin.status": handler}

        mgr = PluginManager()
        mgr.register(IPCPlugin())
        handlers = mgr.collect_ipc_handlers()
        assert "myPlugin.status" in handlers


class TestLoadPluginsFromConfig:
    def test_load_nonexistent(self) -> None:
        plugins = load_plugins_from_config(["nonexistent.module.Plugin"])
        assert plugins == []

    def test_load_valid(self) -> None:
        # Load our SamplePlugin from this test module
        plugins = load_plugins_from_config([
            "tests.test_plugins.SamplePlugin",
        ])
        assert len(plugins) == 1
        assert plugins[0].name == "sample"


# ---------------------------------------------------------------------------
# Completed plugin API — bind, task context, and live fabric dispatches
# ---------------------------------------------------------------------------


class RecordingPlugin(FabricPlugin):
    """Records every hook invocation for fabric-integration assertions."""

    name = "recording"

    def __init__(self) -> None:
        self.bound_fabric = None
        self.cycle_calls: list[str] = []
        self.completes: list[tuple[str, str]] = []
        self.fails: list[tuple[str, str]] = []
        self.task_context_calls: list[tuple[str, str, float]] = []

    def bind(self, fabric):
        self.bound_fabric = fabric

    async def on_cycle(self, agent_id):
        self.cycle_calls.append(agent_id)

    async def on_task_complete(self, agent_id, task, outcome):
        self.completes.append((agent_id, outcome))

    async def on_task_fail(self, agent_id, task, error):
        self.fails.append((agent_id, error))

    def context_provider(self, agent_id):
        return "## Cognitive Plan Context"

    def task_context_provider(self, agent_id, task_description, importance=5.0):
        self.task_context_calls.append((agent_id, task_description, importance))
        return f"## Cognitive Task Context: {task_description[:30]}"


class TestPluginBinding:
    def test_register_binds_fabric(self) -> None:
        fabric_stub = object()
        mgr = PluginManager(fabric=fabric_stub)
        plugin = RecordingPlugin()
        mgr.register(plugin)
        assert plugin.bound_fabric is fabric_stub

    def test_register_without_fabric_does_not_bind(self) -> None:
        mgr = PluginManager()
        plugin = RecordingPlugin()
        mgr.register(plugin)
        assert plugin.bound_fabric is None

    def test_bind_error_does_not_break_registration(self) -> None:
        class ExplodingBind(FabricPlugin):
            name = "exploder"

            def bind(self, fabric):
                raise RuntimeError("boom")

        mgr = PluginManager(fabric=object())
        mgr.register(ExplodingBind())
        assert mgr.plugin_names == ["exploder"]


class TestCollectTaskContext:
    def test_collects_per_task_context(self) -> None:
        mgr = PluginManager()
        plugin = RecordingPlugin()
        mgr.register(plugin)
        ctx = mgr.collect_task_context(
            "agent-1", "process the invoice batch", importance=7.0,
        )
        assert "Cognitive Task Context" in ctx
        assert plugin.task_context_calls == [
            ("agent-1", "process the invoice batch", 7.0),
        ]

    def test_empty_when_no_plugins_contribute(self) -> None:
        mgr = PluginManager()
        mgr.register(SamplePlugin())  # has no task_context_provider override
        assert mgr.collect_task_context("agent-1", "task") == ""

    def test_provider_error_is_isolated(self) -> None:
        class Exploder(FabricPlugin):
            name = "exploder2"

            def task_context_provider(self, agent_id, task_description, importance=5.0):
                raise RuntimeError("boom")

        mgr = PluginManager()
        mgr.register(Exploder())
        plugin = RecordingPlugin()
        mgr.register(plugin)
        ctx = mgr.collect_task_context("agent-1", "task")
        assert "Cognitive Task Context" in ctx


@pytest.mark.asyncio
class TestFabricDispatchIntegration:
    """The fabric must actually CALL the declared dispatch points —
    pre-fix, on_task_complete/on_task_fail/on_cycle/context hooks were
    declared in the plugin API but never invoked anywhere."""

    def _make_fabric(self, tmp_path, think_content="Did the work."):
        from cortiva.adapters.memory.inmemory import InMemoryAdapter
        from cortiva.adapters.protocols import ConsciousResponse
        from cortiva.core.fabric import Fabric

        class StubConsciousness:
            async def think(self, **kw):
                return ConsciousResponse(content=think_content, model="stub")

            async def reflect(self, **kw):
                return ConsciousResponse(content="reflected", model="stub")

        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=StubConsciousness(),
        )
        plugin = RecordingPlugin()
        fabric.plugin_manager.register(plugin)
        return fabric, plugin

    def test_fabric_constructs_manager_with_self(self, tmp_path) -> None:
        fabric, plugin = self._make_fabric(tmp_path)
        assert plugin.bound_fabric is fabric

    async def test_task_complete_dispatched(self, tmp_path) -> None:
        from cortiva.core.agent import AgentState

        fabric, plugin = self._make_fabric(tmp_path)
        agent = fabric.register_agent("agent-01")
        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        from cortiva.core.agent import Task, TaskQueue

        agent.task_queue = TaskQueue(
            tasks=[Task(id="t1", description="write the report", priority=1)],
        )
        await fabric.cycle("agent-01")

        assert plugin.cycle_calls == ["agent-01"]
        assert len(plugin.completes) == 1
        assert plugin.completes[0][0] == "agent-01"
        # Per-task plugin context was consulted during execution, with
        # the task's real importance (5.0 baseline + priority 1).
        assert plugin.task_context_calls
        assert plugin.task_context_calls[0][1] == "write the report"
        assert plugin.task_context_calls[0][2] == 6.0
