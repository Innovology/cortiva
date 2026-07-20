"""Trace context propagation tests — RB-001 §5.5 (CI enforcement).

These tests assert the invariants that the check-observability.sh G2/G3 gates
rely on. A passing run means:
  1. FabricEvent carries trace_id and span_id in its schema.
  2. trace_id propagates correctly through emit_simple() and EventBus chains.
  3. Incident-correlated events share a trace_id.
  4. Serialisation round-trips (to_dict, from_dict, to_json) preserve trace ctx.
  5. Independent events each get a unique span_id.

Gate: runs under existing ci.yml → pytest job; no extra workflow scope needed.
Ref: RB-001 §5.5, chaos exercise finding F2 (resolved 2026-07-08).
"""

from __future__ import annotations

import json
import uuid

import pytest

from cortiva.core.events import EventBus, FabricEvent


class TestFabricEventTraceSchema:
    """FabricEvent must carry trace_id and span_id fields in its schema."""

    def test_span_id_auto_generated(self) -> None:
        event = FabricEvent(event_type="test.event")
        assert event.span_id, "span_id must be auto-generated"
        assert len(event.span_id) == 16

    def test_span_id_unique_per_event(self) -> None:
        e1 = FabricEvent(event_type="test.event")
        e2 = FabricEvent(event_type="test.event")
        assert e1.span_id != e2.span_id, "each event must have a unique span_id"

    def test_trace_id_defaults_to_none(self) -> None:
        event = FabricEvent(event_type="independent.event")
        assert event.trace_id is None, "trace_id must be None for independent events"

    def test_trace_id_accepted_when_provided(self) -> None:
        tid = uuid.uuid4().hex
        event = FabricEvent(event_type="test.event", trace_id=tid)
        assert event.trace_id == tid

    def test_to_dict_includes_trace_context(self) -> None:
        tid = uuid.uuid4().hex
        event = FabricEvent(event_type="test.event", trace_id=tid)
        d = event.to_dict()
        assert "trace_id" in d, "to_dict() must include trace_id"
        assert "span_id" in d, "to_dict() must include span_id"
        assert d["trace_id"] == tid
        assert d["span_id"] == event.span_id

    def test_to_json_includes_trace_context(self) -> None:
        tid = uuid.uuid4().hex
        event = FabricEvent(event_type="test.event", trace_id=tid)
        parsed = json.loads(event.to_json())
        assert parsed["trace_id"] == tid
        assert parsed["span_id"] == event.span_id

    def test_from_dict_preserves_trace_context(self) -> None:
        tid = uuid.uuid4().hex
        sid = uuid.uuid4().hex[:16]
        data = {
            "event_type": "task.completed",
            "agent_id": "a1",
            "trace_id": tid,
            "span_id": sid,
        }
        event = FabricEvent.from_dict(data)
        assert event.trace_id == tid
        assert event.span_id == sid

    def test_from_dict_without_trace_context_does_not_crash(self) -> None:
        data = {"event_type": "legacy.event", "agent_id": "a1"}
        event = FabricEvent.from_dict(data)
        assert event.trace_id is None
        assert event.span_id  # auto-generated


class TestTracePropagationViaEmitSimple:
    """trace_id must thread through EventBus.emit_simple() to the FabricEvent."""

    def test_emit_simple_propagates_trace_id(self) -> None:
        bus = EventBus()
        received: list[FabricEvent] = []
        bus.subscribe(received.append)

        tid = uuid.uuid4().hex
        bus.emit_simple("task.started", agent_id="a1", trace_id=tid)

        assert len(received) == 1
        assert received[0].trace_id == tid, (
            "emit_simple must thread trace_id into the emitted FabricEvent"
        )

    def test_emit_simple_without_trace_id_still_has_span_id(self) -> None:
        bus = EventBus()
        received: list[FabricEvent] = []
        bus.subscribe(received.append)

        bus.emit_simple("independent.event")
        assert received[0].span_id, "every event must have a span_id even without trace_id"
        assert received[0].trace_id is None


class TestIncidentTraceChain:
    """All events in one incident must share the same trace_id (RB-001 §5.5).

    This is the invariant that the watcher_trace_context watcher enforces
    post-hoc. This test enforces it at the emission site.
    """

    def test_incident_events_share_trace_id(self) -> None:
        bus = EventBus()
        emitted: list[FabricEvent] = []
        bus.subscribe(emitted.append)

        incident_trace_id = uuid.uuid4().hex

        bus.emit_simple("fault.injected", agent_id="node-1", trace_id=incident_trace_id)
        bus.emit_simple("recovery.started", agent_id="node-1", trace_id=incident_trace_id)
        bus.emit_simple("recovered", agent_id="node-1", trace_id=incident_trace_id)

        trace_ids = {e.trace_id for e in emitted}
        assert trace_ids == {incident_trace_id}, (
            "all events in an incident chain must carry the same trace_id; "
            f"got: {trace_ids}"
        )

    def test_span_ids_are_unique_within_incident(self) -> None:
        bus = EventBus()
        emitted: list[FabricEvent] = []
        bus.subscribe(emitted.append)

        tid = uuid.uuid4().hex
        for event_type in ("fault.injected", "recovery.started", "recovered"):
            bus.emit_simple(event_type, trace_id=tid)

        span_ids = [e.span_id for e in emitted]
        assert len(span_ids) == len(set(span_ids)), (
            "each event in an incident must have a unique span_id "
            f"(got duplicates in {span_ids})"
        )

    def test_independent_events_use_different_trace_ids(self) -> None:
        bus = EventBus()
        emitted: list[FabricEvent] = []
        bus.subscribe(emitted.append)

        tid_1 = uuid.uuid4().hex
        tid_2 = uuid.uuid4().hex

        bus.emit_simple("incident_a.fault", trace_id=tid_1)
        bus.emit_simple("incident_b.fault", trace_id=tid_2)

        assert emitted[0].trace_id == tid_1
        assert emitted[1].trace_id == tid_2
        assert emitted[0].trace_id != emitted[1].trace_id


class TestTraceContextRoundTrip:
    """Serialisation round-trips must not lose trace context."""

    def test_jsonl_log_round_trip_preserves_trace(self, tmp_path: pytest.TempPathFactory) -> None:
        log_file = tmp_path / "events.jsonl"
        bus = EventBus(log_path=log_file)

        tid = uuid.uuid4().hex
        bus.emit_simple("task.started", agent_id="a1", trace_id=tid)
        bus.emit_simple("task.completed", agent_id="a1", trace_id=tid)

        loaded = bus.load_log()
        assert all(e.trace_id == tid for e in loaded), (
            "trace_id must survive the jsonl write/load round-trip"
        )
        span_ids = [e.span_id for e in loaded]
        assert len(set(span_ids)) == len(span_ids), "span_ids must be unique after reload"

    def test_w3c_traceparent_format(self) -> None:
        trace_id = uuid.uuid4().hex
        event = FabricEvent(event_type="outbound.call", trace_id=trace_id)
        span_id = event.span_id

        traceparent = f"00-{trace_id}-{span_id}-01"
        assert len(traceparent) == 55, (
            "W3C traceparent must be 55 chars: version(2)-trace_id(32)-span_id(16)-flags(2) "
            f"+ 3 dashes; got {len(traceparent)}"
        )
        parts = traceparent.split("-")
        assert parts[1] == trace_id
        assert parts[2] == span_id
