"""
Cortiva Agent — a persistent identity with cognitive state.

An agent is not a function that runs and disposes. It's an entity that
sleeps, wakes, plans, works, reflects, and learns. Identity persists
across sleep cycles via markdown files on disk.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any


@dataclass
class Task:
    """A single unit of work in an agent's plan.

    A task may hold ``subtasks``. A parent is NOT complete until every subtask
    is — 'acknowledged' (read it, started on it) is a real, distinct state, but
    it is NOT 'done'. This is the fix for marking intent as completion (ticking
    "reply to the founder" done before the reply is actually sent).
    """

    id: str
    description: str = ""
    # pending | acknowledged | in_progress | done | skipped | exception
    #   acknowledged = read & owned, work not finished (NOT done)
    status: str = "pending"
    priority: int = 0  # 0=normal, 1=high, 2=critical
    outcome: str = ""
    error: str = ""
    subtasks: list[Task] = field(default_factory=list)

    def can_complete(self) -> bool:
        """A task may only be marked done once every subtask is resolved
        (done or skipped). Open subtasks block the parent."""
        return all(st.status in ("done", "skipped") for st in self.subtasks)

    def is_done(self) -> bool:
        """Truly complete: marked done AND no subtask left open."""
        return self.status == "done" and self.can_complete()


@dataclass
class TaskQueue:
    """Ordered queue of tasks with exception tracking."""

    tasks: list[Task] = field(default_factory=list)
    exceptions: list[Task] = field(default_factory=list)
    replan_count: int = 0

    def next_pending(self) -> Task | None:
        """Highest-priority pending WORK unit. A parent that has subtasks is
        not worked directly (its work IS its subtasks) — so workable = pending
        leaf tasks (top-level tasks with no subtasks) + pending subtasks."""
        workable: list[Task] = []
        for t in self.tasks:
            if t.subtasks:
                workable.extend(st for st in t.subtasks if st.status == "pending")
            elif t.status == "pending":
                workable.append(t)
        if not workable:
            return None
        workable.sort(key=lambda t: t.priority, reverse=True)
        return workable[0]

    def all_done(self) -> bool:
        """True when no unit (task OR subtask) is still open. A parent marked
        'done' with open subtasks does NOT count — its leaves gate it."""

        def _open(t: Task) -> bool:
            if t.status in ("pending", "in_progress", "acknowledged"):
                return True
            return any(_open(st) for st in t.subtasks)

        return not any(_open(t) for t in self.tasks)

    def completion_summary(self) -> dict[str, int]:
        """Count tasks by status."""
        summary: dict[str, int] = {}
        for t in self.tasks:
            summary[t.status] = summary.get(t.status, 0) + 1
        summary["exceptions"] = len(self.exceptions)
        return summary


def _normalise_intent(description: str) -> str:
    """Canonical intent fingerprint for planner seed-time dedupe.

    Strips priority markers, markdown, trailing dates/IDs/SHAs, lowercases,
    and returns the first 12 content words.  Two tasks phrasing the same
    work usually collide; two phrasing different work rarely do.
    """
    d = description or ""
    d = re.sub(r"\*\*\[\w+\]\*\*\s*", "", d)          # **[CRITICAL]** etc.
    d = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", d)     # [text](url) → text
    # Strip specific ref patterns BEFORE the general # → space substitution so
    # "#364" becomes " " rather than "364" (which the later word-filter misses).
    d = re.sub(r"#\d+", " ", d)                        # #123 PR/issue refs
    d = re.sub(r"[`*_~#]", " ", d)                     # remaining markdown punctuation
    d = d.lower()
    d = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", d)      # dates 2026-11-17
    d = re.sub(                                         # UUIDs
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        " ",
        d,
    )
    d = re.sub(r"\btask-\d+\b", " ", d)                # task-NNN IDs
    d = re.sub(r"\b[0-9a-f]{7,40}\b", " ", d)          # git SHAs (after lowercase)
    words = [w for w in d.split() if re.match(r"[a-z0-9]", w)]
    return " ".join(words[:12])


def _station_name_from_description(description: str) -> str | None:
    """Extract the station slug from a 'build ... station X' task.

    Returns lowercase slug or None when not a station-build task.
    """
    d = description.lower()
    if "station" not in d or not re.search(r"\bbuild\b", d):
        return None
    # "build <name> station"
    m = re.search(r"\bbuild\s+(?:the\s+)?['\"]?([a-z0-9][a-z0-9_-]*)['\"]?\s+station\b", d)
    if m:
        return m.group(1)
    # "station <name>"
    m = re.search(r"\bstation\s+['\"]?([a-z0-9][a-z0-9_-]*)['\"]?", d)
    if m:
        return m.group(1)
    return None


def _is_station_ready(station_name: str, agent_dir: Path) -> bool:
    """Return True when factory/<station_name>/station.yaml has ready: true."""
    yaml_path = agent_dir / "factory" / station_name / "station.yaml"
    if not yaml_path.exists():
        return False
    try:
        content = yaml_path.read_text(encoding="utf-8")
        return bool(re.search(r"^\s*ready\s*:\s*true\s*$", content, re.MULTILINE | re.IGNORECASE))
    except OSError:
        return False


def _load_done_intents(agent_dir: Path, days: int = 7) -> set[str]:
    """Read normalised intents from done.jsonl, up to `days` days back."""
    done_file = agent_dir / "done.jsonl"
    if not done_file.exists():
        return set()
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    intents: set[str] = set()
    try:
        for raw in done_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
                ts_str = record.get("at", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str).timestamp()
                        if ts < cutoff:
                            continue
                    except ValueError:
                        pass
                intent = record.get("intent", "")
                if intent:
                    intents.add(intent)
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return intents


def _parse_plan(plan_text: str) -> TaskQueue:
    """Parse plan markdown into a TaskQueue.

    Recognises checkbox lists (``- [ ]`` / ``- [x]``), numbered lists
    (``1.``), and plain bullet lists (``- ``).  Priority markers like
    ``**[CRITICAL]**`` and ``**[HIGH]**`` are extracted.
    """
    tasks: list[Task] = []
    task_id = 0
    last_top: Task | None = None  # most recent top-level task, for subtasks

    for line in plan_text.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        checkbox_match = re.match(r"^[-*]\s*\[([ xX])\]\s*(.*)", stripped)
        numbered_match = re.match(r"^\d+[.)]\s+(.*)", stripped)
        bullet_match = re.match(r"^[-*]\s+(.*)", stripped)

        description: str | None = None
        done = False

        if checkbox_match:
            done = checkbox_match.group(1).lower() == "x"
            description = checkbox_match.group(2).strip()
        elif numbered_match:
            description = numbered_match.group(1).strip()
        elif bullet_match:
            candidate = bullet_match.group(1).strip()
            if candidate and not candidate.startswith("#"):
                description = candidate

        if not description:
            continue

        priority = 0
        priority_pattern = r"\*\*\[(\w+)\]\*\*\s*"
        priority_match = re.search(priority_pattern, description)
        if priority_match:
            marker = priority_match.group(1).upper()
            if marker == "CRITICAL":
                priority = 2
            elif marker == "HIGH":
                priority = 1
            description = re.sub(priority_pattern, "", description).strip()

        if not description:
            continue

        task_id += 1
        task = Task(
            id=f"task-{task_id}",
            description=description,
            status="done" if done else "pending",
            priority=priority,
        )
        # Indented bullets attach as subtasks of the last top-level task — so
        # a directive can decompose into acknowledge → act → reply, and the
        # parent can't be 'done' until every leaf is.
        if indent >= 2 and last_top is not None:
            last_top.subtasks.append(task)
        else:
            tasks.append(task)
            last_top = task

    return TaskQueue(tasks=tasks)


class AgentState(Enum):
    """Lifecycle states. Agents don't start/stop — they sleep/wake."""

    ONBOARDING = "onboarding"  # First-time setup, no experience yet
    SLEEPING = "sleeping"  # Idle, identity persists on disk
    WAKING = "waking"  # Loading identity, checking queue
    PLANNING = "planning"  # Building today's plan (conscious)
    EXECUTING = "executing"  # Working through plan
    REPLANNING = "replanning"  # Adjusting plan mid-cycle (conscious)
    REFLECTING = "reflecting"  # End-of-day review (conscious)


# Standard identity file names (subdirectory layout)
IDENTITY_FILES = {
    "identity": "identity/identity.md",  # Living Summary
    "soul": "identity/soul.md",  # Persona parameters
    "skills": "identity/skills.md",  # Domain knowledge
    "responsibilities": "identity/responsibilities.md",  # Role & Responsibilities
    "plan": "today/plan.md",  # Current plan
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

    def archive_identity(self, file_key: str) -> Path | None:
        """Archive the current identity file before a rewrite.

        The Living Summary regeneration *overwrites* identity.md with a
        lossy LLM compression — without an archive, anything the rewrite
        drops is gone forever and a bad regeneration (the failure mode
        behind the role-contamination incident) is unrecoverable.
        Each rewrite snapshots the outgoing version to
        ``identity/history/<file_key>-<timestamp>.md`` so identity
        compounds instead of churning.

        Returns the archive path, or None when there is nothing to
        archive. Writing is skipped when the content is identical to the
        most recent archive.
        """
        current = self.read_identity(file_key)
        if not current.strip():
            return None
        history_dir = self.directory / "identity" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(history_dir.glob(f"{file_key}-*.md"))
        if existing and existing[-1].read_text(encoding="utf-8") == current:
            return existing[-1]
        # Microsecond stamp: fixed width, so filename sort order IS
        # chronological order, and same-second rewrites can't collide.
        stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S-%f")
        path = history_dir / f"{file_key}-{stamp}.md"
        path.write_text(current, encoding="utf-8")
        return path

    def identity_history(self, file_key: str) -> list[Path]:
        """Archived versions of an identity file, oldest first."""
        history_dir = self.directory / "identity" / "history"
        if not history_dir.exists():
            return []
        return sorted(history_dir.glob(f"{file_key}-*.md"))

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
                AgentState.REPLANNING,
                AgentState.REFLECTING,
                AgentState.SLEEPING,
            },
            AgentState.REPLANNING: {
                AgentState.EXECUTING,
                AgentState.REFLECTING,
                AgentState.SLEEPING,
            },
            AgentState.REFLECTING: {AgentState.SLEEPING},
        }
        return target in valid.get(self.state, set())

    def transition(self, target: AgentState) -> None:
        """Transition to a new state."""
        if not self.can_transition(target):
            raise ValueError(f"Invalid transition: {self.state.value} → {target.value}")
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

    # ----- Plan ownership -----
    #
    # The agent owns its planned work. The Fabric provides the plan
    # text (from the LLM), but the agent decides what to work on next,
    # when to replan, and whether to accept a task.

    # How many exceptions before a replan is triggered
    EXCEPTION_THRESHOLD = 3
    # Maximum number of replans per wake cycle
    MAX_REPLANS = 3

    def _filter_duplicate_tasks(
        self, tasks: list[Task]
    ) -> tuple[list[Task], list[tuple[str, str, str]]]:
        """Remove duplicate or already-done tasks from a parsed task list.

        Checks in order:
        1. Same normalised intent already seen in this queue (intra-queue).
        2. Intent already completed this week (done.jsonl).
        3. Station-build task whose station.yaml is already ready: true.

        Returns (filtered_tasks, drops) where each drop is
        (reason, intent, original_description).  Non-pending tasks (already
        done/skipped/etc.) are always kept — only pending duplicates are dropped.
        """
        done_intents = _load_done_intents(self.directory)
        seen_intents: set[str] = set()
        filtered: list[Task] = []
        drops: list[tuple[str, str, str]] = []

        for task in tasks:
            intent = _normalise_intent(task.description)

            # Intra-queue dedupe applies to all tasks (done or pending): a plan
            # that lists the same completed item three times keeps only the first.
            if intent and intent in seen_intents:
                drops.append(("intra-queue duplicate", intent, task.description))
                continue

            # For pending tasks only: also check done.jsonl and station.yaml.
            if task.status == "pending" and intent:
                if intent in done_intents:
                    drops.append(("already done this week", intent, task.description))
                    continue

                station = _station_name_from_description(task.description)
                if station and _is_station_ready(station, self.directory):
                    drops.append((f"station {station!r} already ready", intent, task.description))
                    continue

            seen_intents.add(intent)
            filtered.append(task)

        return filtered, drops

    def _log_drops(self, drops: list[tuple[str, str, str]]) -> None:
        """Append one line per dropped task to today's journal file."""
        if not drops:
            return
        journal = self.journal_path()
        lines = []
        for reason, intent, description in drops:
            # Keep the description short so the journal stays scannable
            short = description[:80].replace("\n", " ")
            lines.append(f"- dropped ({reason}): {intent!r}  [{short}]")
        try:
            with journal.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def set_plan(self, plan_text: str) -> TaskQueue:
        """Parse a plan markdown into a TaskQueue and take ownership.

        Called by the Fabric after the consciousness adapter produces a
        plan.  The agent stores the plan text in its identity files and
        parses it into an executable queue.
        """
        raw_queue = _parse_plan(plan_text)
        filtered, drops = self._filter_duplicate_tasks(raw_queue.tasks)
        self._log_drops(drops)
        raw_queue.tasks = filtered
        self.task_queue = raw_queue
        self.write_identity("plan", plan_text)
        self.persist_runtime_state()
        return self.task_queue

    def update_plan(self, plan_text: str) -> TaskQueue:
        """Replace the current plan (replan), preserving replan count."""
        replan_count = self.task_queue.replan_count if self.task_queue else 0
        raw_queue = _parse_plan(plan_text)
        filtered, drops = self._filter_duplicate_tasks(raw_queue.tasks)
        self._log_drops(drops)
        raw_queue.tasks = filtered
        raw_queue.replan_count = replan_count + 1
        self.task_queue = raw_queue
        self.write_identity("plan", plan_text)
        self.persist_runtime_state()
        return self.task_queue

    def next_task(self) -> Task | None:
        """Return the next task to work on (highest priority pending).

        The agent decides the order, not the Fabric.
        """
        if self.task_queue is None:
            plan_text = self.read_identity("plan")
            self.task_queue = _parse_plan(plan_text)
        return self.task_queue.next_pending()

    def complete_task(self, task: Task, outcome: str) -> None:
        """Mark a task complete — but a parent with open subtasks can only be
        ACKNOWLEDGED, never done. The work isn't finished until every leaf is
        (e.g. you can't 'complete' a directive while 'reply to founder' is open).
        Done only counts as delivered work; acknowledged does not."""
        if not task.can_complete():
            task.status = "acknowledged"
            task.outcome = outcome
            return
        task.status = "done"
        task.outcome = outcome
        self.tasks_completed_today += 1
        # Record the completed intent so future plan cycles can dedupe against it.
        intent = _normalise_intent(task.description)
        if intent:
            record = json.dumps({
                "intent": intent,
                "description": task.description[:120],
                "at": datetime.now(UTC).isoformat(),
            })
            done_path = self.directory / "done.jsonl"
            try:
                with done_path.open("a", encoding="utf-8") as f:
                    f.write(record + "\n")
            except OSError:
                pass

    def fail_task(self, task: Task, error: str) -> None:
        """Mark a task as failed and add to exception pile."""
        task.status = "exception"
        task.error = error
        if self.task_queue is not None:
            self.task_queue.exceptions.append(task)
        self.tasks_escalated_today += 1

    def defer_task(self, task: Task, reason: str) -> None:
        """Defer a task (pending_approval, budget exhausted, etc.)."""
        task.status = "pending_approval" if "approv" in reason.lower() else "exception"
        task.error = reason
        if task.status == "exception" and self.task_queue is not None:
            self.task_queue.exceptions.append(task)
            self.tasks_escalated_today += 1

    def needs_replan(self, messages: list[Any]) -> bool:
        """Decide whether a replan is warranted.

        The agent checks its own state — exception count, urgent
        messages, and completion status — rather than the Fabric
        deciding for it.
        """
        if self.task_queue is None:
            return False
        if self.task_queue.replan_count >= self.MAX_REPLANS:
            return False

        # Too many exceptions
        if len(self.task_queue.exceptions) >= self.EXCEPTION_THRESHOLD:
            return True

        # Urgent message
        for msg in messages:
            content = getattr(msg, "content", "")
            if "urgent" in content.lower():
                return True

        # All pending done but exceptions remain
        pending = [t for t in self.task_queue.tasks if t.status == "pending"]
        if not pending and self.task_queue.exceptions:
            return True

        return False

    def clear_plan(self) -> None:
        """Clear the task queue (called on sleep)."""
        self.task_queue = None

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
