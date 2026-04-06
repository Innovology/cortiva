"""
Cortiva AuditLog — tamper-evident, hash-chained audit trail.

Each entry includes a SHA-256 hash that chains to the previous entry,
making any modification to historical entries detectable via verify().
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64


def _compute_hash(
    sequence_number: int,
    timestamp: str,
    event_type: str,
    agent_id: str,
    data: dict[str, Any],
    previous_hash: str,
) -> str:
    json_data = json.dumps(data, sort_keys=True, separators=(",", ":"))
    payload = f"{sequence_number}:{timestamp}:{event_type}:{agent_id}:{json_data}:{previous_hash}"
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class AuditEntry:
    """A single tamper-evident audit log entry."""

    sequence_number: int
    timestamp: str
    event_type: str
    agent_id: str
    data: dict[str, Any]
    previous_hash: str
    entry_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AuditEntry:
        return cls(
            sequence_number=d["sequence_number"],
            timestamp=d["timestamp"],
            event_type=d["event_type"],
            agent_id=d["agent_id"],
            data=d["data"],
            previous_hash=d["previous_hash"],
            entry_hash=d["entry_hash"],
        )


class AuditLog:
    """Hash-chained audit log with daily file rotation.

    Files are stored as ``audit-YYYY-MM-DD.jsonl`` inside *log_dir*.
    The first entry of each day chains from the last entry of the previous
    day's file, or uses a genesis hash if no prior file exists.
    """

    def __init__(self, log_dir: Path | str) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(
        self,
        event_type: str,
        agent_id: str,
        data: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Append a new entry, computing the hash chain automatically."""
        data = data or {}
        today = date.today()
        path = self._path_for(today)

        # Determine previous hash and sequence number
        last = self._last_entry(today)
        if last is not None:
            previous_hash = last.entry_hash
            sequence_number = last.sequence_number + 1
        else:
            previous_hash = self._previous_day_hash(today)
            sequence_number = 0

        timestamp = datetime.now(UTC).isoformat()
        entry_hash = _compute_hash(
            sequence_number, timestamp, event_type, agent_id, data, previous_hash
        )

        entry = AuditEntry(
            sequence_number=sequence_number,
            timestamp=timestamp,
            event_type=event_type,
            agent_id=agent_id,
            data=data,
            previous_hash=previous_hash,
            entry_hash=entry_hash,
        )

        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.to_json() + "\n")

        return entry

    def verify(self, target_date: date) -> tuple[bool, int | None]:
        """Verify the hash chain for *target_date*.

        Returns ``(True, None)`` if the chain is intact, or
        ``(False, sequence_number)`` indicating the first broken entry.
        """
        entries = self.read(target_date)
        if not entries:
            return (True, None)

        expected_prev = self._previous_day_hash(target_date)

        for entry in entries:
            if entry.previous_hash != expected_prev:
                return (False, entry.sequence_number)

            expected_hash = _compute_hash(
                entry.sequence_number,
                entry.timestamp,
                entry.event_type,
                entry.agent_id,
                entry.data,
                entry.previous_hash,
            )
            if entry.entry_hash != expected_hash:
                return (False, entry.sequence_number)

            expected_prev = entry.entry_hash

        return (True, None)

    def read(self, target_date: date, limit: int | None = None) -> list[AuditEntry]:
        """Read entries for *target_date*, optionally limited to *limit*."""
        path = self._path_for(target_date)
        if not path.exists():
            return []

        entries: list[AuditEntry] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entries.append(AuditEntry.from_dict(json.loads(line)))
                if limit is not None and len(entries) >= limit:
                    break

        return entries

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path_for(self, target_date: date) -> Path:
        return self._log_dir / f"audit-{target_date.isoformat()}.jsonl"

    def _last_entry(self, target_date: date) -> AuditEntry | None:
        """Return the last entry written for *target_date*, or None."""
        path = self._path_for(target_date)
        if not path.exists():
            return None

        last_line: str | None = None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    last_line = stripped

        if last_line is None:
            return None
        return AuditEntry.from_dict(json.loads(last_line))

    def _previous_day_hash(self, target_date: date) -> str:
        """Find the last hash from the most recent prior day's log, or genesis."""
        # Scan existing files for the most recent date before target_date
        candidates: list[date] = []
        for p in self._log_dir.glob("audit-*.jsonl"):
            try:
                d = date.fromisoformat(p.stem.removeprefix("audit-"))
                if d < target_date:
                    candidates.append(d)
            except ValueError:
                continue

        if not candidates:
            return GENESIS_HASH

        candidates.sort(reverse=True)
        for d in candidates:
            last = self._last_entry(d)
            if last is not None:
                return last.entry_hash

        return GENESIS_HASH
