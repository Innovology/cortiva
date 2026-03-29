"""Tests for the snapshot and promotion engines."""

from pathlib import Path

import pytest

from cortiva.core.agent import Agent, WORKSPACE_DIRS
from cortiva.core.snapshots import (
    SnapshotManager,
    clone_from_snapshot,
    create_snapshot,
    delete_snapshot,
    enforce_retention,
    export_snapshot,
    get_snapshot,
    import_snapshot,
    list_snapshots,
    restore_snapshot,
)
from cortiva.core.promotion import (
    PromotionAssessment,
    PromotionManager,
    assess_probation,
    auto_resolve_probation,
    confirm_promotion,
    extend_probation,
    get_promotion,
    initiate_promotion,
    is_probationary,
    revert_promotion,
    set_backfill,
)


def _make_agent(tmp_path: Path, agent_id: str = "test-01") -> Path:
    """Create a minimal agent directory with identity files."""
    agent_dir = tmp_path / agent_id
    agent_dir.mkdir()
    for subdir in WORKSPACE_DIRS:
        (agent_dir / subdir).mkdir()

    (agent_dir / "identity" / "identity.md").write_text(
        f"# {agent_id}\n\nI am a test agent.\n"
    )
    (agent_dir / "identity" / "soul.md").write_text(
        f"# {agent_id} — Persona\n\nMethodical and thorough.\n"
    )
    (agent_dir / "identity" / "responsibilities.md").write_text(
        f"# {agent_id} — Responsibilities\n\n## Primary\n\nProcess invoices.\n"
    )
    (agent_dir / "identity" / "procedures.md").write_text(
        f"# {agent_id} — Procedures\n\n## Invoice Processing\n\n1. Read invoice.\n"
    )
    (agent_dir / "journal" / "2026-03-01.md").write_text(
        "# 2026-03-01\n\nFirst day on the job.\n"
    )
    return agent_dir


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------

class TestSnapshots:
    def test_create_and_list(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        meta = create_snapshot(agent_dir, name="initial", description="First snapshot")

        assert meta.agent_id == "test-01"
        assert meta.name == "initial"
        assert meta.trigger == "manual"

        snapshots = list_snapshots(agent_dir)
        assert len(snapshots) == 1
        assert snapshots[0].snapshot_id == meta.snapshot_id

    def test_snapshot_captures_identity(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        meta = create_snapshot(agent_dir)

        snap_path = get_snapshot(agent_dir, meta.snapshot_id)
        assert snap_path is not None
        assert (snap_path / "identity" / "identity.md").exists()
        assert (snap_path / "identity" / "procedures.md").exists()

    def test_snapshot_captures_journal(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        meta = create_snapshot(agent_dir)

        snap_path = get_snapshot(agent_dir, meta.snapshot_id)
        assert snap_path is not None
        assert (snap_path / "journal" / "2026-03-01.md").exists()

    def test_restore_snapshot(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        meta = create_snapshot(agent_dir, name="before-change")

        # Modify identity
        (agent_dir / "identity" / "identity.md").write_text("# Modified\n\nChanged.\n")

        # Restore
        assert restore_snapshot(agent_dir, meta.snapshot_id) is True

        # Verify restored content
        content = (agent_dir / "identity" / "identity.md").read_text()
        assert "I am a test agent" in content

    def test_restore_creates_safety_snapshot(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        meta = create_snapshot(agent_dir)

        restore_snapshot(agent_dir, meta.snapshot_id)

        # Should now have 2 snapshots: original + pre-restore
        snapshots = list_snapshots(agent_dir)
        assert len(snapshots) == 2
        triggers = {s.trigger for s in snapshots}
        assert "pre-edit" in triggers

    def test_clone_from_snapshot(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        meta = create_snapshot(agent_dir)

        new_dir = tmp_path / "test-02"
        assert clone_from_snapshot(agent_dir, meta.snapshot_id, new_dir) is True

        # Verify new agent has identity files
        assert (new_dir / "identity" / "identity.md").exists()
        assert (new_dir / "identity" / "procedures.md").exists()
        assert (new_dir / "journal" / "2026-03-01.md").exists()

        # Verify identity.md references new agent ID
        content = (new_dir / "identity" / "identity.md").read_text()
        assert "test-02" in content
        assert "Cloned from test-01" in content

    def test_clone_has_fresh_today(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        (agent_dir / "today" / "plan.md").write_text("# Old plan\n")
        meta = create_snapshot(agent_dir)

        new_dir = tmp_path / "test-02"
        clone_from_snapshot(agent_dir, meta.snapshot_id, new_dir)

        plan = (new_dir / "today" / "plan.md").read_text()
        assert "Newly cloned" in plan

    def test_delete_snapshot(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        meta = create_snapshot(agent_dir)

        assert delete_snapshot(agent_dir, meta.snapshot_id) is True
        assert list_snapshots(agent_dir) == []

    def test_nonexistent_snapshot(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        assert get_snapshot(agent_dir, "nonexistent") is None
        assert restore_snapshot(agent_dir, "nonexistent") is False
        assert clone_from_snapshot(agent_dir, "nonexistent", tmp_path / "x") is False
        assert delete_snapshot(agent_dir, "nonexistent") is False

    def test_multiple_snapshots_ordered(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        m1 = create_snapshot(agent_dir, name="first")
        m2 = create_snapshot(agent_dir, name="second")

        snapshots = list_snapshots(agent_dir)
        assert len(snapshots) == 2
        # Newest first
        assert snapshots[0].snapshot_id >= snapshots[1].snapshot_id


# ---------------------------------------------------------------------------
# Promotion tests
# ---------------------------------------------------------------------------

class TestPromotion:
    def _make_role_template(self, tmp_path: Path) -> Path:
        """Create a target role template directory."""
        tpl = tmp_path / "head-accounting"
        (tpl / "identity").mkdir(parents=True)
        (tpl / "identity" / "responsibilities.md").write_text(
            "# head-accounting — Responsibilities\n\n"
            "## Primary\n\nManage the accounting department.\n"
            "Supervise bookkeepers. Approve exceptions.\n"
        )
        (tpl / "identity" / "soul.md").write_text(
            "# head-accounting — Persona\n\n"
            "Strategic thinker. Decisive under pressure.\n"
        )
        return tpl

    def test_initiate_promotion(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)

        record = initiate_promotion(agent_dir, tpl, probation_days=14)

        assert record.agent_id == "test-01"
        assert record.status == "probationary"
        assert record.target_role == "head-accounting"
        assert is_probationary(agent_dir) is True

    def test_promotion_creates_snapshot(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)

        record = initiate_promotion(agent_dir, tpl)
        snapshots = list_snapshots(agent_dir)
        assert any("pre-promotion" in s.name for s in snapshots)

    def test_promotion_swaps_responsibilities(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)

        initiate_promotion(agent_dir, tpl)

        resp = (agent_dir / "identity" / "responsibilities.md").read_text()
        assert "Manage the accounting department" in resp

    def test_promotion_updates_identity(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)

        initiate_promotion(agent_dir, tpl)

        identity = (agent_dir / "identity" / "identity.md").read_text()
        assert "Promoted" in identity
        assert "probation" in identity.lower()

    def test_confirm_promotion(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)

        initiate_promotion(agent_dir, tpl)
        record = confirm_promotion(agent_dir)

        assert record is not None
        assert record.status == "confirmed"
        assert record.confirmed_at is not None
        assert is_probationary(agent_dir) is False

    def test_revert_promotion(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)

        initiate_promotion(agent_dir, tpl)
        record = revert_promotion(agent_dir)

        assert record is not None
        assert record.status == "reverted"

        # Identity should be restored to pre-promotion state
        resp = (agent_dir / "identity" / "responsibilities.md").read_text()
        assert "Process invoices" in resp

    def test_extend_probation(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)

        record = initiate_promotion(agent_dir, tpl, probation_days=14)
        original_end = record.probation_end

        extended = extend_probation(agent_dir, additional_days=7)
        assert extended is not None
        assert extended.probation_config.duration_days == 21
        assert extended.probation_end > original_end

    def test_confirm_already_confirmed(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)

        initiate_promotion(agent_dir, tpl)
        confirm_promotion(agent_dir)
        assert confirm_promotion(agent_dir) is None  # Already confirmed

    def test_no_promotion_record(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        assert get_promotion(agent_dir) is None
        assert is_probationary(agent_dir) is False
        assert confirm_promotion(agent_dir) is None
        assert revert_promotion(agent_dir) is None


# ---------------------------------------------------------------------------
# Snapshot Manager tests
# ---------------------------------------------------------------------------

class TestSnapshotManager:
    def test_create_and_list(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        mgr = SnapshotManager(agent_dir, max_snapshots=5)
        meta = mgr.create(name="v1")
        assert meta.name == "v1"
        assert len(mgr.list()) == 1

    def test_retention_enforcement(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        mgr = SnapshotManager(agent_dir, max_snapshots=3)
        for i in range(5):
            mgr.create(name=f"snap-{i}")
        assert len(mgr.list()) == 3

    def test_export_and_import(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        mgr = SnapshotManager(agent_dir)
        meta = mgr.create(name="exportable")

        archive = tmp_path / "export.tar.gz"
        result = mgr.export(meta.snapshot_id, archive)
        assert result is not None
        assert result.exists()

        # Import into a different agent
        agent2 = _make_agent(tmp_path, "test-02")
        imported = import_snapshot(agent2, archive)
        assert imported is not None
        assert imported.agent_id == "test-02"


class TestEnforceRetention:
    def test_no_delete_when_under_limit(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        create_snapshot(agent_dir)
        deleted = enforce_retention(agent_dir, max_snapshots=5)
        assert deleted == []

    def test_deletes_oldest(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        for _ in range(5):
            create_snapshot(agent_dir)
        deleted = enforce_retention(agent_dir, max_snapshots=2)
        assert len(deleted) == 3
        assert len(list_snapshots(agent_dir)) == 2


class TestExportImport:
    def test_export_nonexistent(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        result = export_snapshot(agent_dir, "nonexistent", tmp_path / "out.tar.gz")
        assert result is None

    def test_import_nonexistent(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        result = import_snapshot(agent_dir, tmp_path / "missing.tar.gz")
        assert result is None


# ---------------------------------------------------------------------------
# Promotion Assessment tests
# ---------------------------------------------------------------------------

class TestPromotionAssessment:
    def _make_role_template(self, tmp_path: Path) -> Path:
        tpl = tmp_path / "head-accounting"
        (tpl / "identity").mkdir(parents=True)
        (tpl / "identity" / "responsibilities.md").write_text(
            "# head-accounting — Responsibilities\n\n## Primary\n\nManage dept.\n"
        )
        (tpl / "identity" / "soul.md").write_text(
            "# head-accounting — Persona\n\nDecisive.\n"
        )
        return tpl

    def test_assess_not_probationary(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        assert assess_probation(agent_dir) is None

    def test_assess_probationary(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)
        initiate_promotion(agent_dir, tpl)

        assessment = assess_probation(agent_dir)
        assert assessment is not None
        assert assessment.recommendation == "continue"

    def test_assessment_with_metrics(self, tmp_path: Path) -> None:
        import json
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)
        initiate_promotion(agent_dir, tpl)

        # Write mock task metrics
        (agent_dir / "today" / "task_queue.json").write_text(json.dumps({
            "tasks": [],
            "replan_count": 0,
            "summary": {"done": 8, "pending": 0, "exceptions": 2},
        }))

        assessment = assess_probation(agent_dir)
        assert assessment is not None
        assert assessment.tasks_completed == 8
        assert assessment.tasks_escalated == 2

    def test_assessment_dataclass(self) -> None:
        a = PromotionAssessment(agent_id="x", tasks_completed=9, tasks_escalated=1, total_tasks=10)
        a.escalation_ratio = 0.1
        a.decision_quality = 0.9
        assert a.escalation_ok is True
        assert a.quality_ok is True
        d = a.to_dict()
        assert d["agent_id"] == "x"

    def test_set_backfill(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)
        initiate_promotion(agent_dir, tpl)

        record = set_backfill(agent_dir, "backup-01")
        assert record is not None
        assert record.backfill_agent_id == "backup-01"


class TestPromotionManager:
    def _make_role_template(self, tmp_path: Path) -> Path:
        tpl = tmp_path / "senior-role"
        (tpl / "identity").mkdir(parents=True)
        (tpl / "identity" / "responsibilities.md").write_text(
            "# senior-role — Responsibilities\n\n## Primary\n\nLead.\n"
        )
        (tpl / "identity" / "soul.md").write_text(
            "# senior-role — Persona\n\nLeader.\n"
        )
        return tpl

    def test_manager_lifecycle(self, tmp_path: Path) -> None:
        agent_dir = _make_agent(tmp_path)
        tpl = self._make_role_template(tmp_path)
        mgr = PromotionManager(agent_dir)

        assert mgr.get() is None
        assert mgr.is_probationary is False

        mgr.initiate(tpl, probation_days=7)
        assert mgr.is_probationary is True

        assessment = mgr.assess()
        assert assessment is not None

        mgr.confirm()
        assert mgr.is_probationary is False
