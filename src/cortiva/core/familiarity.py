"""
Familiarity Engine — the agent's gut feeling about a task.

Searches memory for similar past experiences and produces a
:class:`~cortiva.adapters.protocols.FamiliaritySignal` that tells the
routine layer (and the agent itself) how novel or routine this task is.
"""

from __future__ import annotations

from cortiva.adapters.protocols import FamiliaritySignal, MemoryAdapter, MemoryRecord


# Thresholds for classifying familiarity strength
_ROUTINE_MIN = 5      # >=5 similar memories → routine
_FAMILIAR_MIN = 2     # 2-4 → familiar
_VAGUE_MIN = 1        # 1 → vague recognition
# 0 → novel


def _infer_valence(memories: list[MemoryRecord]) -> str:
    """Derive valence from past experiences.

    Looks at memory content for negative/positive sentiment keywords
    and importance scores.  High importance + negative keywords → cautious.
    """
    if not memories:
        return "neutral"

    negative_keywords = {"fail", "error", "bug", "wrong", "reject", "problem", "issue", "broken"}
    positive_keywords = {"success", "done", "complete", "approved", "good", "great", "resolved"}

    neg_count = 0
    pos_count = 0
    for m in memories:
        words = set(m.content.lower().split())
        if words & negative_keywords:
            neg_count += 1
        if words & positive_keywords:
            pos_count += 1

    if neg_count > pos_count and neg_count >= 2:
        return "cautious"
    if neg_count > 0 and pos_count == 0:
        return "negative"
    if pos_count > neg_count:
        return "positive"
    return "neutral"


def _classify_strength(match_count: int) -> str:
    if match_count >= _ROUTINE_MIN:
        return "routine"
    if match_count >= _FAMILIAR_MIN:
        return "familiar"
    if match_count >= _VAGUE_MIN:
        return "vague_recognition"
    return "novel"


def _build_text(strength: str, valence: str, match_count: int) -> str:
    """Generate a natural-language description for context injection."""
    if strength == "novel":
        return "This task is entirely new. No similar experiences found in memory."
    if strength == "vague_recognition":
        base = f"This task seems vaguely familiar ({match_count} similar experience found)."
    elif strength == "familiar":
        base = f"This task is familiar ({match_count} similar experiences found)."
    else:
        base = f"This task is routine ({match_count} similar experiences found)."

    if valence == "cautious":
        base += " Past experiences suggest caution — there were problems before."
    elif valence == "negative":
        base += " Previous attempts had negative outcomes."
    elif valence == "positive":
        base += " Previous experiences were positive."

    return base


class FamiliarityEngine:
    """Computes familiarity signals by searching agent memory.

    Parameters
    ----------
    memory:
        The memory adapter to search.
    search_limit:
        Maximum number of memories to retrieve per query.
    min_importance:
        Only consider memories at or above this importance level.
    """

    def __init__(
        self,
        memory: MemoryAdapter,
        *,
        search_limit: int = 10,
        min_importance: float = 3.0,
    ) -> None:
        self._memory = memory
        self._search_limit = search_limit
        self._min_importance = min_importance

    async def assess(self, agent_id: str, task_description: str) -> FamiliaritySignal:
        """Search memory and produce a familiarity signal for *task_description*."""
        memories = await self._memory.search(
            agent_id,
            task_description,
            limit=self._search_limit,
            min_importance=self._min_importance,
        )

        match_count = len(memories)
        strength = _classify_strength(match_count)
        valence = _infer_valence(memories)
        text = _build_text(strength, valence, match_count)

        return FamiliaritySignal(
            strength=strength,
            valence=valence,
            match_count=match_count,
            text=text,
            retrieved=memories,
        )
