"""
Work delegation — structured task assignment between agents.

Managers create :class:`WorkAssignment` records that appear in their
subordinate's planning context.  Subordinates report completion via
the ``complete_assignment`` field in the reflection suffix.

Assignments persist to ``{agents_dir}/.delegation/assignments.json``.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortiva.delegation")


class AssignmentStatus(Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass
class WorkAssignment:
    """A structured work assignment from a manager to a subordinate."""

    id: str
    from_agent: str
    to_agent: str
    description: str
    priority: int = 1
    status: AssignmentStatus = AssignmentStatus.PENDING
    deadline: str | None = None
    created_at: str = ""
    completed_at: str | None = None
    outcome: str = ""
    rejection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "description": self.description,
            "priority": self.priority,
            "status": self.status.value,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "outcome": self.outcome,
            "rejection_reason": self.rejection_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkAssignment:
        return cls(
            id=data["id"],
            from_agent=data["from_agent"],
            to_agent=data["to_agent"],
            description=data["description"],
            priority=data.get("priority", 1),
            status=AssignmentStatus(data.get("status", "pending")),
            deadline=data.get("deadline"),
            created_at=data.get("created_at", ""),
            completed_at=data.get("completed_at"),
            outcome=data.get("outcome", ""),
            rejection_reason=data.get("rejection_reason", ""),
        )


class DelegationManager:
    """Manages work assignments between agents.

    Persists to ``{data_dir}/assignments.json``.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._assignments: list[WorkAssignment] = []
        self._load()

    def _path(self) -> Path:
        return self._data_dir / "assignments.json"

    def _load(self) -> None:
        path = self._path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._assignments = [
                    WorkAssignment.from_dict(a) for a in data.get("assignments", [])
                ]
            except (json.JSONDecodeError, KeyError):
                self._assignments = []

    def _persist(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        data = {"assignments": [a.to_dict() for a in self._assignments]}
        self._path().write_text(json.dumps(data, indent=2), encoding="utf-8")

    def create_assignment(
        self,
        from_agent: str,
        to_agent: str,
        description: str,
        *,
        priority: int = 1,
        deadline: str | None = None,
        org: Any | None = None,
    ) -> WorkAssignment:
        """Create a new work assignment.

        If *org* (an :class:`OrgModel`) is provided, validates that
        *from_agent* has delegation authority over *to_agent*.
        """
        if org is not None and not org.can_delegate_to(from_agent, to_agent):
            raise PermissionError(
                f"{from_agent!r} does not have delegation authority over {to_agent!r}"
            )

        assignment = WorkAssignment(
            id=str(uuid.uuid4())[:8],
            from_agent=from_agent,
            to_agent=to_agent,
            description=description,
            priority=priority,
            deadline=deadline,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._assignments.append(assignment)
        self._persist()
        logger.info(
            "Assignment %s created: %s → %s: %s",
            assignment.id, from_agent, to_agent, description[:60],
        )
        return assignment

    def get_assignments_for(
        self,
        agent_id: str,
        *,
        status: AssignmentStatus | None = None,
    ) -> list[WorkAssignment]:
        """Get all assignments delegated TO this agent."""
        result = [a for a in self._assignments if a.to_agent == agent_id]
        if status is not None:
            result = [a for a in result if a.status == status]
        return result

    def get_assignments_from(self, agent_id: str) -> list[WorkAssignment]:
        """Get all assignments created BY this agent."""
        return [a for a in self._assignments if a.from_agent == agent_id]

    def get_assignment(self, assignment_id: str) -> WorkAssignment | None:
        for a in self._assignments:
            if a.id == assignment_id:
                return a
        return None

    def complete_assignment(
        self, assignment_id: str, outcome: str,
    ) -> WorkAssignment | None:
        """Mark an assignment as completed."""
        assignment = self.get_assignment(assignment_id)
        if assignment is None:
            return None
        assignment.status = AssignmentStatus.COMPLETED
        assignment.completed_at = datetime.now(UTC).isoformat()
        assignment.outcome = outcome
        self._persist()
        logger.info("Assignment %s completed: %s", assignment_id, outcome[:60])
        return assignment

    def reject_assignment(
        self, assignment_id: str, reason: str,
    ) -> WorkAssignment | None:
        """Mark an assignment as rejected."""
        assignment = self.get_assignment(assignment_id)
        if assignment is None:
            return None
        assignment.status = AssignmentStatus.REJECTED
        assignment.rejection_reason = reason
        self._persist()
        logger.info("Assignment %s rejected: %s", assignment_id, reason[:60])
        return assignment

    def cancel_assignment(self, assignment_id: str) -> WorkAssignment | None:
        """Cancel a pending assignment (manager-initiated)."""
        assignment = self.get_assignment(assignment_id)
        if assignment is None:
            return None
        assignment.status = AssignmentStatus.CANCELLED
        self._persist()
        return assignment

    def pending_for_context(self, agent_id: str) -> str:
        """Render pending/in-progress assignments as markdown.

        Used for injection into the agent's planning context.
        """
        pending = self.get_assignments_for(
            agent_id, status=AssignmentStatus.PENDING,
        )
        in_progress = self.get_assignments_for(
            agent_id, status=AssignmentStatus.IN_PROGRESS,
        )
        items = pending + in_progress
        if not items:
            return ""

        lines = ["## Delegated Tasks\n"]
        lines.append(
            "These tasks were assigned to you by your manager. "
            "They take priority over self-planned work.\n"
        )
        for a in sorted(items, key=lambda x: x.priority, reverse=True):
            priority_label = (
                "**[CRITICAL]** " if a.priority >= 2
                else "**[HIGH]** " if a.priority >= 1
                else ""
            )
            deadline_str = f" (deadline: {a.deadline})" if a.deadline else ""
            lines.append(
                f"- [ ] {priority_label}{a.description}{deadline_str} "
                f"[assignment:{a.id}]"
            )

        return "\n".join(lines)
