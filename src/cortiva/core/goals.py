"""
OKR (Objectives and Key Results) system for org-level goal tracking.

Integrates with the org model's departments and agent ownership to provide
structured goal setting, progress tracking, and LLM context injection.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class KeyResult:
    """A measurable outcome contributing to an objective."""

    id: str
    description: str
    target_value: float
    current_value: float = 0.0
    unit: str = ""
    agent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "target_value": self.target_value,
            "current_value": self.current_value,
            "unit": self.unit,
            "agent_id": self.agent_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeyResult:
        return cls(
            id=data["id"],
            description=data["description"],
            target_value=data["target_value"],
            current_value=data.get("current_value", 0.0),
            unit=data.get("unit", ""),
            agent_id=data.get("agent_id"),
        )


@dataclass
class Objective:
    """An org-level objective with associated key results."""

    id: str
    title: str
    description: str
    key_results: list[KeyResult] = field(default_factory=list)
    department: str | None = None
    owner: str = ""
    quarter: str = ""
    status: str = "active"  # active | completed | cancelled

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "key_results": [kr.to_dict() for kr in self.key_results],
            "department": self.department,
            "owner": self.owner,
            "quarter": self.quarter,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Objective:
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            key_results=[KeyResult.from_dict(kr) for kr in data.get("key_results", [])],
            department=data.get("department"),
            owner=data.get("owner", ""),
            quarter=data.get("quarter", ""),
            status=data.get("status", "active"),
        )


# ---------------------------------------------------------------------------
# GoalManager
# ---------------------------------------------------------------------------


class GoalManager:
    """Manages OKR objectives with JSON persistence.

    Data is stored in ``{data_dir}/objectives.json``.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._objectives: dict[str, Objective] = {}
        self._load()

    # -- public API ----------------------------------------------------------

    def create_objective(
        self,
        title: str,
        description: str,
        key_results: list[KeyResult],
        owner: str,
        department: str | None = None,
        quarter: str = "",
    ) -> Objective:
        """Create and persist a new objective."""
        obj = Objective(
            id=uuid.uuid4().hex[:12],
            title=title,
            description=description,
            key_results=key_results,
            department=department,
            owner=owner,
            quarter=quarter,
        )
        self._objectives[obj.id] = obj
        self._persist()
        return obj

    def update_key_result(
        self,
        objective_id: str,
        kr_id: str,
        current_value: float,
    ) -> None:
        """Update progress on a specific key result."""
        obj = self._objectives.get(objective_id)
        if obj is None:
            raise KeyError(f"Objective {objective_id!r} not found")
        for kr in obj.key_results:
            if kr.id == kr_id:
                kr.current_value = current_value
                self._persist()
                return
        raise KeyError(f"KeyResult {kr_id!r} not found in objective {objective_id!r}")

    def get_objectives(
        self,
        quarter: str | None = None,
        department: str | None = None,
        agent_id: str | None = None,
    ) -> list[Objective]:
        """Filter objectives by quarter, department, and/or agent ownership."""
        results: list[Objective] = []
        for obj in self._objectives.values():
            if quarter is not None and obj.quarter != quarter:
                continue
            if department is not None and obj.department != department:
                continue
            if agent_id is not None:
                owner_match = obj.owner == agent_id
                kr_match = any(kr.agent_id == agent_id for kr in obj.key_results)
                if not owner_match and not kr_match:
                    continue
            results.append(obj)
        return results

    def progress(self, objective_id: str) -> float:
        """Compute objective progress as average KR completion (0.0-1.0)."""
        obj = self._objectives.get(objective_id)
        if obj is None:
            raise KeyError(f"Objective {objective_id!r} not found")
        if not obj.key_results:
            return 0.0
        total = 0.0
        for kr in obj.key_results:
            if kr.target_value == 0.0:
                ratio = 1.0 if kr.current_value >= 0.0 else 0.0
            else:
                ratio = kr.current_value / kr.target_value
            total += min(ratio, 1.0)
        return total / len(obj.key_results)

    _STATUS_ICONS = {"active": "🔄", "completed": "✅", "cancelled": "❌"}

    def agent_goals_context(self, agent_id: str) -> str:
        """Render markdown context for LLM injection showing the agent's goals."""
        objectives = self.get_objectives(agent_id=agent_id)
        if not objectives:
            return ""
        lines: list[str] = ["## My OKR Goals", ""]
        for obj in objectives:
            prog = self.progress(obj.id)
            status_icon = self._STATUS_ICONS.get(obj.status, "❓")
            lines.append(
                f"### {status_icon} {obj.title} ({obj.quarter}) — {prog:.0%}"
            )
            lines.append(f"_{obj.description}_")
            lines.append("")
            for kr in obj.key_results:
                owner_tag = f" (@{kr.agent_id})" if kr.agent_id else ""
                lines.append(
                    f"- [ ] {kr.description}: "
                    f"{kr.current_value}/{kr.target_value} {kr.unit}{owner_tag}"
                )
            lines.append("")
        return "\n".join(lines)

    # -- persistence ---------------------------------------------------------

    def _persist(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / "objectives.json"
        data = [obj.to_dict() for obj in self._objectives.values()]
        path.write_text(json.dumps(data, indent=2))

    def _load(self) -> None:
        path = self._data_dir / "objectives.json"
        if not path.exists():
            return
        data = json.loads(path.read_text())
        for entry in data:
            obj = Objective.from_dict(entry)
            self._objectives[obj.id] = obj
