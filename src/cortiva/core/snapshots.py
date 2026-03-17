"""
Agent snapshot engine — capture, restore, and clone agent state.

A snapshot preserves an agent's identity, journal, and metrics at a point
in time. Snapshots enable rollback, cloning, and cross-cluster portability.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class SnapshotMetadata:
    """Metadata about a snapshot."""

    agent_id: str
    snapshot_id: str
    name: str
    description: str
    created_at: str
    trigger: str  # "manual" | "pre-edit" | "pre-migration" | "milestone" | "scheduled"
    node_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "snapshot_id": self.snapshot_id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "trigger": self.trigger,
            "node_id": self.node_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SnapshotMetadata:
        return cls(
            agent_id=data["agent_id"],
            snapshot_id=data["snapshot_id"],
            name=data["name"],
            description=data.get("description", ""),
            created_at=data["created_at"],
            trigger=data.get("trigger", "manual"),
            node_id=data.get("node_id"),
        )


def _snapshot_dir(agent_dir: Path) -> Path:
    """Return the snapshots directory for an agent."""
    return agent_dir / ".snapshots"


def _timestamp_id() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%S-%f")


def create_snapshot(
    agent_dir: Path,
    name: str = "",
    description: str = "",
    trigger: str = "manual",
    node_id: str | None = None,
) -> SnapshotMetadata:
    """Capture a snapshot of an agent's current state.

    Copies ``identity/`` and ``journal/`` into a timestamped snapshot
    directory.  Returns the snapshot metadata.
    """
    agent_id = agent_dir.name
    snapshot_id = _timestamp_id()
    snap_root = _snapshot_dir(agent_dir) / snapshot_id
    snap_root.mkdir(parents=True, exist_ok=True)

    # Copy identity files
    identity_src = agent_dir / "identity"
    if identity_src.is_dir():
        shutil.copytree(identity_src, snap_root / "identity")

    # Copy journal
    journal_src = agent_dir / "journal"
    if journal_src.is_dir():
        shutil.copytree(journal_src, snap_root / "journal")

    # Capture basic metrics if they exist in today/
    metrics_dir = snap_root / "metrics"
    metrics_dir.mkdir(exist_ok=True)
    for metrics_file in ("task_queue.json", "familiarity_signals.json", "exception_pile.json"):
        src = agent_dir / "today" / metrics_file
        if src.exists():
            shutil.copy2(src, metrics_dir / metrics_file)

    # Write metadata
    meta = SnapshotMetadata(
        agent_id=agent_id,
        snapshot_id=snapshot_id,
        name=name or snapshot_id,
        description=description,
        created_at=datetime.now(tz=UTC).isoformat(),
        trigger=trigger,
        node_id=node_id,
    )
    meta_path = snap_root / "snapshot.json"
    meta_path.write_text(json.dumps(meta.to_dict(), indent=2), encoding="utf-8")

    return meta


def list_snapshots(agent_dir: Path) -> list[SnapshotMetadata]:
    """List all snapshots for an agent, newest first."""
    snap_root = _snapshot_dir(agent_dir)
    if not snap_root.is_dir():
        return []

    snapshots: list[SnapshotMetadata] = []
    for entry in sorted(snap_root.iterdir(), reverse=True):
        meta_path = entry / "snapshot.json"
        if meta_path.exists():
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            snapshots.append(SnapshotMetadata.from_dict(data))

    return snapshots


def get_snapshot(agent_dir: Path, snapshot_id: str) -> Path | None:
    """Return the path to a specific snapshot, or None if not found."""
    snap_path = _snapshot_dir(agent_dir) / snapshot_id
    if snap_path.is_dir() and (snap_path / "snapshot.json").exists():
        return snap_path
    return None


def restore_snapshot(
    agent_dir: Path,
    snapshot_id: str,
    restore_journal: bool = True,
) -> bool:
    """Restore an agent's identity from a snapshot.

    Always restores ``identity/``.  Optionally restores ``journal/``.
    Creates a pre-restore snapshot automatically.

    Returns True if the restore succeeded.
    """
    snap_path = get_snapshot(agent_dir, snapshot_id)
    if snap_path is None:
        return False

    # Pre-restore safety snapshot
    create_snapshot(
        agent_dir,
        name=f"pre-restore-{snapshot_id}",
        trigger="pre-edit",
    )

    # Restore identity
    snap_identity = snap_path / "identity"
    if snap_identity.is_dir():
        target = agent_dir / "identity"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(snap_identity, target)

    # Optionally restore journal
    if restore_journal:
        snap_journal = snap_path / "journal"
        if snap_journal.is_dir():
            target = agent_dir / "journal"
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(snap_journal, target)

    return True


def clone_from_snapshot(
    agent_dir: Path,
    snapshot_id: str,
    new_agent_dir: Path,
) -> bool:
    """Create a new agent from a snapshot of an existing one.

    The new agent gets the snapshot's identity and journal but with a
    fresh ``today/`` and ``outbox/``.  The new agent's identity.md is
    updated to reference its new ID.

    Returns True if the clone succeeded.
    """
    snap_path = get_snapshot(agent_dir, snapshot_id)
    if snap_path is None:
        return False

    from cortiva.core.agent import WORKSPACE_DIRS

    new_agent_dir.mkdir(parents=True, exist_ok=True)
    for subdir in WORKSPACE_DIRS:
        (new_agent_dir / subdir).mkdir(exist_ok=True)

    # Copy identity from snapshot
    snap_identity = snap_path / "identity"
    if snap_identity.is_dir():
        target = new_agent_dir / "identity"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(snap_identity, target)

    # Copy journal from snapshot
    snap_journal = snap_path / "journal"
    if snap_journal.is_dir():
        target = new_agent_dir / "journal"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(snap_journal, target)

    # Update identity.md to reference new agent ID
    new_id = new_agent_dir.name
    source_id = agent_dir.name
    identity_md = new_agent_dir / "identity" / "identity.md"
    if identity_md.exists():
        content = identity_md.read_text(encoding="utf-8")
        header = (
            f"# {new_id}\n\n"
            f"*Cloned from {source_id} on {datetime.now(tz=UTC).strftime('%Y-%m-%d')}. "
            f"Inherits procedures and experience from the source agent.*\n\n"
        )
        # Replace the first heading line
        lines = content.split("\n")
        if lines and lines[0].startswith("# "):
            lines[0] = f"# {new_id}"
            content = "\n".join(lines)
            # Insert clone note after first heading
            content = content.replace(
                f"# {new_id}\n",
                header,
                1,
            )
        identity_md.write_text(content, encoding="utf-8")

    # Create default plan for new agent
    plan_path = new_agent_dir / "today" / "plan.md"
    plan_path.write_text(
        f"# {new_id} — Plan\n\nNewly cloned agent. Awaiting first wake cycle.\n",
        encoding="utf-8",
    )

    return True


def delete_snapshot(agent_dir: Path, snapshot_id: str) -> bool:
    """Delete a specific snapshot. Returns True if deleted."""
    snap_path = get_snapshot(agent_dir, snapshot_id)
    if snap_path is None:
        return False
    shutil.rmtree(snap_path)
    return True


def enforce_retention(agent_dir: Path, max_snapshots: int = 20) -> list[str]:
    """Delete oldest snapshots to stay within the retention limit.

    Returns the list of deleted snapshot IDs.
    """
    snapshots = list_snapshots(agent_dir)  # newest first
    if len(snapshots) <= max_snapshots:
        return []

    to_delete = snapshots[max_snapshots:]
    deleted: list[str] = []
    for snap in to_delete:
        if delete_snapshot(agent_dir, snap.snapshot_id):
            deleted.append(snap.snapshot_id)
    return deleted


def export_snapshot(agent_dir: Path, snapshot_id: str, dest: Path) -> Path | None:
    """Export a snapshot as a tar.gz archive.

    Returns the path to the created archive, or None if the snapshot
    doesn't exist.
    """
    import tarfile

    snap_path = get_snapshot(agent_dir, snapshot_id)
    if snap_path is None:
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    archive_path = dest if str(dest).endswith(".tar.gz") else dest.with_suffix(".tar.gz")

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(snap_path, arcname=snapshot_id)

    return archive_path


def import_snapshot(agent_dir: Path, archive_path: Path) -> SnapshotMetadata | None:
    """Import a snapshot from a tar.gz archive.

    Extracts the archive into the agent's ``.snapshots`` directory.
    Returns the imported snapshot metadata, or None on failure.
    """
    import tarfile

    if not archive_path.exists():
        return None

    snap_root = _snapshot_dir(agent_dir)
    snap_root.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as tar:
        # Security: check for path traversal
        for member in tar.getmembers():
            resolved = (snap_root / member.name).resolve()
            if not str(resolved).startswith(str(snap_root.resolve())):
                return None
        tar.extractall(path=snap_root)

    # Find the extracted snapshot directory and read its metadata
    for entry in snap_root.iterdir():
        meta_path = entry / "snapshot.json"
        if meta_path.exists():
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            meta = SnapshotMetadata.from_dict(data)
            # Update agent_id to match destination agent
            meta.agent_id = agent_dir.name
            meta_path.write_text(
                json.dumps(meta.to_dict(), indent=2), encoding="utf-8"
            )
            return meta

    return None


def export_memory(agent_dir: Path, dest: Path) -> Path:
    """Export an agent's full state (identity + journal + snapshots) as tar.gz.

    This is a comprehensive export for cross-cluster portability.
    """
    import tarfile

    dest.parent.mkdir(parents=True, exist_ok=True)
    archive_path = dest if str(dest).endswith(".tar.gz") else dest.with_suffix(".tar.gz")
    agent_id = agent_dir.name

    with tarfile.open(archive_path, "w:gz") as tar:
        for subdir in ("identity", "journal"):
            src = agent_dir / subdir
            if src.is_dir():
                tar.add(src, arcname=f"{agent_id}/{subdir}")
        # Include snapshots
        snap_dir = _snapshot_dir(agent_dir)
        if snap_dir.is_dir():
            tar.add(snap_dir, arcname=f"{agent_id}/.snapshots")

    return archive_path


class SnapshotManager:
    """High-level manager wrapping snapshot operations with retention policy.

    Parameters
    ----------
    agent_dir:
        The agent's root directory.
    max_snapshots:
        Maximum number of snapshots to retain. Oldest are pruned after
        each ``create()`` call.
    """

    def __init__(self, agent_dir: Path, max_snapshots: int = 20) -> None:
        self.agent_dir = agent_dir
        self.max_snapshots = max_snapshots

    def create(
        self,
        name: str = "",
        description: str = "",
        trigger: str = "manual",
        node_id: str | None = None,
    ) -> SnapshotMetadata:
        """Create a snapshot and enforce retention policy."""
        meta = create_snapshot(
            self.agent_dir,
            name=name,
            description=description,
            trigger=trigger,
            node_id=node_id,
        )
        enforce_retention(self.agent_dir, self.max_snapshots)
        return meta

    def list(self) -> list[SnapshotMetadata]:
        return list_snapshots(self.agent_dir)

    def get(self, snapshot_id: str) -> Path | None:
        return get_snapshot(self.agent_dir, snapshot_id)

    def restore(self, snapshot_id: str, restore_journal: bool = True) -> bool:
        return restore_snapshot(self.agent_dir, snapshot_id, restore_journal)

    def clone(self, snapshot_id: str, new_agent_dir: Path) -> bool:
        return clone_from_snapshot(self.agent_dir, snapshot_id, new_agent_dir)

    def delete(self, snapshot_id: str) -> bool:
        return delete_snapshot(self.agent_dir, snapshot_id)

    def export(self, snapshot_id: str, dest: Path) -> Path | None:
        return export_snapshot(self.agent_dir, snapshot_id, dest)

    def import_archive(self, archive_path: Path) -> SnapshotMetadata | None:
        return import_snapshot(self.agent_dir, archive_path)

    def export_full(self, dest: Path) -> Path:
        return export_memory(self.agent_dir, dest)
