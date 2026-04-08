"""Tests for reactive triggers."""

from __future__ import annotations

from cortiva.core.reactive import (
    FiredTrigger,
    ReactiveEngine,
    ReactiveTrigger,
    TriggerAction,
    TriggerCondition,
)


class TestReactiveEngine:
    def test_load_from_config(self) -> None:
        engine = ReactiveEngine()
        engine.load([
            {
                "name": "incident",
                "agent": "dev-cortiva",
                "condition": {"type": "hook", "source": "pagerduty", "events": ["incident.trigger"]},
                "action": {"type": "inject_task", "description": "Handle incident", "priority": "critical"},
            },
        ])
        assert len(engine.triggers) == 1
        assert engine.triggers[0].name == "incident"

    def test_check_hook_match(self) -> None:
        engine = ReactiveEngine()
        engine.load([{
            "name": "pd-alert",
            "agent": "dev",
            "condition": {"type": "hook", "source": "pagerduty", "events": ["incident.trigger"]},
            "action": {"type": "inject_task", "description": "Respond to incident"},
        }])
        fired = engine.check_hook("pagerduty", "incident.trigger", ["dev", "pm"])
        assert len(fired) == 1
        assert fired[0].trigger_name == "pd-alert"
        assert fired[0].agent_id == "dev"

    def test_check_hook_no_match(self) -> None:
        engine = ReactiveEngine()
        engine.load([{
            "name": "pd-alert",
            "agent": "dev",
            "condition": {"type": "hook", "source": "pagerduty", "events": ["incident.trigger"]},
            "action": {"type": "inject_task"},
        }])
        fired = engine.check_hook("github", "push", ["dev"])
        assert fired == []

    def test_check_hook_wildcard_agent(self) -> None:
        engine = ReactiveEngine()
        engine.load([{
            "name": "all-alert",
            "agent": "*",
            "condition": {"type": "hook", "source": "pagerduty"},
            "action": {"type": "inject_task"},
        }])
        fired = engine.check_hook("pagerduty", "incident", ["dev", "qa", "pm"])
        assert len(fired) == 3

    def test_check_budget_threshold(self) -> None:
        engine = ReactiveEngine()
        engine.load([{
            "name": "budget-warn",
            "agent": "dev",
            "condition": {"type": "budget_threshold", "threshold": 0.9},
            "action": {"type": "replan", "reason": "Budget nearly exhausted"},
        }])
        # Below threshold
        fired = engine.check_budget("dev", 0.5)
        assert fired == []

        # Above threshold
        fired = engine.check_budget("dev", 0.95)
        assert len(fired) == 1
        assert fired[0].action.type == "replan"

    def test_check_message(self) -> None:
        engine = ReactiveEngine()
        engine.load([{
            "name": "urgent-msg",
            "agent": "dev",
            "condition": {"type": "message", "contains": "urgent"},
            "action": {"type": "inject_task", "description": "Handle urgent request"},
        }])
        fired = engine.check_message("dev", "This is URGENT: server down")
        assert len(fired) == 1

        fired = engine.check_message("dev", "Regular update")
        assert fired == []

    def test_max_fires(self) -> None:
        engine = ReactiveEngine()
        engine.load([{
            "name": "once",
            "agent": "dev",
            "condition": {"type": "hook", "source": "test"},
            "action": {"type": "notify"},
            "max_fires": 1,
        }])
        fired1 = engine.check_hook("test", "event", ["dev"])
        assert len(fired1) == 1

        fired2 = engine.check_hook("test", "event", ["dev"])
        assert fired2 == []  # already fired max times

    def test_disabled_trigger(self) -> None:
        engine = ReactiveEngine()
        engine.load([{
            "name": "disabled",
            "agent": "dev",
            "condition": {"type": "hook", "source": "test"},
            "action": {"type": "notify"},
            "enabled": False,
        }])
        fired = engine.check_hook("test", "event", ["dev"])
        assert fired == []

    def test_add_trigger_programmatically(self) -> None:
        engine = ReactiveEngine()
        trigger = ReactiveTrigger(
            name="custom",
            agent="dev",
            condition=TriggerCondition(type="hook", source="github"),
            action=TriggerAction(type="inject_task", description="Custom task"),
        )
        engine.add_trigger(trigger)
        assert len(engine.triggers) == 1

        fired = engine.check_hook("github", "push", ["dev"])
        assert len(fired) == 1

    def test_remove_trigger(self) -> None:
        engine = ReactiveEngine()
        engine.load([{
            "name": "removable",
            "agent": "dev",
            "condition": {"type": "hook", "source": "test"},
            "action": {"type": "notify"},
        }])
        assert engine.remove_trigger("removable") is True
        assert engine.remove_trigger("nonexistent") is False
        assert len(engine.triggers) == 0

    def test_wildcard_source(self) -> None:
        engine = ReactiveEngine()
        engine.load([{
            "name": "any-hook",
            "agent": "dev",
            "condition": {"type": "hook"},  # no source = match any
            "action": {"type": "notify"},
        }])
        fired = engine.check_hook("anything", "whatever", ["dev"])
        assert len(fired) == 1

    def test_budget_wildcard_agent(self) -> None:
        engine = ReactiveEngine()
        engine.load([{
            "name": "all-budget",
            "agent": "*",
            "condition": {"type": "budget_threshold", "threshold": 0.8},
            "action": {"type": "replan"},
        }])
        fired = engine.check_budget("any-agent", 0.85)
        assert len(fired) == 1
