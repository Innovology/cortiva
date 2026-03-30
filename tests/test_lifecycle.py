"""Tests for agent termination and lifecycle cleanup."""

from __future__ import annotations

import json
from pathlib import Path

from cortiva.core.lifecycle import (
    TerminationRecord,
    get_termination_record,
    is_terminated,
    terminate_agent,
)


def _setup_agent(agents_dir: Path, agent_id: str = "agent-001") -> Path:
    """Create a minimal agent directory with identity files."""
    agent_dir = agents_dir / agent_id
    identity = agent_dir / "identity"
    identity.mkdir(parents=True)
    (agent_dir / "today").mkdir()
    (agent_dir / "journal").mkdir()
    (agent_dir / "outbox").mkdir()
    (agent_dir / "workspace").mkdir()

    (identity / "identity.md").write_text("# Agent 001\nA test agent.\n")
    (identity / "soul.md").write_text("# Soul\nHelpful and diligent.\n")
    (identity / "skills.md").write_text("# Skills\n- Python\n- Testing\n")
    (identity / "procedures.md").write_text("# Procedures\n- Always run tests.\n")
    (identity / "responsibilities.md").write_text("# Responsibilities\n- QA\n")
    (agent_dir / "today" / "plan.md").write_text("# Plan\n- Finish tests\n")
    (agent_dir / "journal" / "2026-03-28.md").write_text("Did some testing.\n")
    (agent_dir / "journal" / "2026-03-29.md").write_text("Finished testing.\n")
    return agent_dir


class TestTerminationRecord:
    def test_round_trip(self):
        record = TerminationRecord(
            agent_id="agent-001",
            reason="retirement",
            terminated_at="2026-03-29T00:00:00+00:00",
            successor_id="agent-002",
            snapshot_id="snap-123",
            knowledge_exported=True,
        )
        data = record.to_dict()
        restored = TerminationRecord.from_dict(data)
        assert restored.agent_id == record.agent_id
        assert restored.reason == record.reason
        assert restored.terminated_at == record.terminated_at
        assert restored.successor_id == record.successor_id
        assert restored.snapshot_id == record.snapshot_id
        assert restored.knowledge_exported == record.knowledge_exported

    def test_no_successor(self):
        data = {
            "agent_id": "a1",
            "reason": "decommissioned",
            "terminated_at": "2026-01-01T00:00:00+00:00",
            "snapshot_id": "s1",
            "knowledge_exported": False,
        }
        record = TerminationRecord.from_dict(data)
        assert record.successor_id is None


class TestTerminateAgent:
    def test_basic_termination(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = _setup_agent(agents_dir)

        record = terminate_agent(agent_dir, reason="retirement")

        assert record.agent_id == "agent-001"
        assert record.reason == "retirement"
        assert record.successor_id is None
        assert record.knowledge_exported is True
        assert record.snapshot_id  # non-empty

        # Agent directory moved to archive
        assert not agent_dir.exists()
        archive = agents_dir / ".archive" / "agent-001"
        assert archive.is_dir()

        # Termination record in archive
        term_file = archive / ".terminated.json"
        assert term_file.exists()
        data = json.loads(term_file.read_text())
        assert data["agent_id"] == "agent-001"

        # Knowledge export in archive
        export = archive / "knowledge_export.md"
        assert export.exists()
        content = export.read_text()
        assert "Procedures" in content
        assert "Skills" in content

    def test_termination_with_successor(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = _setup_agent(agents_dir, "agent-001")
        successor_dir = _setup_agent(agents_dir, "agent-002")

        record = terminate_agent(agent_dir, reason="replaced", successor_id="agent-002")

        assert record.successor_id == "agent-002"

        # Successor received predecessor's procedures
        pred_proc = successor_dir / "identity" / "predecessor_agent-001_procedures.md"
        assert pred_proc.exists()
        assert "Always run tests" in pred_proc.read_text()

        # Successor received knowledge export
        pred_knowledge = successor_dir / "identity" / "predecessor_agent-001_knowledge.md"
        assert pred_knowledge.exists()

    def test_termination_no_identity_files(self, tmp_path: Path):
        """Agent with no identity files still terminates cleanly."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = agents_dir / "empty-agent"
        agent_dir.mkdir()

        record = terminate_agent(agent_dir, reason="cleanup")

        assert record.agent_id == "empty-agent"
        assert record.knowledge_exported is False
        assert not agent_dir.exists()
        assert (agents_dir / ".archive" / "empty-agent").is_dir()

    def test_termination_creates_snapshot(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = _setup_agent(agents_dir)

        record = terminate_agent(agent_dir, reason="test")

        # Snapshot should exist in the archived directory
        archive = agents_dir / ".archive" / "agent-001"
        snapshots = archive / ".snapshots"
        assert snapshots.is_dir()
        snap_dirs = list(snapshots.iterdir())
        assert len(snap_dirs) >= 1

    def test_successor_not_found(self, tmp_path: Path):
        """If successor doesn't exist, termination still succeeds."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = _setup_agent(agents_dir)

        record = terminate_agent(
            agent_dir, reason="replaced", successor_id="nonexistent"
        )

        assert record.successor_id == "nonexistent"
        assert not agent_dir.exists()

    def test_archive_overwrites_existing(self, tmp_path: Path):
        """If archive already contains the agent, it gets replaced."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        # Create first agent and terminate
        agent_dir = _setup_agent(agents_dir)
        terminate_agent(agent_dir, reason="first time")

        # Create agent again with same ID and terminate again
        agent_dir = _setup_agent(agents_dir)
        record = terminate_agent(agent_dir, reason="second time")

        assert record.reason == "second time"
        archive = agents_dir / ".archive" / "agent-001"
        data = json.loads((archive / ".terminated.json").read_text())
        assert data["reason"] == "second time"


class TestIsTerminated:
    def test_not_terminated(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = _setup_agent(agents_dir)
        assert is_terminated(agent_dir) is False

    def test_terminated_via_archive(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = _setup_agent(agents_dir)
        terminate_agent(agent_dir, reason="done")

        # Original dir is gone, but is_terminated checks archive
        assert is_terminated(agent_dir) is True

    def test_terminated_record_in_place(self, tmp_path: Path):
        """If .terminated.json exists in agent dir (before archive move)."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = _setup_agent(agents_dir)

        # Manually write termination file without archiving
        term_path = agent_dir / ".terminated.json"
        term_path.write_text(json.dumps({"agent_id": "agent-001"}))

        assert is_terminated(agent_dir) is True


class TestGetTerminationRecord:
    def test_no_record(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = _setup_agent(agents_dir)
        assert get_termination_record(agent_dir) is None

    def test_record_from_archive(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = _setup_agent(agents_dir)
        terminate_agent(agent_dir, reason="retired")

        record = get_termination_record(agent_dir)
        assert record is not None
        assert record.agent_id == "agent-001"
        assert record.reason == "retired"

    def test_record_fields(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = _setup_agent(agents_dir)
        terminate_agent(agent_dir, reason="decommission", successor_id="agent-002")

        record = get_termination_record(agent_dir)
        assert record is not None
        assert record.successor_id == "agent-002"
        assert record.knowledge_exported is True
        assert record.snapshot_id
        assert record.terminated_at
