"""Tests for the delegation system."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortiva.core.delegation import AssignmentStatus, DelegationManager, WorkAssignment
from cortiva.core.org import OrgModel


SAMPLE_ORG = OrgModel.from_dict({
    "name": "Test Org",
    "reporting": {
        "dev-cortiva": "pm-cortiva",
        "qa-cortiva": "pm-cortiva",
    },
})


class TestWorkAssignment:
    def test_roundtrip(self) -> None:
        a = WorkAssignment(
            id="abc123",
            from_agent="pm-cortiva",
            to_agent="dev-cortiva",
            description="Implement auth module",
            priority=2,
            created_at="2026-03-30T09:00:00",
        )
        d = a.to_dict()
        restored = WorkAssignment.from_dict(d)
        assert restored.id == "abc123"
        assert restored.from_agent == "pm-cortiva"
        assert restored.priority == 2
        assert restored.status == AssignmentStatus.PENDING


class TestDelegationManager:
    def test_create_assignment(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        a = mgr.create_assignment("pm-cortiva", "dev-cortiva", "Fix the bug")
        assert a.from_agent == "pm-cortiva"
        assert a.to_agent == "dev-cortiva"
        assert a.status == AssignmentStatus.PENDING

    def test_create_with_org_validation(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        # PM can delegate to dev
        a = mgr.create_assignment(
            "pm-cortiva", "dev-cortiva", "Fix the bug", org=SAMPLE_ORG,
        )
        assert a is not None

    def test_create_rejected_without_authority(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        with pytest.raises(PermissionError, match="delegation authority"):
            mgr.create_assignment(
                "dev-cortiva", "pm-cortiva", "Assign me work", org=SAMPLE_ORG,
            )

    def test_get_assignments_for(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        mgr.create_assignment("pm", "dev", "Task A")
        mgr.create_assignment("pm", "dev", "Task B")
        mgr.create_assignment("pm", "qa", "Task C")

        dev_tasks = mgr.get_assignments_for("dev")
        assert len(dev_tasks) == 2
        qa_tasks = mgr.get_assignments_for("qa")
        assert len(qa_tasks) == 1

    def test_get_assignments_for_with_status(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        a1 = mgr.create_assignment("pm", "dev", "Task A")
        mgr.create_assignment("pm", "dev", "Task B")
        mgr.complete_assignment(a1.id, "Done")

        pending = mgr.get_assignments_for("dev", status=AssignmentStatus.PENDING)
        assert len(pending) == 1
        completed = mgr.get_assignments_for("dev", status=AssignmentStatus.COMPLETED)
        assert len(completed) == 1

    def test_complete_assignment(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        a = mgr.create_assignment("pm", "dev", "Fix bug")
        result = mgr.complete_assignment(a.id, "Fixed the race condition")
        assert result is not None
        assert result.status == AssignmentStatus.COMPLETED
        assert result.outcome == "Fixed the race condition"
        assert result.completed_at is not None

    def test_reject_assignment(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        a = mgr.create_assignment("pm", "dev", "Write code in Ruby")
        result = mgr.reject_assignment(a.id, "Not a Ruby project")
        assert result is not None
        assert result.status == AssignmentStatus.REJECTED
        assert result.rejection_reason == "Not a Ruby project"

    def test_cancel_assignment(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        a = mgr.create_assignment("pm", "dev", "Cancelled task")
        result = mgr.cancel_assignment(a.id)
        assert result is not None
        assert result.status == AssignmentStatus.CANCELLED

    def test_persistence(self, tmp_path: Path) -> None:
        data_dir = tmp_path / ".delegation"
        mgr1 = DelegationManager(data_dir)
        mgr1.create_assignment("pm", "dev", "Task A")
        mgr1.create_assignment("pm", "qa", "Task B")

        # Load fresh
        mgr2 = DelegationManager(data_dir)
        assert len(mgr2.get_assignments_for("dev")) == 1
        assert len(mgr2.get_assignments_for("qa")) == 1

    def test_pending_for_context(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        mgr.create_assignment("pm", "dev", "Implement auth", priority=2)
        mgr.create_assignment("pm", "dev", "Write tests", priority=1)

        ctx = mgr.pending_for_context("dev")
        assert "Delegated Tasks" in ctx
        assert "Implement auth" in ctx
        assert "Write tests" in ctx
        assert "CRITICAL" in ctx

    def test_pending_for_context_empty(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        assert mgr.pending_for_context("dev") == ""

    def test_get_nonexistent_assignment(self, tmp_path: Path) -> None:
        mgr = DelegationManager(tmp_path / ".delegation")
        assert mgr.complete_assignment("nonexistent", "done") is None
