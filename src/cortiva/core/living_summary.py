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

DAY_REPORT_DELIMITER = "---DAY-REPORT---"


def split_identity_and_day_report(
    content: str,
) -> tuple[str | None, str | None]:
    """Split a regeneration response into (identity, day_report).

    The regeneration prompt asks for the updated identity.md followed
    by ``---DAY-REPORT---`` and a first-person day report. Parsing is
    graceful: if the delimiter is absent the whole response is treated
    as identity and the day report is ``None`` (callers fall back to
    the stats-based day summary for the journal).
    """
    if not content:
        return None, None
    if DAY_REPORT_DELIMITER not in content:
        identity = content.strip()
        return (identity or None), None
    identity_part, report_part = content.split(DAY_REPORT_DELIMITER, 1)
    identity = identity_part.strip()
    report = report_part.strip()
    return (identity or None), (report or None)


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
        *,
        soul: str = "",
        responsibilities: str = "",
        revision_count: int = 0,
        ground_truth: str = "",
    ) -> str:
        """Build the prompt for identity.md regeneration.

        ``soul`` and ``responsibilities`` anchor the rewrite: identity
        evolves from the agent's own experience but must never drift off
        its role or persona (the role-contamination failure mode — an
        agent absorbing colleagues' domains until the org converges on a
        monoculture). ``revision_count`` tells the agent its identity is
        a compounding document with an archived history, not a blank
        page.
        """
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

        if soul:
            sections.append(
                "## Your Soul (immutable persona — set at creation, never "
                f"changes)\n\n{soul[:1500]}\n"
            )

        if responsibilities:
            sections.append(
                "## Your Role & Responsibilities (your anchor)\n\n"
                f"{responsibilities[:1500]}\n"
            )

        if revision_count > 0:
            sections.append(
                f"## Continuity\n\nThis is revision {revision_count + 1} of "
                "your identity. Previous versions are archived in "
                "identity/history/ — this document compounds over time; "
                "it is never a blank page.\n"
            )

        # Tested reality — the arbiter. Identity is rewritten from experience,
        # but experience can be self-sealing: an agent that wrongly decided a
        # capability was down then *worked around it* never generated the
        # experience that would disprove the belief, so the belief crystallises
        # into identity and nothing evicts it. The capability probe is the one
        # source that actually tested the world; placing it ABOVE the current
        # identity (with an explicit override rule below) is what lets the agent
        # reconcile a stale belief against reality and rewrite it ITSELF.
        if ground_truth:
            sections.append(
                "## Verified reality — TESTED just now (this OUTRANKS your "
                f"memory and your current identity)\n\n{ground_truth}\n"
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
            "Don't just list facts — show how experience has shaped you.\n\n"
            "Two hard rules:\n"
            "1. Stay anchored to YOUR role and soul above. Evolve only from "
            "your own experience — never adopt the role, domain, or "
            "specialisations of colleagues whose work you have merely "
            "observed or read about.\n"
            "2. This document compounds. Carry forward the durable "
            "specialisations and hard-won learnings already in your current "
            "identity unless your experience now contradicts them — do not "
            "start from scratch or let detail evaporate.\n"
            "3. Reconcile against tested reality. If the 'Verified reality' "
            "block above contradicts any belief in your current identity, "
            "learnings, or themes — e.g. you believe a capability is "
            "down/blocked/missing or that you must route around it, but the "
            "check says it is LIVE/OK — that belief is FALSE. Correct it or "
            "remove it; do NOT carry it forward. Tested reality outranks "
            "memory, experience, and your prior identity. (You don't keep "
            "believing a door is locked after you've just opened it.)\n\n"
            f"Then, on its own line, write exactly `{DAY_REPORT_DELIMITER}` "
            "followed by a short first-person day report — your standup for "
            "the humans you work with. Cover: what you worked on today, "
            "what you completed, where you've got to, anything blocking "
            "you, and what you plan to do next. Under 200 words, plain "
            "prose or short bullets."
        )

        return "\n\n".join(sections)

    async def regenerate(
        self,
        agent: Agent,
        day_summary: str,
        ground_truth: str = "",
    ) -> str | None:
        """Regenerate the Living Summary for an agent.

        ``ground_truth`` is the just-tested capability status (from the node
        probe). It is fed into the rewrite as the arbiter so the agent
        reconciles any stale belief against reality and corrects it itself —
        the cure for a false belief crystallising into identity.

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

        # Anchors: soul (immutable persona) and responsibilities (role)
        # keep the rewrite from drifting off who this agent IS; the
        # revision count tells it the document compounds. Tolerant of
        # mocks/older Agent objects that lack these surfaces.
        soul = ""
        responsibilities = ""
        revision_count = 0
        try:
            soul = str(agent.read_identity("soul") or "")
            responsibilities = str(agent.read_identity("responsibilities") or "")
        except Exception:
            pass
        try:
            revision_count = len(agent.identity_history("identity"))
        except Exception:
            revision_count = 0

        prompt = self.build_regeneration_prompt(
            agent, current_identity, day_summary, experience,
            soul=soul,
            responsibilities=responsibilities,
            revision_count=revision_count,
            ground_truth=ground_truth,
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
