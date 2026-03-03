"""Tests for the template system."""

import json
from pathlib import Path

import pytest

from cortiva.templates import apply_template, get_template_path, list_templates

# The six standard identity files every agent needs.
STANDARD_FILES = {
    "identity.md",
    "soul.md",
    "skills.md",
    "responsibilities.md",
    "procedures.md",
    "plan.md",
}

EXPECTED_TEMPLATES = {"dev-cortiva", "qa-cortiva", "pm-cortiva"}


class TestListTemplates:
    def test_lists_all_bundled_templates(self) -> None:
        templates = list_templates()
        assert set(templates) == EXPECTED_TEMPLATES

    def test_returns_sorted(self) -> None:
        templates = list_templates()
        assert templates == sorted(templates)


class TestGetTemplatePath:
    def test_returns_path_for_known_template(self) -> None:
        for name in EXPECTED_TEMPLATES:
            path = get_template_path(name)
            assert path.is_dir()

    def test_raises_for_unknown_template(self) -> None:
        with pytest.raises(KeyError, match="Unknown template"):
            get_template_path("nonexistent-template")


class TestApplyTemplate:
    def test_copies_standard_files(self, tmp_path: Path) -> None:
        target = tmp_path / "my-agent"
        written = apply_template("dev-cortiva", target)
        assert STANDARD_FILES.issubset(set(written))
        for f in STANDARD_FILES:
            assert (target / f).exists()
            assert (target / f).read_text().strip() != ""

    def test_creates_journal_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "my-agent"
        apply_template("qa-cortiva", target)
        assert (target / "journal").is_dir()

    def test_all_templates_have_standard_files(self) -> None:
        """Every template must ship the six standard identity files."""
        for name in EXPECTED_TEMPLATES:
            path = get_template_path(name)
            present = {f.name for f in path.iterdir() if not f.name.startswith("_")}
            missing = STANDARD_FILES - present
            assert not missing, f"Template {name!r} is missing: {missing}"

    def test_pm_backlog_json_is_valid(self) -> None:
        """PM template must include a parseable backlog.json."""
        path = get_template_path("pm-cortiva")
        backlog_path = path / "backlog.json"
        assert backlog_path.exists(), "pm-cortiva missing backlog.json"
        data = json.loads(backlog_path.read_text())
        assert "items" in data
        assert isinstance(data["items"], list)
        assert len(data["items"]) > 0
        # Each item must have at least id, title, status
        for item in data["items"]:
            assert "id" in item
            assert "title" in item
            assert "status" in item

    def test_apply_pm_includes_backlog(self, tmp_path: Path) -> None:
        target = tmp_path / "pm-agent"
        written = apply_template("pm-cortiva", target)
        assert "backlog.json" in written
        assert (target / "backlog.json").exists()
