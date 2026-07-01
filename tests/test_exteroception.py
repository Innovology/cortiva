"""Exteroception — affect that answers to the world's response, not just own execution.

Myelin's emotions (incl. confidence) were computed purely from task execution
("did it throw?"), so confidence measured activity not quality and criticism
could never land. These tests pin the two new affective inputs: feedback-derived
emotions (a founder's rebuke stings; a stranger's flattery is inert) and the
reality drag (confidence pulled toward outcomes, bounded, released on resolution).
"""

from cortiva.core.emotions import (
    DRAG_MAX,
    EmotionDimensions,
    FeedbackSignal,
    PersonaModifiers,
    blend_emotions,
    classify_feedback,
    emotions_from_feedback,
    reality_drag_dimensions,
)

# --- Phase 2: deterministic valence classifier -----------------------------

# The real founder rebuke that exposed the closed loop.
_FOUNDER_REBUKE = (
    "Maren — Head-in-hands. For SailCoach, our lead product, do we only have a "
    "decision matrix? On MarketMesh, a product I asked to halt, you tell me about "
    "PRs open for a month. On EP, what unit economics are you waiting on? It's been "
    "over a week. Glad you think it's going well... it doesn't look that way from here."
)


def test_classifier_flags_the_founder_rebuke_as_strongly_negative():
    valence, severity, conf = classify_feedback("Re: state of play", _FOUNDER_REBUKE)
    assert valence < -0.5
    assert severity > 0.6
    assert conf > 0.6


def test_classifier_reads_praise_as_positive():
    valence, _sev, _conf = classify_feedback(
        "Re: launch", "Great work on the release — exactly right, thank you."
    )
    assert valence > 0.5


def test_classifier_neutral_mail_is_inert():
    valence, severity, conf = classify_feedback(
        "FYI", "Sharing the weekly metrics export, attached for your records."
    )
    assert abs(valence) < 0.2 and severity < 0.4
    assert conf < 0.4  # not confident there's any valence


def test_classifier_question_pileon_reads_as_pressure():
    valence, _s, _c = classify_feedback(
        "?", "Where is this? Why isn't it done? What happened to the plan?"
    )
    assert valence < 0.0


def test_empty_mail_is_zero():
    assert classify_feedback("", "") == (0.0, 0.0, 0.0)


# --- feedback → emotion mapping (Phase 1) ----------------------------------


def test_founder_criticism_stings_and_drops_confidence():
    e = emotions_from_feedback(
        FeedbackSignal(valence=-0.8, severity=0.9, authority_weight=1.0)
    )
    assert e.frustration > 0.3
    assert e.confidence < -0.2  # criticism erodes confidence regardless of authority
    assert e.caution > 0.0
    assert e.satisfaction < 0.0


def test_low_authority_praise_cannot_inflate_confidence():
    """Flattery from a peer/stranger may not pump self-regard."""
    e = emotions_from_feedback(
        FeedbackSignal(valence=0.9, severity=0.9, authority_weight=0.3)
    )
    assert e.confidence == 0.0  # praise→confidence gated to authority >= 0.7
    assert e.satisfaction >= 0.0


def test_authority_praise_may_lift_confidence():
    e = emotions_from_feedback(
        FeedbackSignal(valence=0.9, severity=0.9, authority_weight=1.0)
    )
    assert e.confidence > 0.0


def test_negativity_bias_criticism_outweighs_equal_praise():
    crit = emotions_from_feedback(FeedbackSignal(valence=-0.7, severity=0.8, authority_weight=1.0))
    praise = emotions_from_feedback(FeedbackSignal(valence=0.7, severity=0.8, authority_weight=1.0))
    assert abs(crit.confidence) > abs(praise.confidence)


def test_low_classifier_confidence_damps_affect():
    sure = emotions_from_feedback(FeedbackSignal(valence=-0.8, severity=0.9, authority_weight=1.0, classifier_confidence=1.0))
    unsure = emotions_from_feedback(FeedbackSignal(valence=-0.8, severity=0.9, authority_weight=1.0, classifier_confidence=0.2))
    assert abs(unsure.frustration) < abs(sure.frustration)


def test_per_event_cap_bounds_a_single_message():
    e = emotions_from_feedback(
        FeedbackSignal(valence=-1.0, severity=1.0, authority_weight=1.5, classifier_confidence=1.0),
        PersonaModifiers(frustration_weight=2.0),
    )
    assert -0.5 <= e.frustration <= 0.5  # cap holds even with max weights + persona


def test_persona_thick_skin_dampens_frustration():
    thin = emotions_from_feedback(FeedbackSignal(valence=-0.6, severity=0.8, authority_weight=1.0), PersonaModifiers(frustration_weight=1.5))
    thick = emotions_from_feedback(FeedbackSignal(valence=-0.6, severity=0.8, authority_weight=1.0), PersonaModifiers(frustration_weight=0.4))
    assert thick.frustration < thin.frustration


# --- reality drag (Phase 3) ------------------------------------------------


def test_drag_pulls_confidence_negative_and_is_bounded():
    d = reality_drag_dimensions(0.5)
    assert d.confidence == -0.5
    assert d.caution > 0
    assert d.satisfaction < 0
    capped = reality_drag_dimensions(99.0)
    assert capped.confidence == -DRAG_MAX  # never floors the agent


def test_drag_humbles_a_confident_agent_without_flooring():
    confident = EmotionDimensions(confidence=1.0, satisfaction=0.6)
    after = blend_emotions(confident, reality_drag_dimensions(0.6))
    assert after.confidence < 0.6  # visibly humbled
    assert after.confidence > -1.0  # not slammed to the floor in one wake


def test_zero_drag_is_inert():
    d = reality_drag_dimensions(0.0)
    assert d.confidence == 0.0 and d.caution == 0.0
