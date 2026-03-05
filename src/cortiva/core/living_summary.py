"""
Living Summary auto-regeneration.

The Living Summary (identity.md) evolves over time based on accumulated
experience. During reflection, the regenerator pulls key memories and
patterns to ensure the summary reflects actual experience, not just the
last day's events.

Early:  "I'm a new bookkeeper"
Later:  "I'm an experienced bookkeeper who specialises in international
         vendor management and has developed a particular sensitivity
         to duplicate receipt patterns"
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cortiva.adapters.protocols import ConsciousnessAdapter, MemoryAdapter, MemoryRecord
    from cortiva.core.agent import Agent


class LivingSummaryRegenerator:
    """Regenerates identity.md from accumulated experience.

    Called during end-of-day reflection to produce a summary that
    reflects the agent's full history, not just the most recent day.
    """

    def __init__(
        self,
        memory: MemoryAdapter,
        consciousness: ConsciousnessAdapter,
        *,
        memory_limit: int = 20,
        learning_limit: int = 10,
        min_importance: float = 6.0,
    ) -> None:
        self.memory = memory
        self.consciousness = consciousness
        self.memory_limit = memory_limit
        self.learning_limit = learning_limit
        self.min_importance = min_importance

    async def gather_experience(self, agent_id: str) -> dict[str, Any]:
        """Gather experience data for summary regeneration."""
        # Key memories (highest importance)
        key_memories = await self.memory.recall(
            agent_id, limit=self.memory_limit, min_importance=self.min_importance,
        )

        # Learnings (tagged with "learning" or "reflection")
        learnings = await self.memory.search(
            agent_id, "learned",
            limit=self.learning_limit,
            tags=["learning"],
        )

        # Task patterns (tagged with "task")
        task_memories = await self.memory.search(
            agent_id, "Task:",
            limit=self.memory_limit,
            tags=["task"],
        )

        # Compute simple stats
        total_tasks = len(task_memories)
        terminal_tasks = sum(
            1 for m in task_memories if "terminal" in m.tags
        )
        escalated = sum(
            1 for m in task_memories if "escalated" in m.tags
        )

        # Extract recurring themes from content
        themes = _extract_themes(key_memories + learnings)

        return {
            "key_memories": key_memories,
            "learnings": learnings,
            "task_count": total_tasks,
            "terminal_task_count": terminal_tasks,
            "escalated_count": escalated,
            "themes": themes,
        }

    def build_regeneration_prompt(
        self,
        agent: Agent,
        current_identity: str,
        day_summary: str,
        experience: dict[str, Any],
    ) -> str:
        """Build the prompt for identity.md regeneration."""
        key_memories = experience.get("key_memories", [])
        learnings = experience.get("learnings", [])
        themes = experience.get("themes", [])
        task_count = experience.get("task_count", 0)

        sections = []

        sections.append(
            "You are updating your Living Summary (identity.md). "
            "This document is your evolving self-description — it should "
            "reflect your accumulated experience, not just today's events.\n"
        )

        sections.append(f"## Current Identity\n\n{current_identity}\n")

        sections.append(f"## Today's Summary\n\n{day_summary}\n")

        if key_memories:
            mem_lines = [f"- {m.content}" for m in key_memories[:10]]
            sections.append(
                f"## Key Experiences ({len(key_memories)} total)\n\n"
                + "\n".join(mem_lines) + "\n"
            )

        if learnings:
            learn_lines = [f"- {m.content}" for m in learnings[:10]]
            sections.append(
                f"## Learnings\n\n" + "\n".join(learn_lines) + "\n"
            )

        if themes:
            sections.append(
                f"## Recurring Themes\n\n"
                + ", ".join(themes) + "\n"
            )

        if task_count > 0:
            sections.append(
                f"## Experience Stats\n\n"
                f"Tasks completed: {task_count}\n"
                f"Terminal tasks: {experience.get('terminal_task_count', 0)}\n"
                f"Escalations: {experience.get('escalated_count', 0)}\n"
            )

        sections.append(
            "## Instructions\n\n"
            "Rewrite identity.md to reflect your full accumulated experience. "
            "Include:\n"
            "- Who you are and what you do\n"
            "- Your current focus and specialisations\n"
            "- Key learnings and patterns you've noticed\n"
            "- Your working style (evolved from experience)\n"
            "- Areas where you've grown or changed\n\n"
            "Write in first person. Be specific about what you've learned. "
            "Don't just list facts — show how experience has shaped you."
        )

        return "\n\n".join(sections)

    async def regenerate(
        self,
        agent: Agent,
        day_summary: str,
    ) -> str | None:
        """Regenerate the Living Summary for an agent.

        Returns the new identity content, or None if regeneration
        was skipped (e.g., not enough experience yet).
        """
        current_identity = agent.read_identity("identity")
        experience = await self.gather_experience(agent.id)

        # Skip regeneration if agent has very little experience
        if (
            not experience["key_memories"]
            and not experience["learnings"]
            and experience["task_count"] == 0
        ):
            return None

        prompt = self.build_regeneration_prompt(
            agent, current_identity, day_summary, experience,
        )

        response = await self.consciousness.reflect(
            agent_id=agent.id,
            context=prompt,
            day_summary=day_summary,
        )

        return response.content if response.content else None


def _extract_themes(memories: list[MemoryRecord]) -> list[str]:
    """Extract recurring themes from memory content via keyword frequency."""
    word_count: dict[str, int] = {}

    # Skip common stop words
    stop_words = {
        "the", "a", "an", "is", "was", "are", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "shall", "can",
        "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "and", "or", "but", "not", "no", "this", "that", "it", "its",
        "i", "my", "me", "we", "our", "you", "your", "they", "their",
        "task", "completed", "done", "outcome", "agent",
    }

    for mem in memories:
        words = mem.content.lower().split()
        for word in words:
            cleaned = word.strip(".,;:!?\"'()-[]{}").lower()
            if len(cleaned) > 3 and cleaned not in stop_words:
                word_count[cleaned] = word_count.get(cleaned, 0) + 1

    # Return words that appear 2+ times, sorted by frequency
    recurring = sorted(
        ((w, c) for w, c in word_count.items() if c >= 2),
        key=lambda x: x[1],
        reverse=True,
    )
    return [w for w, _ in recurring[:10]]
