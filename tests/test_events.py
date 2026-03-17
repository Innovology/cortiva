"""Tests for the EventBus and FabricEvent system."""

from pathlib import Path

import pytest

from cortiva.core.events import EventBus, EventFilter, EventTypes, FabricEvent


class TestFabricEvent:
    def test_create_event(self) -> None:
        event = FabricEvent(event_type="agent.wake", agent_id="test-01")
        assert event.event_type == "agent.wake"
        assert event.agent_id == "test-01"
        assert event.event_id  # auto-generated

    def test_to_dict(self) -> None:
        event = FabricEvent(event_type="task.completed", agent_id="a1", data={"task": "invoice"})
        d = event.to_dict()
        assert d["event_type"] == "task.completed"
        assert d["data"]["task"] == "invoice"

    def test_to_json_and_back(self) -> None:
        import json
        event = FabricEvent(event_type="agent.sleep", agent_id="a1")
        j = event.to_json()
        restored = FabricEvent.from_dict(json.loads(j))
        assert restored.event_type == event.event_type
        assert restored.agent_id == event.agent_id

    def test_from_dict(self) -> None:
        data = {"event_type": "budget.warning", "agent_id": "x", "data": {"level": 90}}
        event = FabricEvent.from_dict(data)
        assert event.event_type == "budget.warning"
        assert event.data["level"] == 90


class TestEventFilter:
    def test_match_all(self) -> None:
        f = EventFilter()
        event = FabricEvent(event_type="agent.wake", agent_id="a1")
        assert f.matches(event) is True

    def test_match_event_type(self) -> None:
        f = EventFilter(event_types=["agent.wake", "agent.sleep"])
        assert f.matches(FabricEvent(event_type="agent.wake")) is True
        assert f.matches(FabricEvent(event_type="task.completed")) is False

    def test_match_wildcard(self) -> None:
        f = EventFilter(event_types=["agent.*"])
        assert f.matches(FabricEvent(event_type="agent.wake")) is True
        assert f.matches(FabricEvent(event_type="agent.sleep")) is True
        assert f.matches(FabricEvent(event_type="task.completed")) is False

    def test_match_agent_id(self) -> None:
        f = EventFilter(agent_ids=["a1", "a2"])
        assert f.matches(FabricEvent(event_type="x", agent_id="a1")) is True
        assert f.matches(FabricEvent(event_type="x", agent_id="a3")) is False

    def test_match_department(self) -> None:
        f = EventFilter(departments=["accounting"])
        assert f.matches(FabricEvent(event_type="x", department="accounting")) is True
        assert f.matches(FabricEvent(event_type="x", department="engineering")) is False


class TestEventBus:
    def test_emit_and_subscribe(self) -> None:
        bus = EventBus()
        received: list[FabricEvent] = []
        bus.subscribe(received.append)

        bus.emit_simple("agent.wake", agent_id="a1")
        assert len(received) == 1
        assert received[0].event_type == "agent.wake"

    def test_filtered_subscription(self) -> None:
        bus = EventBus()
        received: list[FabricEvent] = []
        bus.subscribe(received.append, filter=EventFilter(event_types=["task.*"]))

        bus.emit_simple("agent.wake", agent_id="a1")
        bus.emit_simple("task.completed", agent_id="a1")

        assert len(received) == 1
        assert received[0].event_type == "task.completed"

    def test_unsubscribe(self) -> None:
        bus = EventBus()
        received: list[FabricEvent] = []
        sub_id = bus.subscribe(received.append)

        bus.emit_simple("agent.wake")
        assert len(received) == 1

        bus.unsubscribe(sub_id)
        bus.emit_simple("agent.sleep")
        assert len(received) == 1  # no new events

    def test_unsubscribe_unknown(self) -> None:
        bus = EventBus()
        assert bus.unsubscribe("nonexistent") is False

    def test_buffer(self) -> None:
        bus = EventBus(buffer_size=5)
        for i in range(10):
            bus.emit_simple(f"event.{i}")

        assert bus.buffer_size == 5
        recent = bus.recent(limit=10)
        assert len(recent) == 5
        assert recent[0].event_type == "event.9"  # newest first

    def test_recent_with_filter(self) -> None:
        bus = EventBus()
        bus.emit_simple("agent.wake", agent_id="a1")
        bus.emit_simple("task.done", agent_id="a1")
        bus.emit_simple("agent.sleep", agent_id="a2")

        filtered = bus.recent(filter=EventFilter(agent_ids=["a1"]))
        assert len(filtered) == 2

    def test_clear_buffer(self) -> None:
        bus = EventBus()
        bus.emit_simple("test")
        bus.clear_buffer()
        assert bus.buffer_size == 0

    def test_persistent_log(self, tmp_path: Path) -> None:
        log_file = tmp_path / "events.jsonl"
        bus = EventBus(log_path=log_file)

        bus.emit_simple("agent.wake", agent_id="a1")
        bus.emit_simple("task.done", agent_id="a1")

        assert log_file.exists()
        loaded = bus.load_log()
        assert len(loaded) == 2
        assert loaded[0].event_type == "task.done"  # newest first

    def test_subscriber_error_does_not_break_bus(self) -> None:
        bus = EventBus()
        received: list[FabricEvent] = []

        def bad_listener(event: FabricEvent) -> None:
            raise RuntimeError("boom")

        bus.subscribe(bad_listener)
        bus.subscribe(received.append)

        bus.emit_simple("test")
        assert len(received) == 1  # second subscriber still got the event

    def test_emit_returns_event(self) -> None:
        bus = EventBus()
        event = bus.emit_simple("test.event", agent_id="a1", detail="info")
        assert event.event_type == "test.event"
        assert event.data["detail"] == "info"


class TestEventTypes:
    def test_constants_exist(self) -> None:
        assert EventTypes.AGENT_WAKE == "agent.wake"
        assert EventTypes.TASK_COMPLETED == "task.completed"
        assert EventTypes.PROMOTION_CONFIRMED == "promotion.confirmed"
        assert EventTypes.SNAPSHOT_CREATED == "snapshot.created"
        assert EventTypes.BUDGET_EXHAUSTED == "budget.exhausted"
