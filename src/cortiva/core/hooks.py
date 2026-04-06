"""
Inbound hook system — wake agents on external events.

External systems (GitHub, Slack, Linear, PagerDuty, etc.) can send
webhooks to the Fabric.  The hook router matches the payload to an
agent and wakes it if sleeping, injecting the hook context into the
agent's planning prompt.

Hook routes are configured in ``cortiva.yaml``::

    hooks:
      routes:
        - source: github
          events: [pull_request, push, issues]
          agent: dev-cortiva
          priority: high
        - source: pagerduty
          events: [incident.trigger]
          agent: dev-cortiva
          priority: critical
          wake_if_sleeping: true
        - source: linear
          events: [issue.created, issue.updated]
          agent: pm-cortiva
        - source: "*"
          events: ["*"]
          agent: pm-cortiva
          # catch-all: route unmatched hooks to PM

The Fabric exposes a hook endpoint via IPC (``hook.receive``) and
optionally via the portal API.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortiva.hooks")


@dataclass
class HookEvent:
    """An inbound event from an external system."""

    id: str
    source: str
    """Origin system: ``github``, ``slack``, ``linear``, ``pagerduty``, etc."""

    event_type: str
    """Event name: ``pull_request``, ``incident.trigger``, etc."""

    payload: dict[str, Any]
    """Raw payload from the external system."""

    received_at: str = ""
    priority: str = "normal"
    """``normal``, ``high``, or ``critical``."""

    routed_to: str = ""
    """Agent ID this was routed to."""

    woke_agent: bool = False
    """True if this hook caused a sleeping agent to wake."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "event_type": self.event_type,
            "payload": self.payload,
            "received_at": self.received_at,
            "priority": self.priority,
            "routed_to": self.routed_to,
            "woke_agent": self.woke_agent,
        }

    def summary(self) -> str:
        """One-line summary for context injection."""
        payload_preview = json.dumps(self.payload)[:200]
        return (
            f"[{self.source}/{self.event_type}] "
            f"priority={self.priority} — {payload_preview}"
        )


@dataclass
class HookRoute:
    """A routing rule for matching hooks to agents."""

    source: str
    """Source system to match (or ``*`` for any)."""

    events: list[str]
    """Event types to match (or ``["*"]`` for any)."""

    agent: str
    """Agent ID to route to."""

    priority: str = "normal"
    """Priority to assign to matched hooks."""

    wake_if_sleeping: bool = True
    """Whether to wake the agent if it's sleeping."""

    def matches(self, source: str, event_type: str) -> bool:
        """Check if this route matches the given source and event."""
        if self.source != "*" and self.source != source:
            return False
        if "*" not in self.events and event_type not in self.events:
            return False
        return True


class HookRouter:
    """Routes inbound hooks to agents based on configured rules.

    Maintains a log of recent hooks for debugging and audit.
    """

    def __init__(self) -> None:
        self._routes: list[HookRoute] = []
        self._recent: list[HookEvent] = []
        self._max_recent = 100
        self._pending: dict[str, list[HookEvent]] = {}
        """Per-agent queue of hooks awaiting the next wake cycle."""

    def load(self, config: dict[str, Any]) -> None:
        """Load routing rules from the ``hooks`` config section."""
        self._routes.clear()
        for route_data in config.get("routes", []):
            if not isinstance(route_data, dict):
                continue
            self._routes.append(HookRoute(
                source=route_data.get("source", "*"),
                events=route_data.get("events", ["*"]),
                agent=route_data.get("agent", ""),
                priority=route_data.get("priority", "normal"),
                wake_if_sleeping=route_data.get("wake_if_sleeping", True),
            ))

    def route(self, source: str, event_type: str, payload: dict[str, Any]) -> HookEvent | None:
        """Route an inbound hook to the matching agent.

        Returns the ``HookEvent`` with ``routed_to`` set, or ``None``
        if no route matched.
        """
        for rule in self._routes:
            if rule.matches(source, event_type):
                event = HookEvent(
                    id=str(uuid.uuid4())[:8],
                    source=source,
                    event_type=event_type,
                    payload=payload,
                    received_at=datetime.now(UTC).isoformat(),
                    priority=rule.priority,
                    routed_to=rule.agent,
                )
                self._recent.append(event)
                if len(self._recent) > self._max_recent:
                    self._recent.pop(0)

                # Queue for the agent
                self._pending.setdefault(rule.agent, []).append(event)

                logger.info(
                    "Hook routed: %s/%s → %s (priority=%s)",
                    source, event_type, rule.agent, rule.priority,
                )
                return event

        logger.warning("No route for hook: %s/%s", source, event_type)
        return None

    def should_wake(self, event: HookEvent) -> bool:
        """Check if this hook should wake a sleeping agent."""
        for rule in self._routes:
            if rule.matches(event.source, event.event_type):
                return rule.wake_if_sleeping
        return False

    def pending_for(self, agent_id: str) -> list[HookEvent]:
        """Get and clear pending hooks for an agent."""
        return self._pending.pop(agent_id, [])

    def pending_context(self, agent_id: str) -> str:
        """Render pending hooks as context for the agent's planning prompt.

        Does NOT clear the queue — call ``pending_for()`` to consume.
        """
        hooks = self._pending.get(agent_id, [])
        if not hooks:
            return ""

        lines = [
            f"## Inbound Hooks ({len(hooks)})\n",
            "External events received while you were sleeping. "
            "Address these in your plan.\n",
        ]
        for hook in hooks:
            priority_marker = ""
            if hook.priority == "critical":
                priority_marker = "**[CRITICAL]** "
            elif hook.priority == "high":
                priority_marker = "**[HIGH]** "
            lines.append(f"- {priority_marker}{hook.summary()}")

        return "\n".join(lines)

    def recent_hooks(self, limit: int = 20) -> list[HookEvent]:
        """Get recent hooks for debugging."""
        return list(reversed(self._recent[-limit:]))
