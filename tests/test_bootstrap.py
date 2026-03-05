"""Tests for the bootstrap command and terminal execution integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cortiva.adapters.protocols import AgentResponse, ConsciousResponse, ToolCapabilities


# ---------------------------------------------------------------------------
# cortiva bootstrap CLI command
# ---------------------------------------------------------------------------


class TestBootstrapCommand:
    def test_bootstrap_creates_agents(self, tmp_path) -> None:
        from cortiva.cli.main import cmd_bootstrap

        args = MagicMock()
        args.dir = str(tmp_path)

        cmd_bootstrap(args)

        agents_dir = tmp_path / "agents"
        assert (agents_dir / "dev-cortiva" / "identity" / "identity.md").exists()
        assert (agents_dir / "qa-cortiva" / "identity" / "identity.md").exists()
        assert (agents_dir / "pm-cortiva" / "identity" / "identity.md").exists()
        assert (agents_dir / "pm-cortiva" / "workspace" / "backlog.json").exists()

    def test_bootstrap_creates_config(self, tmp_path) -> None:
        from cortiva.cli.main import cmd_bootstrap

        import yaml

        args = MagicMock()
        args.dir = str(tmp_path)

        cmd_bootstrap(args)

        config_path = tmp_path / "cortiva.yaml"
        assert config_path.exists()

        config = yaml.safe_load(config_path.read_text())
        assert config["fabric"]["name"] == "cortiva-bootstrap"
        assert config["terminal"]["adapter"] == "claude-code"
        assert "dev-cortiva" in config["schedules"]
        assert "qa-cortiva" in config["schedules"]
        assert "pm-cortiva" in config["schedules"]

    def test_bootstrap_skips_existing_agents(self, tmp_path, capsys) -> None:
        from cortiva.cli.main import cmd_bootstrap

        # Pre-create one agent
        (tmp_path / "agents" / "dev-cortiva" / "identity").mkdir(parents=True)
        (tmp_path / "agents" / "dev-cortiva" / "identity" / "identity.md").write_text("existing")

        args = MagicMock()
        args.dir = str(tmp_path)

        cmd_bootstrap(args)

        captured = capsys.readouterr()
        assert "already exists" in captured.out
        # Existing content preserved
        assert (tmp_path / "agents" / "dev-cortiva" / "identity" / "identity.md").read_text() == "existing"

    def test_bootstrap_does_not_overwrite_config(self, tmp_path) -> None:
        from cortiva.cli.main import cmd_bootstrap

        config_path = tmp_path / "cortiva.yaml"
        config_path.write_text("existing: true\n")

        args = MagicMock()
        args.dir = str(tmp_path)

        cmd_bootstrap(args)

        assert config_path.read_text() == "existing: true\n"

    def test_bootstrap_config_has_schedules(self, tmp_path) -> None:
        from cortiva.cli.main import cmd_bootstrap

        import yaml

        args = MagicMock()
        args.dir = str(tmp_path)

        cmd_bootstrap(args)

        config = yaml.safe_load((tmp_path / "cortiva.yaml").read_text())
        dev_sched = config["schedules"]["dev-cortiva"]
        assert "wake" in dev_sched
        assert "sleep" in dev_sched
        pm_sched = config["schedules"]["pm-cortiva"]
        assert "replan" in pm_sched


# ---------------------------------------------------------------------------
# Terminal task detection
# ---------------------------------------------------------------------------


class TestTerminalTaskDetection:
    def _make_fabric(self, tmp_path):
        from cortiva.adapters.memory.inmemory import InMemoryAdapter
        from cortiva.core.fabric import Fabric

        class StubConsciousness:
            async def think(self, **kw):
                return ConsciousResponse(content="done", model="stub")
            async def reflect(self, **kw):
                return ConsciousResponse(content="reflected", model="stub")

        return Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=StubConsciousness(),
        )

    def test_detects_coding_tasks(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        assert fabric._is_terminal_task("Implement the new API endpoint")
        assert fabric._is_terminal_task("Write unit tests for the scheduler")
        assert fabric._is_terminal_task("Fix the broken import in config.py")
        assert fabric._is_terminal_task("Refactor the budget manager")
        assert fabric._is_terminal_task("Run pytest to verify changes")

    def test_rejects_non_coding_tasks(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        assert not fabric._is_terminal_task("Review the project roadmap")
        assert not fabric._is_terminal_task("Prioritise backlog items")
        assert not fabric._is_terminal_task("Send status update to Slack")
        assert not fabric._is_terminal_task("Plan tomorrow's tasks")


# ---------------------------------------------------------------------------
# Terminal execution in the cycle
# ---------------------------------------------------------------------------


class TestTerminalExecution:
    def _make_fabric(self, tmp_path, *, terminal=None):
        from cortiva.adapters.memory.inmemory import InMemoryAdapter
        from cortiva.core.fabric import Fabric

        class StubConsciousness:
            async def think(self, **kw):
                return ConsciousResponse(content="- [ ] Do stuff", model="stub")
            async def reflect(self, **kw):
                return ConsciousResponse(content="Reflected.", model="stub")

        return Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=StubConsciousness(),
            terminal=terminal,
        )

    @pytest.mark.asyncio
    async def test_execute_via_terminal_success(self, tmp_path) -> None:
        terminal = AsyncMock()
        terminal.is_available.return_value = True
        terminal.invoke.return_value = AgentResponse(
            content="Implemented the feature. All tests pass.",
        )

        fabric = self._make_fabric(tmp_path, terminal=terminal)
        agent = fabric.register_agent("dev-01")

        from cortiva.core.agent import Task

        task = Task(id="t1", description="Implement the new endpoint")

        result = await fabric._execute_via_terminal(agent, task)
        assert result is True
        assert task.status == "done"
        assert "Implemented the feature" in task.outcome
        assert agent.tasks_completed_today == 1

    @pytest.mark.asyncio
    async def test_execute_via_terminal_error(self, tmp_path) -> None:
        terminal = AsyncMock()
        terminal.is_available.return_value = True
        terminal.invoke.return_value = AgentResponse(
            content="claude CLI timed out",
            is_error=True,
        )

        fabric = self._make_fabric(tmp_path, terminal=terminal)
        agent = fabric.register_agent("dev-01")

        from cortiva.core.agent import Task, TaskQueue

        agent.task_queue = TaskQueue()
        task = Task(id="t1", description="Implement the new endpoint")

        result = await fabric._execute_via_terminal(agent, task)
        assert result is True
        assert task.status == "exception"
        assert "Terminal error" in task.error
        assert agent.tasks_escalated_today == 1

    @pytest.mark.asyncio
    async def test_execute_via_terminal_unavailable_falls_back(self, tmp_path) -> None:
        terminal = AsyncMock()
        terminal.is_available.return_value = False

        fabric = self._make_fabric(tmp_path, terminal=terminal)
        agent = fabric.register_agent("dev-01")

        from cortiva.core.agent import Task

        task = Task(id="t1", description="Implement the new endpoint")

        result = await fabric._execute_via_terminal(agent, task)
        assert result is None  # Falls back to consciousness
        assert task.status == "pending"

    @pytest.mark.asyncio
    async def test_cycle_uses_terminal_for_coding_tasks(self, tmp_path) -> None:
        terminal = AsyncMock()
        terminal.is_available.return_value = True
        terminal.invoke.return_value = AgentResponse(
            content="Done implementing.",
        )

        fabric = self._make_fabric(tmp_path, terminal=terminal)
        agent = fabric.register_agent("dev-01")

        from cortiva.core.agent import AgentState, Task, TaskQueue

        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)
        agent.task_queue = TaskQueue(tasks=[
            Task(id="t1", description="Implement the new API endpoint"),
        ])

        result = await fabric.cycle("dev-01")
        assert result["action"] == "executed_task"
        terminal.invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_cycle_skips_terminal_for_non_coding_tasks(self, tmp_path) -> None:
        terminal = AsyncMock()
        terminal.is_available.return_value = True

        fabric = self._make_fabric(tmp_path, terminal=terminal)
        agent = fabric.register_agent("dev-01")

        from cortiva.core.agent import AgentState, Task, TaskQueue

        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)
        agent.task_queue = TaskQueue(tasks=[
            Task(id="t1", description="Review the project roadmap"),
        ])

        result = await fabric.cycle("dev-01")
        assert result["action"] == "executed_task"
        terminal.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_terminal_prompt_includes_identity(self, tmp_path) -> None:
        terminal = AsyncMock()
        terminal.is_available.return_value = True
        terminal.invoke.return_value = AgentResponse(content="Done.")

        fabric = self._make_fabric(tmp_path, terminal=terminal)
        agent = fabric.register_agent("dev-01")

        from cortiva.core.agent import Task

        task = Task(id="t1", description="Write tests for scheduler")

        await fabric._execute_via_terminal(agent, task)

        call_args = terminal.invoke.call_args
        prompt = call_args.kwargs.get("prompt") or call_args.args[0]
        assert "dev-01" in prompt
        assert "Write tests for scheduler" in prompt

    @pytest.mark.asyncio
    async def test_terminal_uses_workspace_cwd(self, tmp_path) -> None:
        terminal = AsyncMock()
        terminal.is_available.return_value = True
        terminal.invoke.return_value = AgentResponse(content="Done.")

        fabric = self._make_fabric(tmp_path, terminal=terminal)
        agent = fabric.register_agent("dev-01")

        from cortiva.core.agent import Task

        task = Task(id="t1", description="Implement feature")

        await fabric._execute_via_terminal(agent, task)

        call_args = terminal.invoke.call_args
        cwd = call_args.kwargs.get("cwd") or call_args.args[1]
        assert str(cwd).endswith("workspace")
        assert cwd.exists()

    @pytest.mark.asyncio
    async def test_terminal_stores_memory(self, tmp_path) -> None:
        terminal = AsyncMock()
        terminal.is_available.return_value = True
        terminal.invoke.return_value = AgentResponse(content="Feature implemented.")

        fabric = self._make_fabric(tmp_path, terminal=terminal)
        agent = fabric.register_agent("dev-01")

        from cortiva.core.agent import Task

        task = Task(id="t1", description="Implement the widget")

        await fabric._execute_via_terminal(agent, task)

        memories = await fabric.memory.search("dev-01", "widget")
        assert len(memories) >= 1
        assert "terminal" in memories[0].tags


# ---------------------------------------------------------------------------
# Config integration — bootstrap config builds fabric with terminal + schedules
# ---------------------------------------------------------------------------


class TestBootstrapConfigIntegration:
    def test_bootstrap_config_builds_fabric(self, tmp_path) -> None:
        """Verify that the config generated by bootstrap can build a Fabric."""
        from cortiva.cli.main import cmd_bootstrap

        import yaml

        args = MagicMock()
        args.dir = str(tmp_path)
        cmd_bootstrap(args)

        config_path = tmp_path / "cortiva.yaml"
        config = yaml.safe_load(config_path.read_text())

        from cortiva.core.config import build_fabric

        def _mock_import(registry, name, kind):
            if kind == "memory":
                from cortiva.adapters.memory.inmemory import InMemoryAdapter
                return InMemoryAdapter
            if kind == "terminal":
                from cortiva.adapters.terminal.claude_code import ClaudeCodeAdapter
                return ClaudeCodeAdapter
            class MockCls:
                def __init__(self, **kw): pass
                async def think(self, **kw): pass
                async def reflect(self, **kw): pass
            return MockCls

        with patch("cortiva.core.config._import_adapter", side_effect=_mock_import):
            fabric = build_fabric(config)

        assert fabric.terminal is not None
        assert len(fabric.scheduler.agent_ids) == 3
        assert "dev-cortiva" in fabric.scheduler.agent_ids
