"""Schedule-health measurement — the AR Scheduler's responsiveness readout."""

from __future__ import annotations

from cortiva.scheduling import (
    AgentSpec,
    RoleType,
    Signals,
    WorkWindow,
    assess_schedule_health,
)


def _w(start, end):
    return WorkWindow(start, end)


def test_seamless_24x7_with_overlap_scores_near_perfect():
    # Three tiling shifts cover the clock; manager overlaps the report.
    agents = [
        AgentSpec("mgr", RoleType.MANAGER, reports=["a", "b"]),
        AgentSpec("a", RoleType.IC, manager="mgr"),
        AgentSpec("b", RoleType.IC, manager="mgr"),
    ]
    schedules = {
        "mgr": [_w(0, 8), _w(8, 16), _w(16, 24)],  # manager spans all shifts
        "a": [_w(0, 8)],
        "b": [_w(0, 8)],
    }
    h = assess_schedule_health(agents, schedules)
    assert h.uncovered_hours == 0.0
    assert h.oversight_gaps == []
    assert h.responsiveness_score == 100.0
    assert h.hotspots == []


def test_detects_coverage_gap():
    agents = [AgentSpec("a", RoleType.IC)]
    # Awake only 09–17 → 16h of the day uncovered (17→09 wrapping).
    h = assess_schedule_health(agents, {"a": [_w(9, 17)]})
    assert h.uncovered_hours == 16.0
    assert h.responsiveness_score < 100.0
    # Top hotspot is the coverage hole.
    assert h.hotspots[0].kind == "coverage"


def test_detects_oversight_gap_and_points_at_the_manager():
    agents = [
        AgentSpec("mgr", RoleType.MANAGER, reports=["rep"]),
        AgentSpec("rep", RoleType.IC, manager="mgr"),
    ]
    # Manager awake 0–8, report awake 16–24 → never overlap.
    h = assess_schedule_health(agents, {"mgr": [_w(0, 8)], "rep": [_w(16, 24)]})
    assert {"report": "rep", "manager": "mgr"} in h.oversight_gaps
    over = [hs for hs in h.hotspots if hs.kind == "oversight"]
    assert over and over[0].agent_id == "mgr"  # act on the manager's schedule


def test_detects_isolated_peer():
    agents = [
        AgentSpec("mgr", RoleType.MANAGER, reports=["x", "y"]),
        AgentSpec("x", RoleType.IC, manager="mgr"),
        AgentSpec("y", RoleType.IC, manager="mgr"),
    ]
    # x and y are peers but never overlap each other.
    schedules = {"mgr": [_w(0, 24)], "x": [_w(0, 8)], "y": [_w(16, 24)]}
    h = assess_schedule_health(agents, schedules)
    assert set(h.isolated_agents) == {"x", "y"}
    assert any(hs.kind == "isolation" for hs in h.hotspots)


def test_chronic_overtime_is_a_secondary_signal():
    agents = [AgentSpec("a", RoleType.IC), AgentSpec("b", RoleType.IC)]
    schedules = {"a": [_w(0, 12)], "b": [_w(12, 24)]}  # full coverage
    h = assess_schedule_health(
        agents, schedules, signals=Signals(overtime_hours={"a": 3.0, "b": 0.5})
    )
    assert h.chronic_overtime == ["a"]  # >=2h chronic
    assert "b" not in h.chronic_overtime
    assert any(hs.kind == "overtime" and hs.agent_id == "a" for hs in h.hotspots)


def test_hotspots_ranked_worst_first_so_vera_picks_one_role():
    agents = [
        AgentSpec("mgr", RoleType.MANAGER, reports=["rep"]),
        AgentSpec("rep", RoleType.IC, manager="mgr"),
    ]
    # A big coverage hole (severity scales with hours) outranks one
    # oversight gap → Vera addresses coverage first.
    h = assess_schedule_health(agents, {"mgr": [_w(0, 2)], "rep": [_w(0, 2)]})
    assert h.hotspots[0].kind == "coverage"
    # severities are non-increasing
    sev = [hs.severity for hs in h.hotspots]
    assert sev == sorted(sev, reverse=True)


# --- Single-role recommendation -------------------------------------------

from cortiva.scheduling import recommend_schedule_change  # noqa: E402


def test_recommends_retiming_the_role_that_closes_the_worst_gap():
    # rep is on a shift that never overlaps its manager → oversight gap.
    # Re-timing rep onto the manager's shift should fix it and raise the score.
    agents = [
        AgentSpec("mgr", RoleType.MANAGER, reports=["rep"]),
        AgentSpec("rep", RoleType.IC, manager="mgr"),
        AgentSpec("other", RoleType.IC),
    ]
    schedules = {
        "mgr": [_w(0, 8)],
        "rep": [_w(16, 24)],  # never overlaps mgr
        "other": [_w(8, 16)],  # holds coverage
    }
    before = assess_schedule_health(agents, schedules).responsiveness_score
    rec = recommend_schedule_change(agents, schedules)
    assert rec.target == "mgr" or rec.target == "rep"  # owner of the worst hotspot
    assert rec.score_after > before
    assert rec.delta > 0
    assert rec.recommended_windows != rec.current_windows


def test_recommendation_is_a_noop_when_already_optimal():
    # Two ICs already tiling the day with no gaps/oversight issues.
    agents = [AgentSpec("a", RoleType.IC), AgentSpec("b", RoleType.IC)]
    schedules = {"a": [_w(0, 12)], "b": [_w(12, 24)]}
    rec = recommend_schedule_change(agents, schedules, target="a")
    assert rec.delta <= 0
    assert rec.recommended_windows == rec.current_windows
    assert "near-optimal" in rec.rationale or "no re-timing" in rec.rationale


def test_recommendation_targets_an_explicit_role():
    agents = [
        AgentSpec("mgr", RoleType.MANAGER, reports=["rep"]),
        AgentSpec("rep", RoleType.IC, manager="mgr"),
        AgentSpec("filler", RoleType.IC),
    ]
    schedules = {"mgr": [_w(0, 8)], "rep": [_w(12, 20)], "filler": [_w(8, 16)]}
    rec = recommend_schedule_change(agents, schedules, target="rep")
    assert rec.target == "rep"
