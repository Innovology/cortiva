"""
Cortiva Agent — a persistent identity with cognitive state.

An agent is not a function that runs and disposes. It's an entity that
sleeps, wakes, plans, works, reflects, and learns. Identity persists
across sleep cycles via markdown files on disk.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


@dataclass
class Task:
    """A single unit of work in an agent's plan."""
    id: str
    description: str
    status: str = "pending"   # pending | in_progress | done | skipped | exception
    priority: int = 0         # 0=normal, 1=high, 2=critical
    outcome: str = ""
    error: str = ""


@dataclass
class TaskQueue:
    """Ordered queue of tasks with exception tracking."""
    tasks: list[Task] = field(default_factory=list)
    exceptions: list[Task] = field(default_factory=list)
    replan_count: int = 0

    def next_pending(self) -> Task | None:
        """Return highest-priority pending task, or None."""
        pending = [t for t in self.tasks if t.status == "pending"]
        if not pending:
            return None
        pending.sort(key=lambda t: t.priority, reverse=True)
        return pending[0]

    def all_done(self) -> bool:
        """True when no tasks are pending or in_progress."""
        return all(t.status not in ("pending", "in_progress") for t in self.tasks)

    def completion_summary(self) -> dict[str, int]:
        """Count tasks by status."""
        summary: dict[str, int] = {}
        for t in self.tasks:
            summary[t.status] = summary.get(t.status, 0) + 1
        summary["exceptions"] = len(self.exceptions)
        return summary


class AgentState(Enum):
    """Lifecycle states. Agents don't start/stop — they sleep/wake."""
    ONBOARDING = "onboarding"    # First-time setup, no experience yet
    SLEEPING = "sleeping"        # Idle, identity persists on disk
    WAKING = "waking"            # Loading identity, checking queue
    PLANNING = "planning"        # Building today's plan (conscious)
    EXECUTING = "executing"      # Working through plan
    REPLANNING = "replanning"    # Adjusting plan mid-cycle (conscious)
    REFLECTING = "reflecting"    # End-of-day review (conscious)


# Standard identity file names (subdirectory layout)
IDENTITY_FILES = {
    "identity": "identity/identity.md",           # Living Summary
    "soul": "identity/soul.md",                   # Persona parameters
    "skills": "identity/skills.md",               # Domain knowledge
    "responsibilities": "identity/responsibilities.md",  # R&R authority
    "procedures": "identity/procedures.md",       # Promoted procedural knowledge
    "plan": "today/plan.md",                      # Current plan
}

# Standard workspace subdirectories
WORKSPACE_DIRS = ["identity", "today", "outbox", "journal", "workspace"]


@dataclass
class Agent:
    """
    A Cortiva agent. Represents a persistent identity with state.

    The agent's 'self' is the collection of markdown files in its
    directory. These are human-readable, agent-editable, and form
    the context package that the conscious layer reads.
    """

    id: str
    directory: Path
    state: AgentState = AgentState.SLEEPING
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_wake: datetime | None = None
    last_sleep: datetime | None = None

    # Runtime state (not persisted, rebuilt on wake)
    consciousness_budget_used: int = 0
    consciousness_budget_limit: int = 50
    tasks_completed_today: int = 0
    tasks_escalated_today: int = 0
    task_queue: TaskQueue | None = None

    # ----- Identity file access -----

    def identity_path(self, file_key: str) -> Path:
        """Get path to an identity file."""
        if file_key not in IDENTITY_FILES:
            raise ValueError(f"Unknown identity file: {file_key}")
        return self.directory / IDENTITY_FILES[file_key]

    def journal_path(self, date: datetime | None = None) -> Path:
        """Get path to journal entry for a date."""
        d = date or datetime.utcnow()
        journal_dir = self.directory / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        return journal_dir / f"{d.strftime('%Y-%m-%d')}.md"

    def read_identity(self, file_key: str) -> str:
        """Read an identity file. Returns empty string if missing."""
        path = self.identity_path(file_key)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def write_identity(self, file_key: str, content: str) -> None:
        """Write an identity file."""
        path = self.identity_path(file_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def read_all_identity(self) -> dict[str, str]:
        """Read all identity files into a dict."""
        return {key: self.read_identity(key) for key in IDENTITY_FILES}

    # ----- Workspace helpers -----

    def ensure_workspace(self) -> None:
        """Create all standard workspace subdirectories."""
        for subdir in WORKSPACE_DIRS:
            (self.directory / subdir).mkdir(parents=True, exist_ok=True)

    def migrate_flat_layout(self) -> bool:
        """Move flat identity files into subdirectories.

        Detects flat layout by checking if ``identity.md`` exists at the
        top level while ``identity/identity.md`` does not.  Returns True
        if migration was performed.
        """
        flat_identity = self.directory / "identity.md"
        nested_identity = self.directory / "identity" / "identity.md"
        if not flat_identity.exists() or nested_identity.exists():
            return False

        self.ensure_workspace()

        # Map flat filenames → subdirectory paths
        migrations: dict[str, str] = {
            "identity.md": "identity/identity.md",
            "soul.md": "identity/soul.md",
            "skills.md": "identity/skills.md",
            "responsibilities.md": "identity/responsibilities.md",
            "procedures.md": "identity/procedures.md",
            "plan.md": "today/plan.md",
        }
        for flat_name, nested_name in migrations.items():
            src = self.directory / flat_name
            if src.exists():
                shutil.move(str(src), str(self.directory / nested_name))

        return True

    @staticmethod
    def _validate_filename(filename: str) -> str:
        """Reject filenames containing path separators or traversal.

        Raises :class:`ValueError` if *filename* is unsafe.
        """
        if ".." in filename or "/" in filename or "\\" in filename:
            raise ValueError(
                f"Unsafe filename rejected (contains path separator or '..'): {filename!r}"
            )
        if not filename or filename in (".", ".."):
            raise ValueError(f"Invalid filename: {filename!r}")
        return filename

    def today_path(self, filename: str) -> Path:
        """Return path to a file in the today/ subdirectory."""
        self._validate_filename(filename)
        return self.directory / "today" / filename

    def outbox_path(self, filename: str) -> Path:
        """Return path to a file in the outbox/ subdirectory."""
        self._validate_filename(filename)
        return self.directory / "outbox" / filename

    def workspace_path(self, filename: str) -> Path:
        """Return path to a file in the workspace/ subdirectory."""
        self._validate_filename(filename)
        return self.directory / "workspace" / filename

    def read_today(self, filename: str) -> str:
        """Read a file from the today/ subdirectory. Returns empty string if missing."""
        path = self.today_path(filename)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def write_today(self, filename: str, content: str) -> None:
        """Write a file to the today/ subdirectory."""
        path = self.today_path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def read_outbox(self, filename: str) -> str:
        """Read a file from the outbox/ subdirectory. Returns empty string if missing."""
        path = self.outbox_path(filename)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def write_outbox(self, filename: str, content: str) -> None:
        """Write a file to the outbox/ subdirectory."""
        path = self.outbox_path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def flush_outbox(self) -> dict[str, str]:
        """Read all files in outbox/, return as {filename: content}, then delete them."""
        outbox_dir = self.directory / "outbox"
        if not outbox_dir.is_dir():
            return {}
        result: dict[str, str] = {}
        for path in sorted(outbox_dir.iterdir()):
            if path.is_file():
                result[path.name] = path.read_text(encoding="utf-8")
                path.unlink()
        return result

    # ----- Runtime state persistence -----

    def persist_runtime_state(self) -> None:
        """Serialize in-memory runtime state to today/ as JSON files.

        Writes task_queue.json and exception_pile.json so the portal
        and other tools can read agent metrics without the fabric running.
        """
        if self.task_queue is None:
            return

        # task_queue.json
        tq_data = {
            "tasks": [
                {
                    "id": t.id,
                    "description": t.description,
                    "status": t.status,
                    "priority": t.priority,
                    "outcome": t.outcome,
                    "error": t.error,
                }
                for t in self.task_queue.tasks
            ],
            "replan_count": self.task_queue.replan_count,
            "summary": self.task_queue.completion_summary(),
        }
        self.write_today("task_queue.json", json.dumps(tq_data, indent=2))

        # exception_pile.json
        exc_data = [
            {
                "id": t.id,
                "description": t.description,
                "error": t.error,
            }
            for t in self.task_queue.exceptions
        ]
        self.write_today("exception_pile.json", json.dumps(exc_data, indent=2))

    def persist_familiarity(self, signals: list[dict[str, Any]]) -> None:
        """Write accumulated familiarity signals to today/familiarity_signals.json."""
        self.write_today("familiarity_signals.json", json.dumps(signals, indent=2))

    def reset_today(self) -> None:
        """Clear the today/ directory for a new day cycle."""
        today_dir = self.directory / "today"
        if today_dir.is_dir():
            for path in today_dir.iterdir():
                if path.is_file():
                    path.unlink()

    # ----- State transitions -----

    def can_transition(self, target: AgentState) -> bool:
        """Check if a state transition is valid."""
        valid = {
            AgentState.ONBOARDING: {AgentState.SLEEPING},
            AgentState.SLEEPING: {AgentState.WAKING},
            AgentState.WAKING: {AgentState.PLANNING, AgentState.SLEEPING},
            AgentState.PLANNING: {AgentState.EXECUTING, AgentState.SLEEPING},
            AgentState.EXECUTING: {
                AgentState.REPLANNING, AgentState.REFLECTING, AgentState.SLEEPING,
            },
            AgentState.REPLANNING: {
                AgentState.EXECUTING, AgentState.REFLECTING, AgentState.SLEEPING,
            },
            AgentState.REFLECTING: {AgentState.SLEEPING},
        }
        return target in valid.get(self.state, set())

    def transition(self, target: AgentState) -> None:
        """Transition to a new state."""
        if not self.can_transition(target):
            raise ValueError(
                f"Invalid transition: {self.state.value} → {target.value}"
            )
        if target == AgentState.WAKING:
            self.last_wake = datetime.utcnow()
            self.consciousness_budget_used = 0
            self.tasks_completed_today = 0
            self.tasks_escalated_today = 0
        elif target == AgentState.SLEEPING:
            self.last_sleep = datetime.utcnow()
        self.state = target

    # ----- Consciousness budget -----

    @property
    def consciousness_remaining(self) -> int:
        return max(0, self.consciousness_budget_limit - self.consciousness_budget_used)

    def spend_consciousness(self, amount: int = 1) -> bool:
        """Spend from consciousness budget. Returns False if over budget."""
        if self.consciousness_budget_used + amount > self.consciousness_budget_limit:
            return False
        self.consciousness_budget_used += amount
        return True

    # ----- Serialisation -----

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "directory": str(self.directory),
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "last_wake": self.last_wake.isoformat() if self.last_wake else None,
            "last_sleep": self.last_sleep.isoformat() if self.last_sleep else None,
            "consciousness_budget_limit": self.consciousness_budget_limit,
        }

    @classmethod
    def from_directory(cls, directory: Path) -> Agent:
        """Load an agent from its directory."""
        agent_id = directory.name
        return cls(id=agent_id, directory=directory)
