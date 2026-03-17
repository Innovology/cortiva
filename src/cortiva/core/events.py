"""
Cortiva EventBus — structured event emission, filtering, and buffering.

Every significant fabric action emits a FabricEvent. The portal subscribes
to the EventBus and broadcasts events via WebSocket to connected clients.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


@dataclass
class FabricEvent:
    """A structured event emitted by the fabric or its subsystems."""

    event_type: str
    agent_id: str | None = None
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)
    department: str | None = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "data": self.data,
            "department": self.department,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FabricEvent:
        return cls(
            event_type=data["event_type"],
            agent_id=data.get("agent_id"),
            timestamp=data.get("timestamp", time.time()),
            data=data.get("data", {}),
            department=data.get("department"),
            event_id=data.get("event_id", uuid.uuid4().hex[:12]),
        )


@dataclass
class EventFilter:
    """Filter for event subscriptions."""

    event_types: list[str] | None = None
    agent_ids: list[str] | None = None
    departments: list[str] | None = None

    def matches(self, event: FabricEvent) -> bool:
        if self.event_types and event.event_type not in self.event_types:
            # Also match prefix patterns like "agent.*"
            prefix_match = any(
                event.event_type.startswith(t.rstrip("*"))
                for t in self.event_types
                if t.endswith("*")
            )
            if not prefix_match:
                return False
        if self.agent_ids and event.agent_id not in self.agent_ids:
            return False
        if self.departments and event.department not in self.departments:
            return False
        return True


@dataclass
class _Subscription:
    """Internal subscription record."""

    id: str
    callback: Callable[[FabricEvent], None]
    filter: EventFilter | None


class EventBus:
    """Central event bus for the Cortiva fabric.

    Features:
    - Structured FabricEvent emission
    - Subscription with optional filtering
    - In-memory ring buffer for recent event retrieval
    - Optional persistent log (append-only JSON lines file)
    """

    def __init__(
        self,
        buffer_size: int = 1000,
        log_path: Path | None = None,
    ) -> None:
        self._subscriptions: dict[str, _Subscription] = {}
        self._buffer: deque[FabricEvent] = deque(maxlen=buffer_size)
        self._log_path = log_path
        self._lock = threading.Lock()

        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: FabricEvent) -> None:
        """Emit an event to all matching subscribers and buffer it."""
        with self._lock:
            self._buffer.append(event)

        # Persist to log file
        if self._log_path:
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(event.to_json() + "\n")
            except OSError:
                pass

        # Notify subscribers
        for sub in list(self._subscriptions.values()):
            if sub.filter is None or sub.filter.matches(event):
                try:
                    sub.callback(event)
                except Exception:
                    pass  # Don't let subscriber errors break the bus

    def emit_simple(self, event_type: str, agent_id: str | None = None, **data: Any) -> FabricEvent:
        """Convenience method to emit an event from simple arguments."""
        event = FabricEvent(
            event_type=event_type,
            agent_id=agent_id,
            data=data,
        )
        self.emit(event)
        return event

    def subscribe(
        self,
        callback: Callable[[FabricEvent], None],
        filter: EventFilter | None = None,
    ) -> str:
        """Subscribe to events. Returns a subscription ID."""
        sub_id = uuid.uuid4().hex[:12]
        self._subscriptions[sub_id] = _Subscription(
            id=sub_id,
            callback=callback,
            filter=filter,
        )
        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a subscription. Returns True if found."""
        return self._subscriptions.pop(subscription_id, None) is not None

    def recent(
        self,
        limit: int = 100,
        filter: EventFilter | None = None,
    ) -> list[FabricEvent]:
        """Return recent events from the buffer, newest first."""
        with self._lock:
            events = list(self._buffer)
        events.reverse()
        if filter:
            events = [e for e in events if filter.matches(e)]
        return events[:limit]

    def clear_buffer(self) -> None:
        """Clear the in-memory event buffer."""
        with self._lock:
            self._buffer.clear()

    @property
    def buffer_size(self) -> int:
        """Number of events currently in the buffer."""
        return len(self._buffer)

    def load_log(self, limit: int = 1000) -> list[FabricEvent]:
        """Load events from the persistent log file, newest first."""
        if not self._log_path or not self._log_path.exists():
            return []

        events: list[FabricEvent] = []
        with open(self._log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(FabricEvent.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, KeyError):
                        pass

        events.reverse()
        return events[:limit]


# ---------------------------------------------------------------------------
# Standard event type constants
# ---------------------------------------------------------------------------

class EventTypes:
    """Constants for standard event types."""

    # Agent lifecycle
    AGENT_WAKE = "agent.wake"
    AGENT_SLEEP = "agent.sleep"
    AGENT_PLAN = "agent.plan"
    AGENT_REFLECT = "agent.reflect"

    # Task execution
    TASK_STARTED = "task.started"
    TASK_COMPLETED_PROCEDURAL = "task.completed_procedural"
    TASK_COMPLETED_CONSCIOUS = "task.completed_conscious"
    TASK_COMPLETED = "task.completed"
    TASK_EXCEPTION = "task.exception"
    TASK_FAILED = "task.failed"

    # Messaging
    MESSAGE_SENT = "message.sent"
    MESSAGE_RECEIVED = "message.received"

    # Replanning
    REPLAN_TRIGGERED = "replan.triggered"
    REPLAN_COMPLETED = "replan.completed"

    # Procedures
    PROCEDURE_PROMOTED = "procedure.promoted"
    PROCEDURE_RETIRED = "procedure.retired"

    # Budget
    BUDGET_WARNING = "budget.warning"
    BUDGET_EXHAUSTED = "budget.exhausted"
    BUDGET_RECHARGED = "budget.recharged"

    # Promotion
    PROMOTION_INITIATED = "promotion.initiated"
    PROMOTION_ASSESSED = "promotion.assessed"
    PROMOTION_CONFIRMED = "promotion.confirmed"
    PROMOTION_REVERTED = "promotion.reverted"

    # Snapshots
    SNAPSHOT_CREATED = "snapshot.created"
    SNAPSHOT_RESTORED = "snapshot.restored"

    # Cluster
    CLUSTER_NODE_JOINED = "cluster.node_joined"
    CLUSTER_NODE_LEFT = "cluster.node_left"
    CLUSTER_AGENT_MOVED = "cluster.agent_moved"
