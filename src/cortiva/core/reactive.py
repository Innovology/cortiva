"""
Reactive triggers — interrupt agent execution on events.

Agents normally follow their plan sequentially.  Reactive triggers
allow the Fabric to interrupt an executing agent when a condition
is met: a memory pattern matches, a hook arrives, a metric threshold
is crossed, or a peer sends an urgent message.

The trigger evaluates on every heartbeat (lightweight — no LLM calls).
When it fires, the agent's current cycle is interrupted with a
priority task injection.

Config::

    triggers:
      - name: incident-response
        agent: dev-cortiva
        condition:
          type: hook
          source: pagerduty
          events: [incident.trigger]
        action:
          type: inject_task
          description: "INCIDENT: Respond to PagerDuty alert"
          priority: critical

      - name: budget-warning
        agent: "*"
        condition:
          type: budget_threshold
          threshold: 0.9
        action:
          type: replan
          reason: "Budget 90% exhausted — reprioritise remaining tasks"

      - name: memory-pattern
        agent: dev-cortiva
        condition:
          type: memory
          query: "production error"
          min_matches: 3
        action:
          type: inject_task
          description: "Investigate recurring production errors"
          priority: high
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("cortiva.reactive")


@dataclass
class TriggerCondition:
    """What causes a trigger to fire."""

    type: str
    """``hook``, ``budget_threshold``, ``memory``, ``message``."""

    # Hook conditions
    source: str = ""
    events: list[str] = field(default_factory=list)

    # Budget conditions
    threshold: float = 0.0

    # Memory conditions
    query: str = ""
    min_matches: int = 1

    # Message conditions
    contains: str = ""


@dataclass
class TriggerAction:
    """What happens when a trigger fires."""

    type: str
    """``inject_task``, ``replan``, ``wake``, ``notify``."""

    description: str = ""
    priority: str = "high"
    reason: str = ""
    notify_channel: str = ""
    notify_message: str = ""


@dataclass
class ReactiveTrigger:
    """A rule that monitors conditions and fires actions."""

    name: str
    agent: str
    """Agent ID, or ``*`` for all agents."""

    condition: TriggerCondition
    action: TriggerAction
    enabled: bool = True
    fire_count: int = 0
    max_fires: int = 0
    """0 = unlimited.  Set to 1 for one-shot triggers."""


@dataclass
class FiredTrigger:
    """Record of a trigger that fired."""

    trigger_name: str
    agent_id: str
    action: TriggerAction
    context: dict[str, Any] = field(default_factory=dict)


class ReactiveEngine:
    """Evaluates triggers on each heartbeat and fires matching actions.

    The engine checks lightweight conditions without making LLM calls.
    When a trigger fires, it returns a ``FiredTrigger`` that the
    Fabric uses to inject tasks, force replans, or send notifications.
    """

    def __init__(self) -> None:
        self._triggers: list[ReactiveTrigger] = []

    def load(self, config: list[dict[str, Any]]) -> None:
        """Load triggers from the ``triggers`` config section."""
        self._triggers.clear()
        for entry in config:
            if not isinstance(entry, dict):
                continue
            cond_data = entry.get("condition", {})
            condition = TriggerCondition(
                type=cond_data.get("type", ""),
                source=cond_data.get("source", ""),
                events=cond_data.get("events", []),
                threshold=float(cond_data.get("threshold", 0)),
                query=cond_data.get("query", ""),
                min_matches=int(cond_data.get("min_matches", 1)),
                contains=cond_data.get("contains", ""),
            )
            act_data = entry.get("action", {})
            action = TriggerAction(
                type=act_data.get("type", ""),
                description=act_data.get("description", ""),
                priority=act_data.get("priority", "high"),
                reason=act_data.get("reason", ""),
                notify_channel=act_data.get("notify_channel", ""),
                notify_message=act_data.get("notify_message", ""),
            )
            self._triggers.append(ReactiveTrigger(
                name=entry.get("name", ""),
                agent=entry.get("agent", "*"),
                condition=condition,
                action=action,
                enabled=entry.get("enabled", True),
                max_fires=int(entry.get("max_fires", 0)),
            ))

    def add_trigger(self, trigger: ReactiveTrigger) -> None:
        """Add a trigger programmatically."""
        self._triggers.append(trigger)

    def remove_trigger(self, name: str) -> bool:
        """Remove a trigger by name."""
        for i, t in enumerate(self._triggers):
            if t.name == name:
                self._triggers.pop(i)
                return True
        return False

    @property
    def triggers(self) -> list[ReactiveTrigger]:
        return list(self._triggers)

    def check_hook(
        self, source: str, event_type: str, agent_ids: list[str],
    ) -> list[FiredTrigger]:
        """Check hook-based triggers.  Called when a hook arrives."""
        fired: list[FiredTrigger] = []
        for trigger in self._triggers:
            if not trigger.enabled:
                continue
            if trigger.condition.type != "hook":
                continue
            if trigger.condition.source and trigger.condition.source != source:
                continue
            if trigger.condition.events and event_type not in trigger.condition.events:
                continue
            if self._check_max_fires(trigger):
                continue

            targets = agent_ids if trigger.agent == "*" else [trigger.agent]
            for agent_id in targets:
                if agent_id in agent_ids or trigger.agent == "*":
                    trigger.fire_count += 1
                    fired.append(FiredTrigger(
                        trigger_name=trigger.name,
                        agent_id=agent_id,
                        action=trigger.action,
                        context={"source": source, "event_type": event_type},
                    ))
        return fired

    def check_budget(
        self, agent_id: str, usage_ratio: float,
    ) -> list[FiredTrigger]:
        """Check budget-threshold triggers for an agent."""
        fired: list[FiredTrigger] = []
        for trigger in self._triggers:
            if not trigger.enabled:
                continue
            if trigger.condition.type != "budget_threshold":
                continue
            if trigger.agent != "*" and trigger.agent != agent_id:
                continue
            if usage_ratio < trigger.condition.threshold:
                continue
            if self._check_max_fires(trigger):
                continue

            trigger.fire_count += 1
            fired.append(FiredTrigger(
                trigger_name=trigger.name,
                agent_id=agent_id,
                action=trigger.action,
                context={"usage_ratio": usage_ratio},
            ))
        return fired

    def check_message(
        self, agent_id: str, message_content: str,
    ) -> list[FiredTrigger]:
        """Check message-based triggers (urgent keywords, etc.)."""
        fired: list[FiredTrigger] = []
        for trigger in self._triggers:
            if not trigger.enabled:
                continue
            if trigger.condition.type != "message":
                continue
            if trigger.agent != "*" and trigger.agent != agent_id:
                continue
            if trigger.condition.contains not in message_content.lower():
                continue
            if self._check_max_fires(trigger):
                continue

            trigger.fire_count += 1
            fired.append(FiredTrigger(
                trigger_name=trigger.name,
                agent_id=agent_id,
                action=trigger.action,
                context={"message": message_content[:200]},
            ))
        return fired

    def _check_max_fires(self, trigger: ReactiveTrigger) -> bool:
        """Return True if trigger has exceeded max_fires."""
        if trigger.max_fires > 0 and trigger.fire_count >= trigger.max_fires:
            return True
        return False
