"""
Agent termination and lifecycle cleanup.

Provides formal agent retirement: final snapshot, knowledge export,
archival, and optional successor handoff.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cortiva.core.snapshots import create_snapshot


@dataclass
class TerminationRecord:
    """Record of an agent's termination."""

    agent_id: str
    reason: str
    terminated_at: str
    successor_id: str | None
    snapshot_id: str
    knowledge_exported: bool

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "reason": self.reason,
            "terminated_at": self.terminated_at,
            "successor_id": self.successor_id,
            "snapshot_id": self.snapshot_id,
            "knowledge_exported": self.knowledge_exported,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TerminationRecord:
        return cls(
            agent_id=data["agent_id"],
            reason=data["reason"],
            terminated_at=data["terminated_at"],
            successor_id=data.get("successor_id"),
            snapshot_id=data["snapshot_id"],
            knowledge_exported=data["knowledge_exported"],
        )


_TERMINATION_FILE = ".terminated.json"


def _termination_path(agent_dir: Path) -> Path:
    return agent_dir / _TERMINATION_FILE


def _extract_high_importance_memories(agent_dir: Path) -> str:
    """Build a knowledge summary from an agent's identity files and journal.

    Produces a plain-text document suitable for successor onboarding.
    """
    sections: list[str] = []

    identity_files = [
        ("Procedures", "procedures.md"),
        ("Skills", "skills.md"),
        ("Identity Summary", "identity.md"),
    ]
    for heading, filename in identity_files:
        path = agent_dir / "identity" / filename
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(f"# {heading}\n\n{content}")

    journal_dir = agent_dir / "journal"
    if journal_dir.is_dir():
        entries = sorted(journal_dir.iterdir(), reverse=True)
        journal_texts: list[str] = []
        for entry in entries[:10]:
            if entry.is_file() and entry.suffix == ".md":
                journal_texts.append(
                    f"## {entry.stem}\n\n{entry.read_text(encoding='utf-8').strip()}"
                )
        if journal_texts:
            sections.append("# Journal Entries\n\n" + "\n\n".join(journal_texts))

    if not sections:
        return ""
    return "\n\n---\n\n".join(sections) + "\n"


def terminate_agent(
    agent_dir: Path,
    reason: str,
    successor_id: str | None = None,
) -> TerminationRecord:
    """Terminate an agent: snapshot, export knowledge, archive.

    1. Creates a final snapshot of the agent's state.
    2. Exports high-importance memories as a summary file.
    3. Writes a termination record to ``{agent_dir}/.terminated.json``.
    4. Moves the agent directory to ``{agents_dir}/.archive/{agent_id}/``.
    5. If *successor_id* is provided, copies key procedures to the
       successor's workspace.

    Returns the :class:`TerminationRecord`.
    """
    agent_id = agent_dir.name
    agents_dir = agent_dir.parent
    now = datetime.now(tz=UTC)

    meta = create_snapshot(
        agent_dir,
        name=f"termination-{now.strftime('%Y%m%d')}",
        trigger="manual",
    )

    summary = _extract_high_importance_memories(agent_dir)
    knowledge_exported = bool(summary)
    if knowledge_exported:
        summary_path = agent_dir / "knowledge_export.md"
        summary_path.write_text(summary, encoding="utf-8")

    record = TerminationRecord(
        agent_id=agent_id,
        reason=reason,
        terminated_at=now.isoformat(),
        successor_id=successor_id,
        snapshot_id=meta.snapshot_id,
        knowledge_exported=knowledge_exported,
    )
    _termination_path(agent_dir).write_text(
        json.dumps(record.to_dict(), indent=2), encoding="utf-8"
    )

    # Copy key knowledge to successor before archiving
    if successor_id is not None:
        successor_dir = agents_dir / successor_id
        if successor_dir.is_dir():
            dest = successor_dir / "identity"
            dest.mkdir(parents=True, exist_ok=True)
            src_procedures = agent_dir / "identity" / "procedures.md"
            if src_procedures.exists():
                shutil.copy2(
                    str(src_procedures),
                    str(dest / f"predecessor_{agent_id}_procedures.md"),
                )
            export_path = agent_dir / "knowledge_export.md"
            if export_path.exists():
                shutil.copy2(
                    str(export_path),
                    str(dest / f"predecessor_{agent_id}_knowledge.md"),
                )

    # Archive: move agent directory out of the active agents dir
    archive_dir = agents_dir / ".archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_dest = archive_dir / agent_id
    if archive_dest.exists():
        shutil.rmtree(archive_dest)
    shutil.move(str(agent_dir), str(archive_dest))

    return record


def is_terminated(agent_dir: Path) -> bool:
    """Check if an agent has been terminated.

    Looks for the termination record in the agent directory itself
    (if it hasn't been archived yet) and in the archive location.
    """
    if _termination_path(agent_dir).exists():
        return True
    # Check archive
    agents_dir = agent_dir.parent
    agent_id = agent_dir.name
    archive_path = agents_dir / ".archive" / agent_id / _TERMINATION_FILE
    return archive_path.exists()


def get_termination_record(agent_dir: Path) -> TerminationRecord | None:
    """Read the termination record for an agent.

    Checks both the original directory and the archive location.
    """
    path = _termination_path(agent_dir)
    if not path.exists():
        # Check archive
        agents_dir = agent_dir.parent
        agent_id = agent_dir.name
        path = agents_dir / ".archive" / agent_id / _TERMINATION_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return TerminationRecord.from_dict(data)
