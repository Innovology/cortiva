"""Tests for the culture-health measurement instrument."""

from __future__ import annotations

from cortiva.culture import CultureMember, assess_culture_health


def _members(*ids: str) -> list[CultureMember]:
    return [CultureMember(agent_id=i, name=i.title()) for i in ids]


def _happy(aid: str = "") -> dict[str, float]:
    return {
        "satisfaction": 0.6,
        "frustration": 0.0,
        "curiosity": 0.4,
        "confidence": 0.6,
        "caution": 0.1,
    }


def test_healthy_workforce_scores_high():
    members = _members("a", "b", "c")
    emotions = {m.agent_id: _happy() for m in members}
    h = assess_culture_health(members, emotions)
    assert h.culture_score >= 90
    assert not h.distressed and not h.burnout_risk and not h.fearful
    assert "Culture health" in h.summary


def test_no_signal_is_handled():
    members = _members("a", "b")
    h = assess_culture_health(members, {})
    assert "No emotional signal" in h.summary
    # score stays at the optimistic baseline (nothing measured against it)
    assert h.culture_score == 100.0


def test_distress_detected_and_penalised():
    members = _members("a", "b")
    emotions = {
        "a": {
            "satisfaction": 0.3,
            "frustration": 0.7,
            "curiosity": 0.3,
            "confidence": 0.3,
            "caution": 0.2,
        },
        "b": _happy(),
    }
    h = assess_culture_health(members, emotions)
    assert "a" in h.distressed
    assert h.culture_score < 100
    assert h.hotspots[0].kind in {"distress", "burnout"}
    assert h.hotspots[0].agent_id == "a"


def test_burnout_is_distress_plus_low_satisfaction():
    members = _members("a")
    emotions = {
        "a": {
            "satisfaction": -0.4,
            "frustration": 0.7,
            "curiosity": 0.2,
            "confidence": 0.2,
            "caution": 0.2,
        },
    }
    h = assess_culture_health(members, emotions)
    assert "a" in h.distressed
    assert "a" in h.burnout_risk


def test_fear_signal_from_high_caution():
    members = _members("a", "b")
    emotions = {
        "a": {
            "satisfaction": 0.3,
            "frustration": 0.2,
            "curiosity": 0.3,
            "confidence": 0.3,
            "caution": 0.7,
        },
        "b": _happy(),
    }
    h = assess_culture_health(members, emotions)
    assert "a" in h.fearful
    assert any(hs.kind == "fear" for hs in h.hotspots)


def test_disengagement_when_everything_flat():
    members = _members("a")
    emotions = {
        "a": {
            "satisfaction": 0.05,
            "frustration": 0.1,
            "curiosity": 0.0,
            "confidence": 0.05,
            "caution": 0.1,
        },
    }
    h = assess_culture_health(members, emotions)
    assert "a" in h.disengaged


def test_unheard_voice_from_comms():
    members = _members("loud1", "loud2", "quiet")
    emotions = {m.agent_id: _happy() for m in members}
    # loud1<->loud2 talk a lot; quiet barely participates
    comms = {("loud1", "loud2"): 20, ("loud2", "quiet"): 1}
    h = assess_culture_health(members, emotions, comms=comms)
    assert "quiet" in h.unheard


def test_monoculture_when_one_voice_dominates():
    members = _members("dom", "b", "c", "d")
    emotions = {m.agent_id: _happy() for m in members}
    # dom is in nearly every conversation
    comms = {("dom", "b"): 10, ("dom", "c"): 10, ("dom", "d"): 10, ("b", "c"): 1}
    h = assess_culture_health(members, emotions, comms=comms)
    assert h.monoculture
    assert any(hs.kind == "monoculture" for hs in h.hotspots)


def test_sparse_comms_below_floor_is_ignored():
    members = _members("a", "b", "c")
    emotions = {m.agent_id: _happy() for m in members}
    comms = {("a", "b"): 1}  # below _VOICE_FLOOR
    h = assess_culture_health(members, emotions, comms=comms)
    # too little data to call anyone unheard or a monoculture
    assert not h.unheard
    assert not h.monoculture


def test_net_negative_mood_triggers_wellbeing_hotspot():
    members = _members("a", "b")
    emotions = {
        "a": {
            "satisfaction": -0.3,
            "frustration": 0.6,
            "curiosity": 0.2,
            "confidence": 0.2,
            "caution": 0.2,
        },
        "b": {
            "satisfaction": -0.2,
            "frustration": 0.5,
            "curiosity": 0.2,
            "confidence": 0.2,
            "caution": 0.2,
        },
    }
    h = assess_culture_health(members, emotions)
    assert h.mean_satisfaction < 0 < h.mean_frustration
    assert any(hs.kind == "wellbeing" for hs in h.hotspots)


def test_hotspots_ranked_by_severity():
    members = _members("a", "b")
    emotions = {
        "a": {
            "satisfaction": -0.5,
            "frustration": 0.9,
            "curiosity": 0.2,
            "confidence": 0.1,
            "caution": 0.6,
        },
        "b": _happy(),
    }
    h = assess_culture_health(members, emotions)
    sevs = [hs.severity for hs in h.hotspots]
    assert sevs == sorted(sevs, reverse=True)


def test_score_is_bounded():
    members = _members(*[f"a{i}" for i in range(8)])
    emotions = {
        m.agent_id: {
            "satisfaction": -0.9,
            "frustration": 1.0,
            "curiosity": 0.0,
            "confidence": 0.0,
            "caution": 0.9,
        }
        for m in members
    }
    h = assess_culture_health(members, emotions)
    assert 0.0 <= h.culture_score <= 100.0


def test_to_dict_serialises():
    members = _members("a")
    emotions = {
        "a": {
            "satisfaction": 0.3,
            "frustration": 0.7,
            "curiosity": 0.3,
            "confidence": 0.3,
            "caution": 0.2,
        }
    }
    d = assess_culture_health(members, emotions).to_dict()
    assert set(d) >= {"culture_score", "distressed", "hotspots", "summary"}
    assert isinstance(d["hotspots"], list)
