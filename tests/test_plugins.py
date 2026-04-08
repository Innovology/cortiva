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
