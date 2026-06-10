"""Model-contention dimension of schedule health (overlap vs contention)."""

from __future__ import annotations

from cortiva.scheduling import (
    AgentSpec,
    RoleType,
    WorkWindow,
    assess_schedule_health,
    recommend_schedule_change,
)


def _ics(n: int) -> list[AgentSpec]:
    return [AgentSpec(agent_id=f"a{i}", role_type=RoleType.IC) for i in range(n)]


def test_peak_concurrency_is_reported_without_capacity():
    agents = _ics(5)
    # all awake 09:00-17:00 — peak 5
    scheds = {a.agent_id: [WorkWindow(9.0, 17.0)] for a in agents}
    h = assess_schedule_health(agents, scheds)
    assert h.peak_concurrency == 5
    assert h.model_concurrency is None
    assert h.contended_hours == 0.0  # no capacity → not assessed


def test_contention_penalised_when_overlap_exceeds_capacity():
    agents = _ics(6)
    scheds = {a.agent_id: [WorkWindow(9.0, 13.0)] for a in agents}  # all 6 overlap 4h
    free = assess_schedule_health(agents, scheds)  # no cap
    capped = assess_schedule_health(agents, scheds, model_concurrency=2)
    assert capped.peak_concurrency == 6
    assert capped.contended_hours > 0
    assert capped.responsiveness_score < free.responsiveness_score  # contention costs
    assert any(hs.kind == "contention" for hs in capped.hotspots)


def test_within_capacity_no_contention():
    agents = _ics(2)
    scheds = {a.agent_id: [WorkWindow(9.0, 13.0)] for a in agents}
    h = assess_schedule_health(agents, scheds, model_concurrency=3)
    assert h.contended_hours == 0.0
    assert not any(hs.kind == "contention" for hs in h.hotspots)


def test_contention_hotspot_targets_a_non_manager():
    # one manager + reports, all overlapping; the contention fix should prefer
    # staggering an IC, not the load-bearing manager.
    mgr = AgentSpec(agent_id="mgr", role_type=RoleType.MANAGER, reports=["a0", "a1", "a2"])
    ics = [AgentSpec(agent_id=f"a{i}", role_type=RoleType.IC, manager="mgr") for i in range(3)]
    agents = [mgr, *ics]
    scheds = {a.agent_id: [WorkWindow(9.0, 12.0)] for a in agents}
    h = assess_schedule_health(agents, scheds, model_concurrency=1)
    cont = [hs for hs in h.hotspots if hs.kind == "contention"]
    assert cont and cont[0].agent_id != "mgr"  # not the manager


def test_recommend_trades_overlap_for_contention():
    # 4 ICs stacked in one slot, model serves 1 → recommend should spread one out.
    agents = _ics(4)
    scheds = {a.agent_id: [WorkWindow(9.0, 11.0)] for a in agents}
    rec = recommend_schedule_change(agents, scheds, target="a0", model_concurrency=1)
    # moving a0 out of the stack should not make things worse, and should help
    assert rec.score_after >= rec.score_before


def test_to_dict_carries_contention_fields():
    agents = _ics(3)
    scheds = {a.agent_id: [WorkWindow(9.0, 13.0)] for a in agents}
    d = assess_schedule_health(agents, scheds, model_concurrency=1).to_dict()
    assert "peak_concurrency" in d and "contended_hours" in d and "model_concurrency" in d
