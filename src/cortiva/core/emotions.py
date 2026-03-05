"""
Emotion derivation spec (design only — issue #9).

After each task execution, the subconscious computes emotional dimensions
from task signals weighted by persona modifiers from soul.md.  This is
arithmetic, not LLM inference.

The derived emotions are stored with the experience memory record via
:pyattr:`MemoryRecord.emotion_dimensions` and surface during replan /
reflection to inform autonomous goal generation.

Implementation is planned for v0.2.
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
