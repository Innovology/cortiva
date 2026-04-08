"""
Fabric plugin system — extend the core without forking.

Third-party code (Cortiva HQ, community extensions) can hook into
the Fabric's lifecycle at well-defined extension points without
modifying core code.

Extension points:

- **on_wake**: Called after an agent wakes, before planning.
- **on_plan**: Called after a plan is generated, before execution.
- **on_cycle**: Called at the start of each cycle, before task selection.
- **on_task_complete**: Called when a task completes.
- **on_task_fail**: Called when a task fails.
- **on_sleep**: Called after an agent sleeps.
- **on_heartbeat**: Called at the start of each heartbeat tick.
- **on_hook**: Called when an inbound hook is received.
- **context_provider**: Injects additional context into LLM prompts.
- **ipc_handler**: Registers custom IPC commands.

Usage::

    from cortiva.core.plugins import FabricPlugin

    class MetricsPlugin(FabricPlugin):
        name = "metrics"

        async def on_task_complete(self, agent_id, task, outcome):
            send_to_datadog(agent_id, task.description, outcome)

        def context_provider(self, agent_id):
            return "## Custom Context\\nfrom metrics plugin"

    # Register in cortiva.yaml:
    # plugins:
    #   - cortiva_hq.metrics.MetricsPlugin

    # Or programmatically:
    fabric.plugin_manager.register(MetricsPlugin())
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger("cortiva.plugins")


class FabricPlugin:
    """Base class for Fabric plugins.

    Override any method to hook into that extension point.
    All methods are optional — unimplemented hooks are no-ops.
    """

    name: str = ""
    """Unique plugin name.  Used for logging and deduplication."""

    # ----- Lifecycle hooks (async) -----

    async def on_wake(self, agent_id: str, agent: Any) -> None:
        """Called after an agent wakes, before planning begins."""

    async def on_plan(self, agent_id: str, plan_text: str) -> str | None:
        """Called after a plan is generated.

        Return a modified plan_text to override, or None to keep as-is.
        """
        return None

    async def on_cycle(self, agent_id: str) -> None:
        """Called at the start of each cycle, before task selection."""

    async def on_task_complete(
        self, agent_id: str, task: Any, outcome: str,
    ) -> None:
        """Called when a task completes successfully."""

    async def on_task_fail(
        self, agent_id: str, task: Any, error: str,
    ) -> None:
        """Called when a task fails or is deferred."""

    async def on_sleep(self, agent_id: str) -> None:
        """Called after an agent sleeps."""

    async def on_heartbeat(self) -> None:
        """Called at the start of each heartbeat tick."""

    async def on_hook(self, source: str, event_type: str, payload: dict) -> None:
        """Called when an inbound hook is received."""

    # ----- Context injection (sync) -----

    def context_provider(self, agent_id: str) -> str:
        """Return additional context to inject into LLM prompts.

        Called during planning and execution context assembly.
        Return empty string for no additional context.
        """
        return ""

    # ----- IPC extension -----

    def ipc_handlers(self) -> dict[str, Callable[..., Awaitable[dict[str, Any]]]]:
        """Return a dict of custom IPC command handlers.

        Keys are command names (e.g., ``"myPlugin.status"``).
        Values are async handler functions.
        """
        return {}


class PluginManager:
    """Manages registered plugins and dispatches hooks.

    The Fabric creates one PluginManager and calls its dispatch
    methods at each extension point.
    """

    def __init__(self) -> None:
        self._plugins: list[FabricPlugin] = []

    def register(self, plugin: FabricPlugin) -> None:
        """Register a plugin instance."""
        if any(p.name == plugin.name for p in self._plugins):
            logger.warning("Plugin %r already registered, skipping", plugin.name)
            return
        self._plugins.append(plugin)
        logger.info("Plugin registered: %s", plugin.name)

    def unregister(self, name: str) -> bool:
        """Unregister a plugin by name.  Returns True if found."""
        for i, p in enumerate(self._plugins):
            if p.name == name:
                self._plugins.pop(i)
                logger.info("Plugin unregistered: %s", name)
                return True
        return False

    @property
    def plugins(self) -> list[FabricPlugin]:
        return list(self._plugins)

    @property
    def plugin_names(self) -> list[str]:
        return [p.name for p in self._plugins]

    # ----- Dispatch methods -----

    async def dispatch_wake(self, agent_id: str, agent: Any) -> None:
        for p in self._plugins:
            try:
                await p.on_wake(agent_id, agent)
            except Exception as exc:
                logger.error("Plugin %s on_wake error: %s", p.name, exc)

    async def dispatch_plan(self, agent_id: str, plan_text: str) -> str:
        """Dispatch on_plan to all plugins.  Returns the (possibly modified) plan."""
        result = plan_text
        for p in self._plugins:
            try:
                modified = await p.on_plan(agent_id, result)
                if modified is not None:
                    result = modified
            except Exception as exc:
                logger.error("Plugin %s on_plan error: %s", p.name, exc)
        return result

    async def dispatch_cycle(self, agent_id: str) -> None:
        for p in self._plugins:
            try:
                await p.on_cycle(agent_id)
            except Exception as exc:
                logger.error("Plugin %s on_cycle error: %s", p.name, exc)

    async def dispatch_task_complete(
        self, agent_id: str, task: Any, outcome: str,
    ) -> None:
        for p in self._plugins:
            try:
                await p.on_task_complete(agent_id, task, outcome)
            except Exception as exc:
                logger.error("Plugin %s on_task_complete error: %s", p.name, exc)

    async def dispatch_task_fail(
        self, agent_id: str, task: Any, error: str,
    ) -> None:
        for p in self._plugins:
            try:
                await p.on_task_fail(agent_id, task, error)
            except Exception as exc:
                logger.error("Plugin %s on_task_fail error: %s", p.name, exc)

    async def dispatch_sleep(self, agent_id: str) -> None:
        for p in self._plugins:
            try:
                await p.on_sleep(agent_id)
            except Exception as exc:
                logger.error("Plugin %s on_sleep error: %s", p.name, exc)

    async def dispatch_heartbeat(self) -> None:
        for p in self._plugins:
            try:
                await p.on_heartbeat()
            except Exception as exc:
                logger.error("Plugin %s on_heartbeat error: %s", p.name, exc)

    async def dispatch_hook(
        self, source: str, event_type: str, payload: dict,
    ) -> None:
        for p in self._plugins:
            try:
                await p.on_hook(source, event_type, payload)
            except Exception as exc:
                logger.error("Plugin %s on_hook error: %s", p.name, exc)

    def collect_context(self, agent_id: str) -> str:
        """Collect additional context from all plugins."""
        parts: list[str] = []
        for p in self._plugins:
            try:
                ctx = p.context_provider(agent_id)
                if ctx:
                    parts.append(ctx)
            except Exception as exc:
                logger.error("Plugin %s context_provider error: %s", p.name, exc)
        return "\n\n---\n\n".join(parts) if parts else ""

    def collect_ipc_handlers(self) -> dict[str, Callable[..., Awaitable[dict[str, Any]]]]:
        """Collect IPC handlers from all plugins."""
        handlers: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {}
        for p in self._plugins:
            try:
                for cmd, handler in p.ipc_handlers().items():
                    handlers[cmd] = handler
            except Exception as exc:
                logger.error("Plugin %s ipc_handlers error: %s", p.name, exc)
        return handlers


def load_plugins_from_config(config: list[str]) -> list[FabricPlugin]:
    """Load plugin classes from dotted paths in config.

    Config format::

        plugins:
          - "cortiva_hq.metrics.MetricsPlugin"
          - "mycompany.custom.AuditPlugin"
    """
    plugins: list[FabricPlugin] = []
    for path in config:
        try:
            module_path, class_name = path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            instance = cls()
            if not isinstance(instance, FabricPlugin):
                logger.error("Plugin %s is not a FabricPlugin subclass", path)
                continue
            plugins.append(instance)
            logger.info("Loaded plugin from config: %s", path)
        except Exception as exc:
            logger.error("Failed to load plugin %s: %s", path, exc)
    return plugins
