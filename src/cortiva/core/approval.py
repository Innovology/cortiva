"""
Approval workflow — tasks that require human or manager sign-off.

When a policy marks a task as ``REQUIRE_APPROVAL``, the Fabric
submits an :class:`ApprovalRequest` to the queue.  The designated
approver can approve or reject via the CLI (``cortiva approve``).

Approved tasks re-enter the agent's execution queue.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortiva.approval")


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class ApprovalRequest:
    """A task awaiting approval before execution."""

    id: str
    agent_id: str
    approver_id: str
    task_description: str
    policy_rule: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: str = ""
    resolved_at: str | None = None
    resolved_by: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "approver_id": self.approver_id,
            "task_description": self.task_description,
            "policy_rule": self.policy_rule,
            "status": self.status.value,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRequest:
        return cls(
            id=data["id"],
            agent_id=data["agent_id"],
            approver_id=data["approver_id"],
            task_description=data["task_description"],
            policy_rule=data.get("policy_rule", ""),
            status=ApprovalStatus(data.get("status", "pending")),
            created_at=data.get("created_at", ""),
            resolved_at=data.get("resolved_at"),
            resolved_by=data.get("resolved_by"),
            reason=data.get("reason", ""),
        )


class ApprovalQueue:
    """Manages pending approval requests.

    Persists to ``{data_dir}/queue.json``.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._requests: list[ApprovalRequest] = []
        self._load()

    def _path(self) -> Path:
        return self._data_dir / "queue.json"

    def _load(self) -> None:
        path = self._path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._requests = [
                    ApprovalRequest.from_dict(r) for r in data.get("requests", [])
                ]
            except (json.JSONDecodeError, KeyError):
                self._requests = []

    def _persist(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        data = {"requests": [r.to_dict() for r in self._requests]}
        self._path().write_text(json.dumps(data, indent=2), encoding="utf-8")

    def submit(
        self,
        agent_id: str,
        task_description: str,
        policy_rule: str,
        approver_id: str,
    ) -> ApprovalRequest:
        """Submit a task for approval."""
        request = ApprovalRequest(
            id=str(uuid.uuid4())[:8],
            agent_id=agent_id,
            approver_id=approver_id,
            task_description=task_description,
            policy_rule=policy_rule,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._requests.append(request)
        self._persist()
        logger.info(
            "Approval request %s: %s wants to '%s' (approver: %s)",
            request.id, agent_id, task_description[:60], approver_id,
        )
        return request

    def pending_for(self, approver_id: str) -> list[ApprovalRequest]:
        """Get all pending requests for this approver."""
        return [
            r for r in self._requests
            if r.approver_id == approver_id and r.status == ApprovalStatus.PENDING
        ]

    def pending_by_agent(self, agent_id: str) -> list[ApprovalRequest]:
        """Get all pending requests submitted by this agent."""
        return [
            r for r in self._requests
            if r.agent_id == agent_id and r.status == ApprovalStatus.PENDING
        ]

    def get(self, request_id: str) -> ApprovalRequest | None:
        for r in self._requests:
            if r.id == request_id:
                return r
        return None

    def approve(
        self, request_id: str, resolved_by: str,
    ) -> ApprovalRequest | None:
        """Approve a request."""
        request = self.get(request_id)
        if request is None or request.status != ApprovalStatus.PENDING:
            return None
        request.status = ApprovalStatus.APPROVED
        request.resolved_at = datetime.now(UTC).isoformat()
        request.resolved_by = resolved_by
        self._persist()
        logger.info("Approval %s approved by %s", request_id, resolved_by)
        return request

    def reject(
        self, request_id: str, resolved_by: str, reason: str = "",
    ) -> ApprovalRequest | None:
        """Reject a request."""
        request = self.get(request_id)
        if request is None or request.status != ApprovalStatus.PENDING:
            return None
        request.status = ApprovalStatus.REJECTED
        request.resolved_at = datetime.now(UTC).isoformat()
        request.resolved_by = resolved_by
        request.reason = reason
        self._persist()
        logger.info("Approval %s rejected by %s: %s", request_id, resolved_by, reason)
        return request

    def approved_tasks_for(self, agent_id: str) -> list[ApprovalRequest]:
        """Get recently approved tasks for an agent.

        These should be re-injected into the agent's task queue.
        """
        return [
            r for r in self._requests
            if r.agent_id == agent_id and r.status == ApprovalStatus.APPROVED
        ]

    def all_pending(self) -> list[ApprovalRequest]:
        """Get all pending requests across all agents."""
        return [r for r in self._requests if r.status == ApprovalStatus.PENDING]
