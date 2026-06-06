"""
Emotion derivation (issue #9).

After each task execution, the fabric computes emotional dimensions
from task signals weighted by persona modifiers from soul.md — pure
arithmetic, no LLM inference — and blends them into the agent's rolling
emotional state (persisted to ``today/emotions.json`` for the node
heartbeat → HQ mood grid). Per-task dimensions are also stored with the
experience memory record via :pyattr:`MemoryRecord.emotion_dimensions`.

Wired into fabric._execute_task on 2026-06-06 — before that this module
was design-only and the dashboard's mood grid displayed soul.md's
static disposition WEIGHTS clamped to 1.0 (every agent permanently
"maxed out", which is how the flatline was caught).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TaskSignals:
    """Raw signals captured from a single task execution."""

    completion_speed: float = 0.0
    """Ratio of actual time vs. expected time (< 1.0 = faster than expected)."""

    error_count: int = 0
    """Number of errors encountered during execution."""

    was_escalated: bool = False
    """Whether the task required escalation to a higher authority."""

    outcome_matched_prediction: bool = True
    """Whether the actual outcome aligned with the agent's prediction."""

    familiarity_at_execution: float = 0.5
    """Familiarity strength at execution time (0 = novel, 1 = routine)."""


@dataclass
class PersonaModifiers:
    """Weights derived from soul.md that shape emotional response.

    Each modifier scales the corresponding emotion dimension.  Values
    are in the range ``[0.0, 2.0]`` where 1.0 is neutral.

    Schema (soul.md YAML front-matter)::

        ---
        emotional_modifiers:
          satisfaction_weight: 1.2   # amplifies satisfaction
          frustration_weight: 0.8   # dampens frustration
          curiosity_weight: 1.5     # amplifies curiosity
          confidence_weight: 1.0    # neutral confidence
          caution_weight: 1.3       # amplifies caution
        ---
    """

    satisfaction_weight: float = 1.0
    frustration_weight: float = 1.0
    curiosity_weight: float = 1.0
    confidence_weight: float = 1.0
    caution_weight: float = 1.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonaModifiers:
        """Parse from the ``emotional_modifiers`` block in soul.md."""
        return cls(
            satisfaction_weight=float(data.get("satisfaction_weight", 1.0)),
            frustration_weight=float(data.get("frustration_weight", 1.0)),
            curiosity_weight=float(data.get("curiosity_weight", 1.0)),
            confidence_weight=float(data.get("confidence_weight", 1.0)),
            caution_weight=float(data.get("caution_weight", 1.0)),
        )


@dataclass
class EmotionDimensions:
    """Computed emotional state after a task, stored on the experience node.

    All values are in the range ``[-1.0, 1.0]``.
    """

    satisfaction: float = 0.0
    """Positive when task completed quickly and correctly."""

    frustration: float = 0.0
    """Positive when errors occurred or outcome was unexpected."""

    curiosity: float = 0.0
    """Positive when the task was novel or produced a prediction error."""

    confidence: float = 0.0
    """Positive when familiar tasks succeed as expected."""

    caution: float = 0.0
    """Positive when past similar experiences had negative outcomes."""

    def to_dict(self) -> dict[str, float]:
        """Serialize for storage in :pyattr:`MemoryRecord.emotion_dimensions`."""
        return {
            "satisfaction": round(self.satisfaction, 3),
            "frustration": round(self.frustration, 3),
            "curiosity": round(self.curiosity, 3),
            "confidence": round(self.confidence, 3),
            "caution": round(self.caution, 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, float]) -> EmotionDimensions:
        return cls(
            satisfaction=data.get("satisfaction", 0.0),
            frustration=data.get("frustration", 0.0),
            curiosity=data.get("curiosity", 0.0),
            confidence=data.get("confidence", 0.0),
            caution=data.get("caution", 0.0),
        )


# ---------------------------------------------------------------------------
# Derivation formula
# ---------------------------------------------------------------------------

_DEFAULT_MODIFIERS = PersonaModifiers()


def _clamp(value: float) -> float:
    """Clamp a value to [-1.0, 1.0]."""
    return max(-1.0, min(1.0, value))


def derive_emotions(
    signals: TaskSignals,
    modifiers: PersonaModifiers | None = None,
) -> EmotionDimensions:
    """Compute emotion dimensions from task signals and persona modifiers.

    The formula is pure arithmetic — no LLM call required.

    Parameters
    ----------
    signals:
        Raw signals from task execution.
    modifiers:
        Persona weights from soul.md.  Defaults to neutral (1.0).

    Returns
    -------
    EmotionDimensions with values in [-1.0, 1.0].
    """
    m = modifiers or _DEFAULT_MODIFIERS

    # --- Satisfaction ---
    # Fast completion + no errors + outcome matched → high satisfaction
    speed_bonus = max(0.0, 1.0 - signals.completion_speed)  # faster = higher
    error_penalty = min(1.0, signals.error_count * 0.3)
    match_bonus = 0.3 if signals.outcome_matched_prediction else -0.2
    satisfaction = _clamp(
        (speed_bonus + match_bonus - error_penalty) * m.satisfaction_weight
    )

    # --- Frustration ---
    # Errors + escalation + prediction mismatch → frustration
    escalation_factor = 0.4 if signals.was_escalated else 0.0
    mismatch_factor = 0.3 if not signals.outcome_matched_prediction else 0.0
    frustration = _clamp(
        (signals.error_count * 0.25 + escalation_factor + mismatch_factor)
        * m.frustration_weight
    )

    # --- Curiosity ---
    # Novel tasks + prediction errors → curiosity
    novelty = max(0.0, 1.0 - signals.familiarity_at_execution)
    prediction_surprise = 0.3 if not signals.outcome_matched_prediction else 0.0
    curiosity = _clamp(
        (novelty * 0.7 + prediction_surprise) * m.curiosity_weight
    )

    # --- Confidence ---
    # Familiar tasks + success → confidence; errors erode it
    confidence = _clamp(
        (signals.familiarity_at_execution * 0.6
         + (0.3 if signals.outcome_matched_prediction else -0.3)
         - signals.error_count * 0.2)
        * m.confidence_weight
    )

    # --- Caution ---
    # Past negative valence (via familiarity) + errors → caution
    caution_base = 0.0
    if signals.error_count > 0 and signals.familiarity_at_execution > 0.3:
        caution_base = 0.4  # errors on familiar tasks = extra caution
    if signals.was_escalated:
        caution_base += 0.2
    caution = _clamp(caution_base * m.caution_weight)

    return EmotionDimensions(
        satisfaction=satisfaction,
        frustration=frustration,
        curiosity=curiosity,
        confidence=confidence,
        caution=caution,
    )


# ---------------------------------------------------------------------------
# Rolling per-agent emotional state
# ---------------------------------------------------------------------------

EMOTIONS_FILENAME = "emotions.json"

_BLEND_ALPHA = 0.4
"""Weight of the newest task's emotions vs accumulated state. 0.4 means
a single bad task visibly moves the needle; three in a row dominate it —
emotional state tracks the recent day, not the whole career (that's what
memory's emotion_dimensions records are for)."""


def blend_emotions(
    current: EmotionDimensions,
    new: EmotionDimensions,
    alpha: float = _BLEND_ALPHA,
) -> EmotionDimensions:
    """Exponentially blend a new task's emotions into the rolling state."""
    def mix(a: float, b: float) -> float:
        return _clamp(a * (1.0 - alpha) + b * alpha)

    return EmotionDimensions(
        satisfaction=mix(current.satisfaction, new.satisfaction),
        frustration=mix(current.frustration, new.frustration),
        curiosity=mix(current.curiosity, new.curiosity),
        confidence=mix(current.confidence, new.confidence),
        caution=mix(current.caution, new.caution),
    )


def parse_persona_modifiers(soul_text: str) -> PersonaModifiers:
    """Extract PersonaModifiers from soul.md's YAML front-matter.

    Graceful: missing file content, missing front-matter, or a missing
    ``emotional_modifiers`` block all yield neutral modifiers.
    """
    if not soul_text or not soul_text.startswith("---"):
        return PersonaModifiers()
    parts = soul_text.split("---", 2)
    if len(parts) < 3:
        return PersonaModifiers()
    try:
        import yaml

        front = yaml.safe_load(parts[1]) or {}
    except Exception:
        return PersonaModifiers()
    block = front.get("emotional_modifiers") or front.get("disposition") or {}
    if not isinstance(block, dict):
        return PersonaModifiers()
    return PersonaModifiers.from_dict(block)


def signals_from_task(task: Any, familiarity: Any) -> TaskSignals:
    """Build TaskSignals from a completed/failed Task + familiarity.

    Approximations, documented: ``completion_speed`` is neutral (the
    framework doesn't predict durations yet); ``outcome_matched_
    prediction`` is inferred from success (the reflection suffix's
    prediction_error refines memory records, not this live signal).
    """
    failed = getattr(task, "status", "") == "exception"
    strength = getattr(familiarity, "strength", "novel")
    fam_value = {"routine": 0.9, "familiar": 0.6}.get(strength, 0.2)
    return TaskSignals(
        completion_speed=1.0,
        error_count=1 if failed else 0,
        was_escalated=failed,
        outcome_matched_prediction=not failed,
        familiarity_at_execution=fam_value,
    )
