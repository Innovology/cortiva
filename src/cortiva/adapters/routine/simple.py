"""Simple routine adapter — pure Python, no external dependencies.

Uses keyword overlap between task descriptions and procedures to determine
whether a task can be handled procedurally.  This is the fallback when
Ollama (or any other local model) is not available.
"""

from __future__ import annotations

import re
from typing import Any

from cortiva.adapters.protocols import FamiliaritySignal, MemoryRecord


# Words that carry no semantic weight for matching
_STOP_WORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might can could of in to for with on "
    "at by from as into through during before after above below between "
    "and or but not no nor so yet both either neither each every all "
    "any few more most other some such than too very just about also "
    "back even still already always never often once only then when "
    "where how what which who whom this that these those it its i me my".split()
)


def _tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, remove stop words."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOP_WORDS and len(w) > 1}


def _extract_procedures(procedures_text: str) -> list[tuple[str, set[str]]]:
    """Split procedures.md into (raw_text, token_set) pairs.

    Each procedure is delimited by a markdown heading (## or ###) or a
    numbered/bullet list item that starts a new block.
    """
    blocks: list[str] = []
    current: list[str] = []

    for line in procedures_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and current:
            blocks.append("\n".join(current))
            current = [stripped]
        else:
            current.append(stripped)

    if current:
        blocks.append("\n".join(current))

    return [(b, _tokenize(b)) for b in blocks if _tokenize(b)]


class SimpleRoutineAdapter:
    """Keyword-overlap routine adapter.  Zero external dependencies.

    Works by computing Jaccard similarity between the task description
    tokens and each procedure block.  If the best match exceeds
    *confidence_threshold*, the task is handled procedurally.
    """

    def __init__(
        self,
        *,
        confidence_threshold: float = 0.30,
        defer_threshold: float = 0.15,
    ) -> None:
        self._confidence_threshold = confidence_threshold
        self._defer_threshold = defer_threshold

    async def assess(
        self,
        agent_id: str,
        task_description: str,
        procedural_index: str,
        familiarity: FamiliaritySignal,
    ) -> dict[str, Any]:
        task_tokens = _tokenize(task_description)
        if not task_tokens:
            return {
                "action": "escalate",
                "procedure_match": None,
                "confidence": 0.0,
                "context_for_conscious": None,
            }

        procedures = _extract_procedures(procedural_index)
        best_score = 0.0
        best_block = ""

        for raw, proc_tokens in procedures:
            if not proc_tokens:
                continue
            intersection = task_tokens & proc_tokens
            union = task_tokens | proc_tokens
            score = len(intersection) / len(union) if union else 0.0
            if score > best_score:
                best_score = score
                best_block = raw

        # Familiarity boost: routine tasks get a small confidence bump
        if familiarity.strength == "routine":
            best_score = min(1.0, best_score + 0.10)
        elif familiarity.strength == "familiar":
            best_score = min(1.0, best_score + 0.05)

        if best_score >= self._confidence_threshold:
            return {
                "action": "procedural",
                "procedure_match": best_block,
                "confidence": round(best_score, 4),
                "context_for_conscious": None,
                "result": f"Matched procedure (confidence {best_score:.0%}): {best_block[:200]}",
            }

        if best_score < self._defer_threshold:
            return {
                "action": "escalate",
                "procedure_match": None,
                "confidence": round(best_score, 4),
                "context_for_conscious": None,
            }

        # Between defer and confidence thresholds — defer for batched replan
        return {
            "action": "defer",
            "procedure_match": best_block if best_block else None,
            "confidence": round(best_score, 4),
            "context_for_conscious": None,
        }

    async def compile_context(
        self,
        agent_id: str,
        identity: str,
        memories: list[MemoryRecord],
        familiarity: FamiliaritySignal,
        task: str,
        additional: dict[str, str] | None = None,
    ) -> str:
        sections = [
            f"## Identity\n{identity}",
            f"## Familiarity\n{familiarity.text}",
            f"## Task\n{task}",
        ]
        if memories:
            mem_text = "\n".join(f"- {m.content}" for m in memories[:10])
            sections.insert(2, f"## Relevant Memories\n{mem_text}")
        if additional:
            for key, value in additional.items():
                sections.append(f"## {key}\n{value}")
        return "\n\n".join(sections)
