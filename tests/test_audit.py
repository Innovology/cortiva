"""Tests for cortiva.core.audit — tamper-evident hash-chained audit log."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from cortiva.core.audit import GENESIS_HASH, AuditEntry, AuditLog, _compute_hash


@pytest.fixture()
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "audit"


@pytest.fixture()
def audit(log_dir: Path) -> AuditLog:
    return AuditLog(log_dir)


# ------------------------------------------------------------------
# AuditEntry basics
# ------------------------------------------------------------------


class TestAuditEntry:
    def test_round_trip(self) -> None:
        entry = AuditEntry(
            sequence_number=0,
            timestamp="2026-01-01T00:00:00+00:00",
            event_type="test.event",
            agent_id="agent-1",
            data={"key": "value"},
            previous_hash=GENESIS_HASH,
            entry_hash="abc123",
        )
        restored = AuditEntry.from_dict(json.loads(entry.to_json()))
        assert restored == entry

    def test_to_dict_fields(self) -> None:
        entry = AuditEntry(
            sequence_number=5,
            timestamp="ts",
            event_type="e",
            agent_id="a",
            data={},
            previous_hash="ph",
            entry_hash="eh",
        )
        d = entry.to_dict()
        assert d["sequence_number"] == 5
        assert d["entry_hash"] == "eh"


# ------------------------------------------------------------------
# Hash computation
# ------------------------------------------------------------------


class TestHashComputation:
    def test_deterministic(self) -> None:
        h1 = _compute_hash(0, "ts", "evt", "agent", {"k": 1}, GENESIS_HASH)
        h2 = _compute_hash(0, "ts", "evt", "agent", {"k": 1}, GENESIS_HASH)
        assert h1 == h2

    def test_different_inputs_different_hash(self) -> None:
        h1 = _compute_hash(0, "ts", "evt", "agent", {}, GENESIS_HASH)
        h2 = _compute_hash(1, "ts", "evt", "agent", {}, GENESIS_HASH)
        assert h1 != h2

    def test_hash_is_hex_sha256(self) -> None:
        h = _compute_hash(0, "ts", "evt", "agent", {}, GENESIS_HASH)
        assert len(h) == 64
        int(h, 16)  # must be valid hex


# ------------------------------------------------------------------
# AuditLog.append and read
# ------------------------------------------------------------------


class TestAppendAndRead:
    def test_append_creates_file(self, audit: AuditLog, log_dir: Path) -> None:
        audit.append("test.event", "agent-1")
        today = date.today()
        path = log_dir / f"audit-{today.isoformat()}.jsonl"
        assert path.exists()

    def test_append_returns_entry(self, audit: AuditLog) -> None:
        entry = audit.append("test.event", "agent-1", {"x": 42})
        assert entry.sequence_number == 0
        assert entry.event_type == "test.event"
        assert entry.agent_id == "agent-1"
        assert entry.data == {"x": 42}
        assert entry.previous_hash == GENESIS_HASH

    def test_sequential_entries_chain(self, audit: AuditLog) -> None:
        e1 = audit.append("a", "agent-1")
        e2 = audit.append("b", "agent-1")
        assert e2.previous_hash == e1.entry_hash
        assert e2.sequence_number == 1

    def test_read_returns_entries(self, audit: AuditLog) -> None:
        audit.append("a", "agent-1")
        audit.append("b", "agent-1")
        entries = audit.read(date.today())
        assert len(entries) == 2
        assert entries[0].event_type == "a"
        assert entries[1].event_type == "b"

    def test_read_with_limit(self, audit: AuditLog) -> None:
        for i in range(5):
            audit.append(f"evt-{i}", "agent-1")
        entries = audit.read(date.today(), limit=3)
        assert len(entries) == 3

    def test_read_empty_date(self, audit: AuditLog) -> None:
        entries = audit.read(date(2000, 1, 1))
        assert entries == []

    def test_default_data_is_empty_dict(self, audit: AuditLog) -> None:
        entry = audit.append("evt", "agent-1")
        assert entry.data == {}


# ------------------------------------------------------------------
# AuditLog.verify
# ------------------------------------------------------------------


class TestVerify:
    def test_valid_chain(self, audit: AuditLog) -> None:
        for i in range(10):
            audit.append(f"evt-{i}", "agent-1", {"i": i})
        valid, broken_at = audit.verify(date.today())
        assert valid is True
        assert broken_at is None

    def test_empty_date_is_valid(self, audit: AuditLog) -> None:
        valid, broken_at = audit.verify(date(2000, 1, 1))
        assert valid is True
        assert broken_at is None

    def test_tamper_detection_modified_data(self, audit: AuditLog, log_dir: Path) -> None:
        """Modify data in a middle entry and verify detects it."""
        for i in range(5):
            audit.append(f"evt-{i}", "agent-1", {"i": i})

        today = date.today()
        path = log_dir / f"audit-{today.isoformat()}.jsonl"

        lines = path.read_text().splitlines()
        # Tamper with entry at index 2
        record = json.loads(lines[2])
        record["data"]["i"] = 999  # modify payload
        lines[2] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")

        valid, broken_at = audit.verify(today)
        assert valid is False
        assert broken_at == 2

    def test_tamper_detection_modified_hash(self, audit: AuditLog, log_dir: Path) -> None:
        """Corrupt entry_hash directly and verify detects it."""
        audit.append("evt", "agent-1")
        audit.append("evt", "agent-1")

        today = date.today()
        path = log_dir / f"audit-{today.isoformat()}.jsonl"

        lines = path.read_text().splitlines()
        record = json.loads(lines[0])
        record["entry_hash"] = "bad" + record["entry_hash"][3:]
        lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")

        valid, broken_at = audit.verify(today)
        assert valid is False
        assert broken_at == 0

    def test_tamper_detection_broken_chain(self, audit: AuditLog, log_dir: Path) -> None:
        """Remove an entry and verify the chain breaks."""
        for i in range(4):
            audit.append(f"evt-{i}", "agent-1")

        today = date.today()
        path = log_dir / f"audit-{today.isoformat()}.jsonl"

        lines = path.read_text().splitlines()
        # Remove entry 1, so entry 2's previous_hash won't match entry 0
        del lines[1]
        path.write_text("\n".join(lines) + "\n")

        valid, broken_at = audit.verify(today)
        assert valid is False


# ------------------------------------------------------------------
# Cross-day chaining
# ------------------------------------------------------------------


class TestCrossDayChaining:
    def test_chains_from_previous_day(self, log_dir: Path) -> None:
        """First entry of a new day chains from last entry of previous day."""
        audit = AuditLog(log_dir)
        yesterday = date.today() - timedelta(days=1)

        # Manually write an entry for yesterday
        entry = AuditEntry(
            sequence_number=0,
            timestamp="2026-01-01T00:00:00+00:00",
            event_type="old",
            agent_id="agent-1",
            data={},
            previous_hash=GENESIS_HASH,
            entry_hash=_compute_hash(
                0, "2026-01-01T00:00:00+00:00", "old", "agent-1", {}, GENESIS_HASH
            ),
        )
        path = log_dir / f"audit-{yesterday.isoformat()}.jsonl"
        path.write_text(entry.to_json() + "\n")

        # Append today — should chain from yesterday's last hash
        today_entry = audit.append("new", "agent-1")
        assert today_entry.previous_hash == entry.entry_hash
        assert today_entry.sequence_number == 0

    def test_genesis_when_no_prior(self, audit: AuditLog) -> None:
        entry = audit.append("first", "agent-1")
        assert entry.previous_hash == GENESIS_HASH


# ------------------------------------------------------------------
# Directory creation
# ------------------------------------------------------------------


class TestInit:
    def test_creates_log_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "deep" / "nested" / "audit"
        AuditLog(d)
        assert d.is_dir()
