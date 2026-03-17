"""Tests for snapshot sanitisation."""

import json
from pathlib import Path

import pytest

from cortiva.core.sanitise import (
    RedactionMatch,
    SanitisationRule,
    SanitisationRules,
    SnapshotSanitiser,
)


def _make_snapshot(tmp_path: Path) -> Path:
    """Create a minimal snapshot directory with test content."""
    snap = tmp_path / "2026-03-09T12-00-00-000000"
    (snap / "identity").mkdir(parents=True)
    (snap / "journal").mkdir()
    (snap / "metrics").mkdir()

    (snap / "identity" / "identity.md").write_text(
        "# bookkeep-01\n\n"
        "I handle invoices for acme@corp.com and review entries.\n"
        "Budget is $45,000 per quarter.\n"
    )
    (snap / "identity" / "procedures.md").write_text(
        "# Procedures\n\n"
        "1. Check invoice from https://vendor-portal.example.com/api\n"
        "2. Call 555-123-4567 if amount exceeds $10,000\n"
    )
    (snap / "journal" / "2026-03-01.md").write_text(
        "# 2026-03-01\n\nProcessed 12 invoices for Acme Corp today.\n"
    )
    (snap / "metrics" / "task_queue.json").write_text(
        json.dumps({"tasks": [], "summary": {"done": 12}})
    )
    (snap / "snapshot.json").write_text(
        json.dumps({"agent_id": "bookkeep-01", "snapshot_id": snap.name,
                     "name": "test", "created_at": "2026-03-09", "trigger": "manual"})
    )
    return snap


class TestSanitisationRules:
    def test_default_rules(self) -> None:
        rules = SanitisationRules.default()
        assert len(rules.rules) >= 4
        names = {r.name for r in rules.rules}
        assert "email" in names
        assert "phone" in names
        assert "url" in names
        assert "currency" in names

    def test_add_custom_rule(self) -> None:
        rules = SanitisationRules.default()
        rules.add_rule("company", r"Acme\s+Corp", "[COMPANY]")
        assert any(r.name == "company" for r in rules.rules)

    def test_from_dict(self) -> None:
        data = {
            "rules": [
                {"name": "ssn", "pattern": r"\d{3}-\d{2}-\d{4}", "replacement": "[SSN]"},
            ],
            "strip_journal": False,
        }
        rules = SanitisationRules.from_dict(data)
        assert len(rules.rules) == 1
        assert rules.strip_journal is False


class TestSnapshotSanitiser:
    def test_preview(self, tmp_path: Path) -> None:
        snap = _make_snapshot(tmp_path)
        sanitiser = SnapshotSanitiser(SanitisationRules.default())
        matches = sanitiser.preview(snap)

        # Should find: email, url, phone, currency amounts, and journal stripping
        rule_names = {m.rule_name for m in matches}
        assert "email" in rule_names
        assert "url" in rule_names
        assert "strip_journal" in rule_names

    def test_preview_finds_currency(self, tmp_path: Path) -> None:
        snap = _make_snapshot(tmp_path)
        sanitiser = SnapshotSanitiser(SanitisationRules.default())
        matches = sanitiser.preview(snap)
        currency_matches = [m for m in matches if m.rule_name == "currency"]
        assert len(currency_matches) >= 1

    def test_sanitise_and_export(self, tmp_path: Path) -> None:
        snap = _make_snapshot(tmp_path)
        sanitiser = SnapshotSanitiser(SanitisationRules.default())
        output = tmp_path / "sanitised.tar.gz"
        result = sanitiser.sanitise_and_export(snap, output)

        assert result.exists()
        assert result.suffix == ".gz"

    def test_sanitise_preserves_metrics(self, tmp_path: Path) -> None:
        snap = _make_snapshot(tmp_path)
        sanitiser = SnapshotSanitiser(SanitisationRules.default())
        matches = sanitiser.preview(snap)

        # Metrics should not appear in matches
        metric_matches = [m for m in matches if "metrics/" in m.file]
        assert len(metric_matches) == 0

    def test_sanitise_strips_journal(self, tmp_path: Path) -> None:
        snap = _make_snapshot(tmp_path)
        rules = SanitisationRules.default()
        rules.strip_journal = True
        sanitiser = SnapshotSanitiser(rules)

        import tarfile
        import tempfile

        output = tmp_path / "out.tar.gz"
        sanitiser.sanitise_and_export(snap, output)

        with tarfile.open(output, "r:gz") as tar:
            names = tar.getnames()
            journal_files = [n for n in names if "journal" in n]
            assert len(journal_files) == 0

    def test_sanitise_keeps_journal_when_configured(self, tmp_path: Path) -> None:
        snap = _make_snapshot(tmp_path)
        rules = SanitisationRules.default()
        rules.strip_journal = False
        sanitiser = SnapshotSanitiser(rules)

        matches = sanitiser.preview(snap)
        journal_strip = [m for m in matches if m.rule_name == "strip_journal"]
        assert len(journal_strip) == 0

    def test_sanitise_in_place(self, tmp_path: Path) -> None:
        snap = _make_snapshot(tmp_path)
        sanitiser = SnapshotSanitiser(SanitisationRules.default())
        modified = sanitiser.sanitise_in_place(snap)
        assert modified >= 1

        # Check that email was redacted
        content = (snap / "identity" / "identity.md").read_text()
        assert "acme@corp.com" not in content
        assert "[EMAIL]" in content

    def test_custom_company_rule(self, tmp_path: Path) -> None:
        snap = _make_snapshot(tmp_path)
        rules = SanitisationRules.default()
        rules.add_rule("company", r"Acme\s+Corp", "[COMPANY]")
        sanitiser = SnapshotSanitiser(rules)

        sanitiser.sanitise_in_place(snap)
        journal_gone = not (snap / "journal").exists()
        # Check identity files for company redaction
        content = (snap / "identity" / "identity.md").read_text()
        assert "Acme Corp" not in content

    def test_snapshot_json_preserved(self, tmp_path: Path) -> None:
        snap = _make_snapshot(tmp_path)
        original = (snap / "snapshot.json").read_text()
        sanitiser = SnapshotSanitiser(SanitisationRules.default())
        sanitiser.sanitise_in_place(snap)
        assert (snap / "snapshot.json").read_text() == original
