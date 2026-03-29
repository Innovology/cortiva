"""
Snapshot sanitisation — strip sensitive data before sharing.

Processes snapshot files to remove company-specific information like
email addresses, phone numbers, URLs, currency amounts, and custom
patterns. Supports preview mode and configurable rules.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SanitisationRule:
    """A single redaction rule."""

    name: str
    pattern: str  # regex pattern
    replacement: str = "[REDACTED]"
    enabled: bool = True

    def compile(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


@dataclass
class SanitisationRules:
    """Collection of sanitisation rules with defaults."""

    rules: list[SanitisationRule] = field(default_factory=list)
    strip_journal: bool = True
    strip_memory: bool = True

    @classmethod
    def default(cls) -> SanitisationRules:
        """Create rules with sensible defaults."""
        return cls(
            rules=[
                SanitisationRule(
                    name="email",
                    pattern=r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                    replacement="[EMAIL]",
                ),
                SanitisationRule(
                    name="phone",
                    pattern=r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}",
                    replacement="[PHONE]",
                ),
                SanitisationRule(
                    name="url",
                    pattern=r"https?://[^\s)\]>\"']+",
                    replacement="[URL]",
                ),
                SanitisationRule(
                    name="currency",
                    pattern=r"[$£€¥]\s?\d[\d,]*\.?\d*",
                    replacement="[AMOUNT]",
                ),
                SanitisationRule(
                    name="ip_address",
                    pattern=r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
                    replacement="[IP]",
                ),
            ],
            strip_journal=True,
            strip_memory=True,
        )

    def add_rule(self, name: str, pattern: str, replacement: str = "[REDACTED]") -> None:
        """Add a custom rule."""
        self.rules.append(SanitisationRule(name=name, pattern=pattern, replacement=replacement))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SanitisationRules:
        """Load rules from a dictionary (e.g. parsed YAML/JSON)."""
        rules = [
            SanitisationRule(
                name=r.get("name", f"rule-{i}"),
                pattern=r["pattern"],
                replacement=r.get("replacement", "[REDACTED]"),
                enabled=r.get("enabled", True),
            )
            for i, r in enumerate(data.get("rules", []))
        ]
        return cls(
            rules=rules,
            strip_journal=data.get("strip_journal", True),
            strip_memory=data.get("strip_memory", True),
        )


@dataclass
class RedactionMatch:
    """A single redaction found during preview."""

    file: str
    line_number: int
    rule_name: str
    original: str
    replacement: str


class SnapshotSanitiser:
    """Sanitises snapshot content by applying redaction rules.

    Usage::

        sanitiser = SnapshotSanitiser(SanitisationRules.default())

        # Preview what will change
        matches = sanitiser.preview(snapshot_path)
        for m in matches:
            print(f"{m.file}:{m.line_number} [{m.rule_name}] {m.original} → {m.replacement}")

        # Apply sanitisation and export
        sanitiser.sanitise_and_export(snapshot_path, output_path)
    """

    def __init__(self, rules: SanitisationRules) -> None:
        self.rules = rules
        self._compiled: list[tuple[SanitisationRule, re.Pattern[str]]] = [
            (rule, rule.compile())
            for rule in rules.rules
            if rule.enabled
        ]

    def _apply_rules(self, text: str) -> str:
        """Apply all rules to a string."""
        for rule, pattern in self._compiled:
            text = pattern.sub(rule.replacement, text)
        return text

    def _scan_file(self, path: Path, rel_path: str) -> list[RedactionMatch]:
        """Scan a file and return all matches without modifying it."""
        matches: list[RedactionMatch] = []
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return matches

        for line_num, line in enumerate(content.split("\n"), 1):
            for rule, pattern in self._compiled:
                for match in pattern.finditer(line):
                    matches.append(RedactionMatch(
                        file=rel_path,
                        line_number=line_num,
                        rule_name=rule.name,
                        original=match.group(),
                        replacement=rule.replacement,
                    ))
        return matches

    def preview(self, snapshot_path: Path) -> list[RedactionMatch]:
        """Preview all redactions that would be applied to a snapshot.

        Returns a list of RedactionMatch objects describing each change.
        Does not modify any files.
        """
        matches: list[RedactionMatch] = []

        for path in sorted(snapshot_path.rglob("*")):
            if not path.is_file():
                continue
            if path.name == "snapshot.json":
                continue
            rel = str(path.relative_to(snapshot_path))

            # If stripping journal, mark all journal files
            if self.rules.strip_journal and rel.startswith("journal/"):
                matches.append(RedactionMatch(
                    file=rel,
                    line_number=0,
                    rule_name="strip_journal",
                    original="[entire file]",
                    replacement="[REMOVED]",
                ))
                continue

            # Skip binary/non-text files
            if path.suffix in (".json",):
                if rel.startswith("metrics/"):
                    continue  # Keep metrics as-is

            matches.extend(self._scan_file(path, rel))

        return matches

    def sanitise_and_export(
        self,
        snapshot_path: Path,
        output_path: Path,
    ) -> Path:
        """Create a sanitised copy of a snapshot and export as tar.gz.

        Returns the path to the created archive.
        """
        import tarfile
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / snapshot_path.name
            shutil.copytree(snapshot_path, work_dir)

            # Remove journal if configured
            if self.rules.strip_journal:
                journal_dir = work_dir / "journal"
                if journal_dir.is_dir():
                    shutil.rmtree(journal_dir)

            # Apply rules to text files
            for path in sorted(work_dir.rglob("*")):
                if not path.is_file():
                    continue
                if path.name == "snapshot.json":
                    continue
                rel = str(path.relative_to(work_dir))
                if rel.startswith("metrics/"):
                    continue

                try:
                    content = path.read_text(encoding="utf-8")
                    sanitised = self._apply_rules(content)
                    if sanitised != content:
                        path.write_text(sanitised, encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    pass

            # Export as tar.gz
            output_path.parent.mkdir(parents=True, exist_ok=True)
            archive = output_path if str(output_path).endswith(".tar.gz") else output_path.with_suffix(".tar.gz")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(work_dir, arcname=snapshot_path.name)

        return archive

    def sanitise_in_place(self, snapshot_path: Path) -> int:
        """Apply sanitisation directly to a snapshot directory.

        Returns the number of files modified.
        """
        modified = 0

        if self.rules.strip_journal:
            journal_dir = snapshot_path / "journal"
            if journal_dir.is_dir():
                shutil.rmtree(journal_dir)
                modified += 1

        for path in sorted(snapshot_path.rglob("*")):
            if not path.is_file():
                continue
            if path.name == "snapshot.json":
                continue
            rel = str(path.relative_to(snapshot_path))
            if rel.startswith("metrics/"):
                continue

            try:
                content = path.read_text(encoding="utf-8")
                sanitised = self._apply_rules(content)
                if sanitised != content:
                    path.write_text(sanitised, encoding="utf-8")
                    modified += 1
            except (UnicodeDecodeError, OSError):
                pass

        return modified
