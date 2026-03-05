"""
Context assembly for Cortiva agent lifecycle phases.

Builds priority-ordered, token-aware context packages for each phase
of the agent lifecycle: planning, execution, replanning, and reflection.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cortiva.adapters.protocols import MemoryAdapter, MemoryRecord, Message
    from cortiva.core.agent import Agent, TaskQueue


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token per 4 characters."""
    return len(text) // 4


def _identity_to_context(identity: dict[str, str]) -> str:
    """Render an identity dict as markdown sections."""
    sections = []
    for key, content in identity.items():
        if content.strip():
            sections.append(f"## {key.title()}\n\n{content}")
    return "\n\n---\n\n".join(sections)


def _format_messages(messages: list[Message]) -> str:
    """Render messages as a bullet list."""
    if not messages:
        return ""
    lines = [f"- [{m.sender}]: {m.content}" for m in messages]
    return "## Messages\n\n" + "\n".join(lines)


def _format_memories(memories: list[MemoryRecord], header: str) -> str:
    """Render a list of MemoryRecord as a context section."""
    if not memories:
        return ""
    lines = [f"- [{m.importance:.0f}] {m.content}" for m in memories]
    return f"## {header}\n\n" + "\n".join(lines)


def _format_plan_status(task_queue: TaskQueue) -> str:
    """Render done/pending/in-progress tasks from the queue."""
    if task_queue is None:
        return ""
    summary = task_queue.completion_summary()
    total = len(task_queue.tasks)
    done = summary.get("done", 0)
    pending = summary.get("pending", 0)
    in_progress = summary.get("in_progress", 0)
    exceptions = summary.get("exceptions", 0)
    return (
        "## Plan Status\n\n"
        f"Total: {total}, Done: {done}, Pending: {pending}, "
        f"In-progress: {in_progress}, Exceptions: {exceptions}"
    )


class ContextBuilder:
    """Assembles priority-ordered, token-aware context for each lifecycle phase."""

    def __init__(self, memory: MemoryAdapter, max_tokens: int = 16_000) -> None:
        self.memory = memory
        self.max_tokens = max_tokens

    def _truncate(self, sections: list[tuple[int, str]]) -> str:
        """Assemble sections in priority order, dropping low-priority ones when over budget.

        Each section is a ``(priority, text)`` tuple. Higher priority values
        are included first. If the budget is exceeded, lower-priority sections
        are dropped or truncated.
        """
        # Sort by priority descending
        ordered = sorted(sections, key=lambda s: s[0], reverse=True)
        budget_chars = self.max_tokens * 4
        parts: list[str] = []
        used = 0

        for _priority, text in ordered:
            if not text:
                continue
            text_len = len(text)
            if used + text_len <= budget_chars:
                parts.append(text)
                used += text_len
            else:
                remaining = budget_chars - used
                if remaining > 100:
                    parts.append(text[:remaining])
                    used += remaining
                break

        return "\n\n---\n\n".join(parts)

    async def build_plan_context(
        self,
        agent: Agent,
        identity: dict[str, str],
        messages: list[Message],
    ) -> str:
        """Context for wake/planning phase."""
        identity_text = _identity_to_context(identity)
        responsibilities = identity.get("responsibilities", "")
        procedures = identity.get("procedures", "")
        previous_plan = identity.get("plan", "")
        date_text = f"## Date\n\n{datetime.utcnow().strftime('%A, %Y-%m-%d')}"
        messages_text = _format_messages(messages)

        # Recall recent high-importance memories
        memories = await self.memory.recall(agent.id, limit=10, min_importance=6.0)
        memories_text = _format_memories(memories, "Recent Memories")

        sections: list[tuple[int, str]] = [
            (100, identity_text),
            (95, f"## Responsibilities\n\n{responsibilities}" if responsibilities.strip() else ""),
            (90, f"## Procedures\n\n{procedures}" if procedures.strip() else ""),
            (80, f"## Previous Plan\n\n{previous_plan}" if previous_plan.strip() else ""),
            (70, date_text),
            (60, messages_text),
            (50, memories_text),
        ]
        return self._truncate(sections)

    async def build_execution_context(
        self,
        agent: Agent,
        identity: dict[str, str],
        messages: list[Message],
        task: str,
        *,
        assessment: dict | None = None,
    ) -> str:
        """Context for task execution phase."""
        identity_text = _identity_to_context(identity)
        skills = identity.get("skills", "")
        responsibilities = identity.get("responsibilities", "")

        # Search for task-relevant memories
        memories = await self.memory.search(agent.id, task, limit=5)
        memories_text = _format_memories(memories, "Relevant Memories")

        # Familiarity context from routine assessment
        familiarity_text = ""
        if assessment and assessment.get("context_for_conscious"):
            familiarity_text = (
                "## Familiarity Context\n\n" + assessment["context_for_conscious"]
            )

        plan_status = _format_plan_status(agent.task_queue) if agent.task_queue else ""
        messages_text = _format_messages(messages)

        sections: list[tuple[int, str]] = [
            (100, identity_text),
            (90, f"## Skills\n\n{skills}" if skills.strip() else ""),
            (85, f"## Responsibilities\n\n{responsibilities}" if responsibilities.strip() else ""),
            (70, memories_text),
            (65, familiarity_text),
            (60, plan_status),
            (50, messages_text),
        ]
        return self._truncate(sections)

    async def build_replan_context(
        self,
        agent: Agent,
        identity: dict[str, str],
        messages: list[Message],
    ) -> str:
        """Context for replanning phase."""
        assert agent.task_queue is not None

        identity_text = _identity_to_context(identity)

        # Plan completion summary
        summary = agent.task_queue.completion_summary()
        total = len(agent.task_queue.tasks)
        done = summary.get("done", 0)
        rate = (done / total * 100) if total > 0 else 0
        completed = [t for t in agent.task_queue.tasks if t.status == "done"]
        completed_str = ", ".join(t.description for t in completed) or "none"
        completion_text = (
            "## Plan Completion\n\n"
            f"Completed: {completed_str}\n"
            f"Completion rate: {rate:.0f}%\n"
        )

        # Exception pile
        exceptions = agent.task_queue.exceptions
        exception_str = (
            ", ".join(f"{t.description} ({t.error})" for t in exceptions) or "none"
        )
        exception_text = f"## Exceptions\n\n{exception_str}"

        messages_text = _format_messages(messages)

        # Plan vs reality gap
        pending = summary.get("pending", 0)
        gap_text = (
            "## Plan vs Reality\n\n"
            f"Total: {total}, Done: {done}, Pending: {pending}, "
            f"Exceptions: {len(exceptions)}, Replans: {agent.task_queue.replan_count}"
        )

        sections: list[tuple[int, str]] = [
            (100, identity_text),
            (95, completion_text),
            (90, exception_text),
            (80, messages_text),
            (70, gap_text),
        ]
        return self._truncate(sections)

    async def build_reflection_context(
        self,
        agent: Agent,
        identity: dict[str, str],
        day_summary: str,
    ) -> str:
        """Context for end-of-day reflection."""
        identity_text = _identity_to_context(identity)
        summary_text = f"## Day Summary\n\n{day_summary}" if day_summary else ""

        # Recall high-importance memories
        memories = await self.memory.recall(agent.id, limit=10, min_importance=7.0)
        memories_text = _format_memories(memories, "Key Memories")

        sections: list[tuple[int, str]] = [
            (100, identity_text),
            (95, summary_text),
            (70, memories_text),
        ]
        return self._truncate(sections)

    @staticmethod
    def build_day_summary(agent: Agent) -> str:
        """Build an end-of-day summary for an agent."""
        budget = f"{agent.consciousness_budget_used}/{agent.consciousness_budget_limit}"
        summary = (
            f"Tasks completed: {agent.tasks_completed_today}\n"
            f"Tasks escalated: {agent.tasks_escalated_today}\n"
            f"Consciousness budget used: {budget}\n"
        )

        if agent.task_queue is not None:
            stats = agent.task_queue.completion_summary()
            total = len(agent.task_queue.tasks)
            done_count = stats.get("done", 0)
            exception_count = stats.get("exceptions", 0)
            skipped_count = stats.get("skipped", 0)
            rate = (done_count / total * 100) if total > 0 else 0

            summary += (
                f"\n## Plan vs Reality\n"
                f"Total tasks: {total}\n"
                f"Completed: {done_count}\n"
                f"Exceptions: {exception_count}\n"
                f"Skipped: {skipped_count}\n"
                f"Completion rate: {rate:.0f}%\n"
                f"Replans: {agent.task_queue.replan_count}\n"
            )

            completed_tasks = [t for t in agent.task_queue.tasks if t.status == "done"]
            if completed_tasks:
                summary += "\nCompleted:\n"
                for t in completed_tasks:
                    summary += f"- {t.description}\n"

            if agent.task_queue.exceptions:
                summary += "\nExceptions:\n"
                for t in agent.task_queue.exceptions:
                    summary += f"- {t.description}: {t.error}\n"

        return summary
