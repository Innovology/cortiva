"""
Skill system — composable capability packages for agents.

A skill is a directory containing markdown instructions, policy
overrides, and optional MCP server configuration.  Installing a
skill merges its content into an agent's identity files.

Skills come in two flavours:

- **Curated skills**: Full procedure/skills markdown with detailed
  instructions.  Bundled with Cortiva or hosted in a registry.
- **MCP skills**: Thin wrappers around MCP servers.  The MCP server
  provides the tool; the skill provides procedures telling the agent
  how to use it.

Registry format (``registry.yaml``)::

    skills:
      - name: linear
        description: Create and manage Linear issues
        category: project-management
        version: "1.0"
        mcp:
          package: "@linear/mcp-server"
          command: "npx @linear/mcp-server"
          env: ["LINEAR_API_KEY"]
        procedures: |
          ## Linear Integration
          Use the Linear MCP tools to manage issues...
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("cortiva.skills")

# Delimiter inserted into agent files to mark skill-managed sections
_SKILL_START = "\n\n<!-- skill:{name} -->\n"
_SKILL_END = "\n<!-- /skill:{name} -->\n"


@dataclass
class MCPConfig:
    """MCP server configuration for a skill."""

    package: str = ""
    """Package name (npm or pip)."""

    command: str = ""
    """Command to start the MCP server."""

    env: list[str] = field(default_factory=list)
    """Environment variables required by the server."""

    args: list[str] = field(default_factory=list)
    """Additional arguments for the server command."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "command": self.command,
            "env": self.env,
            "args": self.args,
        }


@dataclass
class Skill:
    """A composable capability package."""

    name: str
    description: str = ""
    category: str = ""
    version: str = "1.0"
    author: str = ""

    procedures: str = ""
    """Markdown procedures to append to the agent's procedures.md."""

    skills_text: str = ""
    """Markdown domain knowledge to append to the agent's skills.md."""

    tools_allowed: list[str] = field(default_factory=list)
    """Tools this skill requires (merged into agent policy)."""

    mcp: MCPConfig | None = None
    """Optional MCP server configuration."""

    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "version": self.version,
        }
        if self.author:
            d["author"] = self.author
        if self.mcp:
            d["mcp"] = self.mcp.to_dict()
        if self.tags:
            d["tags"] = self.tags
        if self.tools_allowed:
            d["tools_allowed"] = self.tools_allowed
        return d


def parse_skill(data: dict[str, Any]) -> Skill:
    """Parse a skill from a registry entry or skill.yaml."""
    mcp = None
    mcp_data = data.get("mcp")
    if mcp_data and isinstance(mcp_data, dict):
        mcp = MCPConfig(
            package=mcp_data.get("package", ""),
            command=mcp_data.get("command", ""),
            env=mcp_data.get("env", []),
            args=mcp_data.get("args", []),
        )

    return Skill(
        name=data.get("name", ""),
        description=data.get("description", ""),
        category=data.get("category", ""),
        version=str(data.get("version", "1.0")),
        author=data.get("author", ""),
        procedures=data.get("procedures", ""),
        skills_text=data.get("skills_text", ""),
        tools_allowed=data.get("tools_allowed", []),
        mcp=mcp,
        tags=data.get("tags", []),
    )


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def installed_skills(agent_dir: Path) -> list[str]:
    """List skills installed for an agent."""
    manifest_path = agent_dir / "identity" / "skills_manifest.json"
    if not manifest_path.exists():
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return [s["name"] for s in data.get("installed", [])]
    except (json.JSONDecodeError, KeyError):
        return []


def _read_manifest(agent_dir: Path) -> dict[str, Any]:
    manifest_path = agent_dir / "identity" / "skills_manifest.json"
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return {"installed": []}


def _write_manifest(agent_dir: Path, manifest: dict[str, Any]) -> None:
    manifest_path = agent_dir / "identity" / "skills_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def install_skill(agent_dir: Path, skill: Skill) -> list[str]:
    """Install a skill into an agent's identity directory.

    Returns a list of files modified.
    """
    already = installed_skills(agent_dir)
    if skill.name in already:
        raise ValueError(f"Skill {skill.name!r} is already installed")

    modified: list[str] = []

    # Append procedures
    if skill.procedures:
        _append_to_identity_file(
            agent_dir, "procedures", skill.name, skill.procedures,
        )
        modified.append("identity/procedures.md")

    # Append skills knowledge
    if skill.skills_text:
        _append_to_identity_file(
            agent_dir, "skills", skill.name, skill.skills_text,
        )
        modified.append("identity/skills.md")

    # Write MCP config
    if skill.mcp and skill.mcp.command:
        mcp_dir = agent_dir / "identity" / "mcp"
        mcp_dir.mkdir(parents=True, exist_ok=True)
        mcp_config = {
            "name": skill.name,
            "command": skill.mcp.command,
            "args": skill.mcp.args,
            "env": skill.mcp.env,
        }
        mcp_path = mcp_dir / f"{skill.name}.json"
        mcp_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")
        modified.append(f"identity/mcp/{skill.name}.json")

    # Update manifest
    manifest = _read_manifest(agent_dir)
    manifest["installed"].append(skill.to_dict())
    _write_manifest(agent_dir, manifest)
    modified.append("identity/skills_manifest.json")

    logger.info("Installed skill %s for agent %s", skill.name, agent_dir.name)
    return modified


def uninstall_skill(agent_dir: Path, skill_name: str) -> list[str]:
    """Remove a skill from an agent's identity directory.

    Returns a list of files modified.
    """
    already = installed_skills(agent_dir)
    if skill_name not in already:
        raise ValueError(f"Skill {skill_name!r} is not installed")

    modified: list[str] = []

    # Remove from procedures
    if _remove_from_identity_file(agent_dir, "procedures", skill_name):
        modified.append("identity/procedures.md")

    # Remove from skills
    if _remove_from_identity_file(agent_dir, "skills", skill_name):
        modified.append("identity/skills.md")

    # Remove MCP config
    mcp_path = agent_dir / "identity" / "mcp" / f"{skill_name}.json"
    if mcp_path.exists():
        mcp_path.unlink()
        modified.append(f"identity/mcp/{skill_name}.json")

    # Update manifest
    manifest = _read_manifest(agent_dir)
    manifest["installed"] = [
        s for s in manifest["installed"] if s.get("name") != skill_name
    ]
    _write_manifest(agent_dir, manifest)
    modified.append("identity/skills_manifest.json")

    logger.info("Uninstalled skill %s from agent %s", skill_name, agent_dir.name)
    return modified


def _append_to_identity_file(
    agent_dir: Path, file_key: str, skill_name: str, content: str,
) -> None:
    """Append skill content to an identity file with markers."""
    from cortiva.core.agent import IDENTITY_FILES

    path = agent_dir / IDENTITY_FILES[file_key]
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    start_marker = _SKILL_START.format(name=skill_name)
    end_marker = _SKILL_END.format(name=skill_name)

    new_content = existing.rstrip() + start_marker + content + end_marker
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")


def _remove_from_identity_file(
    agent_dir: Path, file_key: str, skill_name: str,
) -> bool:
    """Remove skill-marked content from an identity file."""
    from cortiva.core.agent import IDENTITY_FILES

    path = agent_dir / IDENTITY_FILES[file_key]
    if not path.exists():
        return False

    content = path.read_text(encoding="utf-8")
    start_marker = _SKILL_START.format(name=skill_name)
    end_marker = _SKILL_END.format(name=skill_name)

    if start_marker not in content:
        return False

    start_idx = content.index(start_marker)
    end_idx = content.index(end_marker) + len(end_marker)
    new_content = content[:start_idx] + content[end_idx:]
    path.write_text(new_content, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """Loads and searches the skill registry.

    The registry is a YAML file with a ``skills`` list.  Multiple
    registries can be merged (bundled + community + custom).
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    @property
    def count(self) -> int:
        return len(self._skills)

    def load_file(self, path: Path) -> int:
        """Load skills from a registry YAML file.  Returns count loaded."""
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not data or "skills" not in data:
            return 0
        count = 0
        for entry in data["skills"]:
            if isinstance(entry, dict) and "name" in entry:
                skill = parse_skill(entry)
                self._skills[skill.name] = skill
                count += 1
        return count

    def load_bundled(self) -> int:
        """Load the bundled registry shipped with Cortiva."""
        registry_path = Path(__file__).parent.parent / "skills" / "registry.yaml"
        if registry_path.exists():
            return self.load_file(registry_path)
        return 0

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def search(
        self,
        query: str = "",
        category: str = "",
    ) -> list[Skill]:
        """Search for skills by name, description, or category."""
        results = []
        query_lower = query.lower()
        for skill in self._skills.values():
            if category and skill.category != category:
                continue
            if query_lower:
                searchable = f"{skill.name} {skill.description} {' '.join(skill.tags)}".lower()
                if query_lower not in searchable:
                    continue
            results.append(skill)
        return sorted(results, key=lambda s: s.name)

    def categories(self) -> dict[str, int]:
        """Return category → count mapping."""
        cats: dict[str, int] = {}
        for skill in self._skills.values():
            cat = skill.category or "uncategorized"
            cats[cat] = cats.get(cat, 0) + 1
        return dict(sorted(cats.items()))

    def all_skills(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: s.name)
