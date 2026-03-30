"""Tests for the skill system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortiva.core.skills import (
    MCPConfig,
    Skill,
    SkillRegistry,
    install_skill,
    installed_skills,
    parse_skill,
    uninstall_skill,
)


class TestSkillParsing:
    def test_parse_minimal(self) -> None:
        skill = parse_skill({"name": "test-skill"})
        assert skill.name == "test-skill"
        assert skill.mcp is None

    def test_parse_full(self) -> None:
        skill = parse_skill({
            "name": "linear",
            "description": "Linear integration",
            "category": "project-management",
            "version": "2.0",
            "tags": ["issues", "tickets"],
            "mcp": {
                "package": "@linear/mcp-server",
                "command": "npx @linear/mcp-server",
                "env": ["LINEAR_API_KEY"],
            },
            "procedures": "## Linear\nUse Linear tools.",
        })
        assert skill.name == "linear"
        assert skill.category == "project-management"
        assert skill.mcp is not None
        assert skill.mcp.package == "@linear/mcp-server"
        assert "LINEAR_API_KEY" in skill.mcp.env
        assert "Linear" in skill.procedures

    def test_to_dict(self) -> None:
        skill = Skill(
            name="test",
            description="A test",
            category="testing",
            mcp=MCPConfig(command="npx test"),
        )
        d = skill.to_dict()
        assert d["name"] == "test"
        assert d["mcp"]["command"] == "npx test"


class TestSkillInstall:
    def _make_agent(self, tmp_path: Path) -> Path:
        agent_dir = tmp_path / "agent-1"
        (agent_dir / "identity").mkdir(parents=True)
        (agent_dir / "identity" / "procedures.md").write_text("# Procedures\n")
        (agent_dir / "identity" / "skills.md").write_text("# Skills\n")
        return agent_dir

    def test_install_basic(self, tmp_path: Path) -> None:
        agent_dir = self._make_agent(tmp_path)
        skill = Skill(
            name="test-skill",
            procedures="## Test\nDo the thing.",
            skills_text="## Test Knowledge\nContext.",
        )
        modified = install_skill(agent_dir, skill)
        assert "identity/procedures.md" in modified
        assert "identity/skills.md" in modified
        assert "identity/skills_manifest.json" in modified

        procs = (agent_dir / "identity" / "procedures.md").read_text()
        assert "Do the thing" in procs
        assert "skill:test-skill" in procs

    def test_install_with_mcp(self, tmp_path: Path) -> None:
        agent_dir = self._make_agent(tmp_path)
        skill = Skill(
            name="linear",
            mcp=MCPConfig(
                command="npx @linear/mcp-server",
                env=["LINEAR_API_KEY"],
            ),
        )
        install_skill(agent_dir, skill)
        mcp_path = agent_dir / "identity" / "mcp" / "linear.json"
        assert mcp_path.exists()
        mcp_data = json.loads(mcp_path.read_text())
        assert mcp_data["command"] == "npx @linear/mcp-server"

    def test_install_duplicate_raises(self, tmp_path: Path) -> None:
        agent_dir = self._make_agent(tmp_path)
        skill = Skill(name="test-skill", procedures="test")
        install_skill(agent_dir, skill)
        with pytest.raises(ValueError, match="already installed"):
            install_skill(agent_dir, skill)

    def test_installed_skills(self, tmp_path: Path) -> None:
        agent_dir = self._make_agent(tmp_path)
        assert installed_skills(agent_dir) == []

        install_skill(agent_dir, Skill(name="skill-a"))
        install_skill(agent_dir, Skill(name="skill-b"))
        assert installed_skills(agent_dir) == ["skill-a", "skill-b"]

    def test_uninstall(self, tmp_path: Path) -> None:
        agent_dir = self._make_agent(tmp_path)
        skill = Skill(
            name="test-skill",
            procedures="## Test\nDo the thing.",
            mcp=MCPConfig(command="npx test"),
        )
        install_skill(agent_dir, skill)
        assert "test-skill" in installed_skills(agent_dir)

        modified = uninstall_skill(agent_dir, "test-skill")
        assert "identity/procedures.md" in modified
        assert "test-skill" not in installed_skills(agent_dir)

        procs = (agent_dir / "identity" / "procedures.md").read_text()
        assert "Do the thing" not in procs

    def test_uninstall_nonexistent_raises(self, tmp_path: Path) -> None:
        agent_dir = self._make_agent(tmp_path)
        with pytest.raises(ValueError, match="not installed"):
            uninstall_skill(agent_dir, "nonexistent")


class TestSkillRegistry:
    def test_load_bundled(self) -> None:
        registry = SkillRegistry()
        count = registry.load_bundled()
        assert count > 100  # we have 100+ skills in the bundled registry

    def test_get(self) -> None:
        registry = SkillRegistry()
        registry.load_bundled()
        skill = registry.get("github")
        assert skill is not None
        assert skill.category == "version-control"

    def test_search(self) -> None:
        registry = SkillRegistry()
        registry.load_bundled()
        results = registry.search("database")
        assert len(results) > 0

    def test_search_by_category(self) -> None:
        registry = SkillRegistry()
        registry.load_bundled()
        results = registry.search(category="databases")
        assert len(results) > 5

    def test_categories(self) -> None:
        registry = SkillRegistry()
        registry.load_bundled()
        cats = registry.categories()
        assert "databases" in cats
        assert "project-management" in cats
        assert "version-control" in cats

    def test_get_nonexistent(self) -> None:
        registry = SkillRegistry()
        registry.load_bundled()
        assert registry.get("nonexistent-skill-xyz") is None
