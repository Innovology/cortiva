"""Tests for the agent-efficiency measurement instrument."""

from __future__ import annotations

from cortiva.workforce import AgentEfficiencyInput, assess_workforce_efficiency


def _good(aid="a", **kw):
    base = dict(
        agent_id=aid, name=aid.title(), tasks_completed=16, tasks_escalated=1,
        active_hours=7.5, scheduled_hours=7.5, prediction_accuracy=0.8,
        cost_gbp=2.0, satisfaction=0.5, frustration=0.0,
    )
    base.update(kw)
    return AgentEfficiencyInput(**base)


def test_healthy_agent_scores_well():
    r = assess_workforce_efficiency([_good()])
    a = r.per_agent[0]
    assert a.score > 60
    assert 0.0 <= a.quality <= 1.0 and 0.0 <= a.sustainability <= 1.0
    assert a.throughput == 16 / 7.5
    assert "Workforce efficiency" in r.summary


def test_high_escalation_tanks_quality():
    # escalation AND poor prediction (the two halves of quality) both low
    a = assess_workforce_efficiency(
        [_good(tasks_completed=4, tasks_escalated=12, prediction_accuracy=0.2)]
    ).per_agent[0]
    assert a.quality < 0.5


def test_overwork_hurts_sustainability():
    base = assess_workforce_efficiency([_good()]).per_agent[0]
    over = assess_workforce_efficiency([_good(active_hours=15.0)]).per_agent[0]
    assert over.sustainability < base.sustainability


def test_burnout_mood_hurts_sustainability():
    base = assess_workforce_efficiency([_good()]).per_agent[0]
    burnt = assess_workforce_efficiency([_good(satisfaction=-0.5, frustration=0.7)]).per_agent[0]
    assert burnt.sustainability < base.sustainability


def test_trend_from_prior_score():
    a = assess_workforce_efficiency([_good(prior_score=90.0)]).per_agent[0]
    assert a.trend == round(a.score - 90.0, 1) or abs(a.trend - (a.score - 90.0)) < 0.05


def test_declining_agent_flagged():
    # current period much weaker than the agent's strong prior → >10pt drop
    r = assess_workforce_efficiency(
        [_good(aid="dip", tasks_completed=3, tasks_escalated=5,
               prediction_accuracy=0.2, prior_score=90.0)]
    )
    kinds = {h.kind for h in r.hotspots}
    assert "declining" in kinds


def test_at_risk_low_quality_flagged():
    r = assess_workforce_efficiency([_good(tasks_completed=2, tasks_escalated=10, prediction_accuracy=0.1)])
    assert any(h.kind == "at_risk" for h in r.hotspots)


def test_hotspots_rank_problems_before_standouts():
    recs = [
        _good(aid="great", prior_score=70.0),  # high + improving → standout
        _good(aid="dip", prior_score=95.0),    # declining
    ]
    r = assess_workforce_efficiency(recs)
    if r.hotspots:
        # declining/at_risk must come before standout
        first_standout = next((i for i, h in enumerate(r.hotspots) if h.kind == "standout"), len(r.hotspots))
        problems = [i for i, h in enumerate(r.hotspots) if h.kind in ("declining", "at_risk")]
        assert all(p < first_standout for p in problems)


def test_empty_is_safe():
    r = assess_workforce_efficiency([])
    assert r.per_agent == [] and "0 agent" in r.summary


def test_ranked_by_score():
    recs = [_good(aid="low", tasks_completed=2, tasks_escalated=6), _good(aid="high", tasks_completed=20)]
    scores = [a.score for a in assess_workforce_efficiency(recs).per_agent]
    assert scores == sorted(scores, reverse=True)


def test_to_dict_serialises():
    d = assess_workforce_efficiency([_good()]).to_dict()
    assert set(d) >= {"per_agent", "hotspots", "mean_score", "summary"}
