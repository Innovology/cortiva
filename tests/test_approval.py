"""Tests for the approval workflow."""

from __future__ import annotations

from pathlib import Path

from cortiva.core.approval import ApprovalQueue, ApprovalStatus


class TestApprovalQueue:
    def test_submit(self, tmp_path: Path) -> None:
        q = ApprovalQueue(tmp_path / ".approvals")
        req = q.submit("dev-cortiva", "merge to main", "merge*", "pm-cortiva")
        assert req.agent_id == "dev-cortiva"
        assert req.approver_id == "pm-cortiva"
        assert req.status == ApprovalStatus.PENDING

    def test_pending_for(self, tmp_path: Path) -> None:
        q = ApprovalQueue(tmp_path / ".approvals")
        q.submit("dev", "task A", "rule", "pm")
        q.submit("qa", "task B", "rule", "pm")
        q.submit("dev", "task C", "rule", "human")

        pm_pending = q.pending_for("pm")
        assert len(pm_pending) == 2
        human_pending = q.pending_for("human")
        assert len(human_pending) == 1

    def test_pending_by_agent(self, tmp_path: Path) -> None:
        q = ApprovalQueue(tmp_path / ".approvals")
        q.submit("dev", "task A", "rule", "pm")
        q.submit("dev", "task B", "rule", "pm")
        q.submit("qa", "task C", "rule", "pm")

        dev_pending = q.pending_by_agent("dev")
        assert len(dev_pending) == 2

    def test_approve(self, tmp_path: Path) -> None:
        q = ApprovalQueue(tmp_path / ".approvals")
        req = q.submit("dev", "merge to main", "merge*", "pm")
        result = q.approve(req.id, "pm")
        assert result is not None
        assert result.status == ApprovalStatus.APPROVED
        assert result.resolved_by == "pm"
        assert result.resolved_at is not None

    def test_reject(self, tmp_path: Path) -> None:
        q = ApprovalQueue(tmp_path / ".approvals")
        req = q.submit("dev", "deploy to prod", "deploy*", "pm")
        result = q.reject(req.id, "pm", "Not ready")
        assert result is not None
        assert result.status == ApprovalStatus.REJECTED
        assert result.reason == "Not ready"

    def test_approve_nonexistent(self, tmp_path: Path) -> None:
        q = ApprovalQueue(tmp_path / ".approvals")
        assert q.approve("nonexistent", "pm") is None

    def test_double_approve(self, tmp_path: Path) -> None:
        q = ApprovalQueue(tmp_path / ".approvals")
        req = q.submit("dev", "task", "rule", "pm")
        q.approve(req.id, "pm")
        # Can't approve again
        assert q.approve(req.id, "pm") is None

    def test_approved_tasks_for(self, tmp_path: Path) -> None:
        q = ApprovalQueue(tmp_path / ".approvals")
        r1 = q.submit("dev", "task A", "rule", "pm")
        q.submit("dev", "task B", "rule", "pm")
        q.approve(r1.id, "pm")

        approved = q.approved_tasks_for("dev")
        assert len(approved) == 1
        assert approved[0].task_description == "task A"

    def test_persistence(self, tmp_path: Path) -> None:
        data_dir = tmp_path / ".approvals"
        q1 = ApprovalQueue(data_dir)
        q1.submit("dev", "task A", "rule", "pm")
        q1.submit("dev", "task B", "rule", "pm")

        q2 = ApprovalQueue(data_dir)
        assert len(q2.all_pending()) == 2

    def test_all_pending(self, tmp_path: Path) -> None:
        q = ApprovalQueue(tmp_path / ".approvals")
        q.submit("dev", "task A", "rule", "pm")
        r2 = q.submit("qa", "task B", "rule", "human")
        q.approve(r2.id, "human")

        pending = q.all_pending()
        assert len(pending) == 1
        assert pending[0].agent_id == "dev"
