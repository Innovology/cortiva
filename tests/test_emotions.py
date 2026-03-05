"""Tests for the emotion derivation spec."""

from __future__ import annotations

from cortiva.core.emotions import (
    EmotionDimensions,
    PersonaModifiers,
    TaskSignals,
    derive_emotions,
)


class TestTaskSignals:
    def test_defaults(self) -> None:
        s = TaskSignals()
        assert s.completion_speed == 0.0
        assert s.error_count == 0
        assert s.was_escalated is False
        assert s.outcome_matched_prediction is True
        assert s.familiarity_at_execution == 0.5


class TestPersonaModifiers:
    def test_defaults(self) -> None:
        m = PersonaModifiers()
        assert m.satisfaction_weight == 1.0
        assert m.frustration_weight == 1.0
        assert m.curiosity_weight == 1.0
        assert m.confidence_weight == 1.0
        assert m.caution_weight == 1.0

    def test_from_dict(self) -> None:
        m = PersonaModifiers.from_dict({
            "satisfaction_weight": 1.5,
            "frustration_weight": 0.5,
            "curiosity_weight": 2.0,
        })
        assert m.satisfaction_weight == 1.5
        assert m.frustration_weight == 0.5
        assert m.curiosity_weight == 2.0
        assert m.confidence_weight == 1.0  # default

    def test_from_dict_empty(self) -> None:
        m = PersonaModifiers.from_dict({})
        assert m.satisfaction_weight == 1.0


class TestEmotionDimensions:
    def test_to_dict(self) -> None:
        e = EmotionDimensions(satisfaction=0.5, frustration=-0.3)
        d = e.to_dict()
        assert d["satisfaction"] == 0.5
        assert d["frustration"] == -0.3
        assert d["curiosity"] == 0.0

    def test_from_dict(self) -> None:
        e = EmotionDimensions.from_dict({"satisfaction": 0.8, "caution": 0.4})
        assert e.satisfaction == 0.8
        assert e.caution == 0.4
        assert e.frustration == 0.0

    def test_roundtrip(self) -> None:
        original = EmotionDimensions(
            satisfaction=0.75,
            frustration=0.2,
            curiosity=-0.1,
            confidence=0.5,
            caution=0.3,
        )
        d = original.to_dict()
        restored = EmotionDimensions.from_dict(d)
        assert restored.satisfaction == original.satisfaction
        assert restored.frustration == original.frustration


class TestDeriveEmotions:
    def test_successful_fast_task(self) -> None:
        """Fast completion, no errors → high satisfaction, low frustration."""
        signals = TaskSignals(
            completion_speed=0.5,
            error_count=0,
            was_escalated=False,
            outcome_matched_prediction=True,
            familiarity_at_execution=0.8,
        )
        emotions = derive_emotions(signals)
        assert emotions.satisfaction > 0.5
        assert emotions.frustration == 0.0
        assert emotions.confidence > 0.0

    def test_failed_task_with_errors(self) -> None:
        """Multiple errors + escalation → frustration, low satisfaction."""
        signals = TaskSignals(
            completion_speed=2.0,
            error_count=3,
            was_escalated=True,
            outcome_matched_prediction=False,
            familiarity_at_execution=0.6,
        )
        emotions = derive_emotions(signals)
        assert emotions.frustration > 0.5
        assert emotions.satisfaction < 0.0
        assert emotions.caution > 0.0

    def test_novel_task(self) -> None:
        """Unfamiliar task → high curiosity."""
        signals = TaskSignals(
            completion_speed=1.0,
            error_count=0,
            familiarity_at_execution=0.0,
        )
        emotions = derive_emotions(signals)
        assert emotions.curiosity > 0.5

    def test_routine_task(self) -> None:
        """Highly familiar successful task → high confidence, low curiosity."""
        signals = TaskSignals(
            completion_speed=0.8,
            error_count=0,
            outcome_matched_prediction=True,
            familiarity_at_execution=1.0,
        )
        emotions = derive_emotions(signals)
        assert emotions.confidence > 0.5
        assert emotions.curiosity < 0.3

    def test_persona_modifiers_amplify(self) -> None:
        """Persona modifiers should scale the dimensions."""
        signals = TaskSignals(
            completion_speed=0.5,
            error_count=0,
            outcome_matched_prediction=True,
        )
        neutral = derive_emotions(signals)
        amplified = derive_emotions(
            signals,
            PersonaModifiers(satisfaction_weight=2.0),
        )
        assert amplified.satisfaction > neutral.satisfaction

    def test_persona_modifiers_dampen(self) -> None:
        """Low modifier weight should reduce the dimension."""
        signals = TaskSignals(
            completion_speed=2.0,
            error_count=2,
            was_escalated=True,
        )
        neutral = derive_emotions(signals)
        dampened = derive_emotions(
            signals,
            PersonaModifiers(frustration_weight=0.3),
        )
        assert dampened.frustration < neutral.frustration

    def test_all_values_clamped(self) -> None:
        """No dimension should exceed [-1.0, 1.0]."""
        # Extreme inputs
        signals = TaskSignals(
            completion_speed=0.0,
            error_count=100,
            was_escalated=True,
            outcome_matched_prediction=False,
            familiarity_at_execution=0.0,
        )
        modifiers = PersonaModifiers(
            satisfaction_weight=2.0,
            frustration_weight=2.0,
            curiosity_weight=2.0,
            confidence_weight=2.0,
            caution_weight=2.0,
        )
        emotions = derive_emotions(signals, modifiers)
        for val in emotions.to_dict().values():
            assert -1.0 <= val <= 1.0

    def test_default_modifiers(self) -> None:
        """Passing None for modifiers should use neutral defaults."""
        signals = TaskSignals()
        e1 = derive_emotions(signals, None)
        e2 = derive_emotions(signals, PersonaModifiers())
        assert e1.to_dict() == e2.to_dict()
