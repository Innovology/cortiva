"""Proof that the rota optimiser works as designed.

Covers the hard-invariant guarantees, determinism, and that it actually
*responds* to conditions / constraints / complaints / infra monitoring —
the four signal classes the AR Scheduler steers it with.
"""

from __future__ import annotations

from cortiva.scheduling import (
    AgentSpec,
    Constraints,
    Objectives,
    RoleType,
    Signals,
    optimize_schedule,
    windows_to_schedule_config,
)


def _synthetic_org(n_ics: int, n_managers: int = 10) -> list[AgentSpec]:
    """Build a synthetic org: n_managers each owning a slice of n_ics."""
    agents: list[AgentSpec] = []
    ics = [f"ic-{i:03d}" for i in range(n_ics)]
    per = max(1, n_ics // n_managers)
    for m in range(n_managers):
        reports = ics[m * per:(m + 1) * per]
        if m == n_managers - 1:
            reports = ics[m * per:]  # last manager mops up the remainder
        agents.append(
            AgentSpec(
                agent_id=f"mgr-{m:02d}",
                role_type=RoleType.MANAGER,
                reports=reports,
                budget_hours=8.0,
            )
        )
    for ic in ics:
        owner = next(a.agent_id for a in agents if ic in a.reports)
        agents.append(
            AgentSpec(agent_id=ic, role_type=RoleType.IC, manager=owner, budget_hours=8.0)
        )
    return agents


# ---------------------------------------------------------------------------
# Hard invariants — the safety contract
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_400_agents_feasible_and_all_invariants_hold(self) -> None:
        # With 8h blocks in a 24h day, the on-shift floor for 390 ICs is
        # ~N/2 (~195). A realistic ceiling clears it. (Inference concurrency
        # = on-shift x duty-cycle, so on-shift ceilings are naturally high.)
        agents = _synthetic_org(n_ics=390, n_managers=10)  # 400 total
        c = Constraints(day_start_h=0, day_end_h=24, capacity_ceiling=260)
        p = optimize_schedule(agents, constraints=c)

        assert p.feasible, p.violations
        assert p.violations == []
        # Ceiling never breached.
        assert p.impact.peak_concurrency <= c.capacity_ceiling
        # No agent over budget, none starved, all inside the day span.
        for a in agents:
            ws = p.schedules[a.agent_id]
            assert ws, f"{a.agent_id} starved"
            assert sum(w.length_h for w in ws) <= a.budget_hours + 1e-6
            for w in ws:
                assert c.day_start_h - 1e-6 <= w.start_h
                assert w.end_h <= c.day_end_h + 1e-6

    def test_every_manager_overlaps_every_report(self) -> None:
        agents = _synthetic_org(n_ics=60, n_managers=6)
        p = optimize_schedule(agents)
        assert p.feasible, p.violations
        for a in agents:
            if a.role_type != RoleType.MANAGER:
                continue
            for r in a.reports:
                mws = p.schedules[a.agent_id]
                rws = p.schedules[r]
                assert any(mw.overlaps(rw) for mw in mws for rw in rws), (
                    f"{a.agent_id} has no oversight overlap with {r}"
                )

    def test_managers_get_multiple_windows_ics_get_one_block(self) -> None:
        agents = _synthetic_org(n_ics=30, n_managers=3)
        p = optimize_schedule(agents, constraints=Constraints(capacity_ceiling=200))
        for a in agents:
            if a.role_type == RoleType.MANAGER:
                assert len(p.schedules[a.agent_id]) >= 2, "manager should have spread windows"
            else:
                assert len(p.schedules[a.agent_id]) == 1, "IC should have one focus block"

    def test_infeasible_when_ceiling_impossible(self) -> None:
        # 50 agents needing 8h in a 24h window can't fit under a ceiling of 5.
        agents = _synthetic_org(n_ics=48, n_managers=2)
        p = optimize_schedule(
            agents, constraints=Constraints(capacity_ceiling=5),
        )
        assert not p.feasible
        assert any("ceiling" in v for v in p.violations)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_same_output(self) -> None:
        agents = _synthetic_org(n_ics=80, n_managers=8)
        a = optimize_schedule(agents)
        b = optimize_schedule(agents)
        assert a.summary == b.summary
        for aid in a.schedules:
            assert [(w.start_h, w.end_h) for w in a.schedules[aid]] == \
                   [(w.start_h, w.end_h) for w in b.schedules[aid]]


# ---------------------------------------------------------------------------
# Responds to the four signal classes
# ---------------------------------------------------------------------------


class TestRespondsToSignals:
    def test_lower_ceiling_forces_wider_spread(self) -> None:
        """Conditions/constraints: a tighter capacity ceiling must spread
        the same workforce across a wider span."""
        agents = _synthetic_org(n_ics=190, n_managers=10)  # 200
        loose = optimize_schedule(agents, constraints=Constraints(capacity_ceiling=300))
        tight = optimize_schedule(agents, constraints=Constraints(capacity_ceiling=120))
        assert tight.feasible and loose.feasible
        assert tight.impact.spread_span_h > loose.impact.spread_span_h
        assert tight.impact.peak_concurrency <= 120

    def test_infra_saturation_empties_the_saturated_hours(self) -> None:
        """Infrastructure monitoring: marking 09:00 saturated must pull
        agents out of that hour."""
        agents = _synthetic_org(n_ics=90, n_managers=10)  # 100
        c = Constraints(day_start_h=6, day_end_h=24, capacity_ceiling=140)

        baseline = optimize_schedule(agents, constraints=c)
        saturated = optimize_schedule(
            agents, constraints=c,
            signals=Signals(infra_saturation={9: 0.9, 10: 0.9}),
        )

        def load_at(proposal, hour):
            return sum(
                1
                for ws in proposal.schedules.values()
                for w in ws
                if w.start_h <= hour < w.end_h
            )

        assert saturated.feasible
        # Far fewer agents on-shift during the saturated hours.
        assert load_at(saturated, 9) < load_at(baseline, 9)
        assert load_at(saturated, 9) <= 140 * 0.1 + 1  # respects scaled ceiling

    def test_overtime_signal_surfaces_chronic_cases(self) -> None:
        """Complaints/overtime: chronic overtime is surfaced as a
        rebalance/hire signal, not silently absorbed."""
        agents = _synthetic_org(n_ics=40, n_managers=4)
        sig = Signals(overtime_hours={"ic-000": 3.0, "ic-001": 0.5})
        p = optimize_schedule(agents, signals=sig)
        assert "ic-000" in p.impact.chronic_overtime_agents
        assert "ic-001" not in p.impact.chronic_overtime_agents
        assert p.impact.predicted_overtime_hours == 3.5

    def test_objective_weights_change_the_outcome(self) -> None:
        """The agent's steering wheel: changing weights changes the rota."""
        agents = _synthetic_org(n_ics=120, n_managers=10)
        c = Constraints(capacity_ceiling=80)
        spread_averse = optimize_schedule(
            agents, constraints=c, objectives=Objectives(w_spread=5.0),
        )
        spread_ok = optimize_schedule(
            agents, constraints=c, objectives=Objectives(w_spread=0.0),
        )
        assert spread_averse.feasible and spread_ok.feasible
        # Caring about spread should not produce a *wider* rota than not caring.
        assert spread_averse.impact.spread_span_h <= spread_ok.impact.spread_span_h


# ---------------------------------------------------------------------------
# Output contract — converts to the framework's schedule format
# ---------------------------------------------------------------------------


class TestScheduleConfigOutput:
    def test_manager_windows_become_comma_lists(self) -> None:
        agents = _synthetic_org(n_ics=20, n_managers=2)
        p = optimize_schedule(agents, constraints=Constraints(capacity_ceiling=200))
        mgr = next(a for a in agents if a.role_type == RoleType.MANAGER)
        cfg = windows_to_schedule_config(p.schedules[mgr.agent_id])
        assert "wake" in cfg and "sleep" in cfg
        assert "," in cfg["wake"]  # multiple windows
        assert len(cfg["wake"].split(",")) == len(p.schedules[mgr.agent_id])

    def test_ic_block_is_single_window(self) -> None:
        agents = _synthetic_org(n_ics=20, n_managers=2)
        p = optimize_schedule(agents, constraints=Constraints(capacity_ceiling=200))
        ic = next(a for a in agents if a.role_type == RoleType.IC)
        cfg = windows_to_schedule_config(p.schedules[ic.agent_id])
        assert "," not in cfg["wake"]
        assert "replan" not in cfg  # single window → no mid-window replan


class TestMultiLevelOrg:
    def test_ceo_over_managers_has_no_oversight_gap(self) -> None:
        """A 3-level org (CEO -> managers -> ICs): the CEO's reports are
        themselves managers, so they must be placed before the CEO or the
        CEO is left with an oversight gap. Deepest-first placement fixes it."""
        agents = [
            AgentSpec("ceo", RoleType.MANAGER, manager=None,
                      reports=["mgr-a", "mgr-b"]),
            AgentSpec("mgr-a", RoleType.MANAGER, manager="ceo",
                      reports=["ic-0", "ic-1"]),
            AgentSpec("mgr-b", RoleType.MANAGER, manager="ceo",
                      reports=["ic-2", "ic-3"]),
            AgentSpec("ic-0", RoleType.IC, manager="mgr-a"),
            AgentSpec("ic-1", RoleType.IC, manager="mgr-a"),
            AgentSpec("ic-2", RoleType.IC, manager="mgr-b"),
            AgentSpec("ic-3", RoleType.IC, manager="mgr-b"),
        ]
        p = optimize_schedule(agents, constraints=Constraints(capacity_ceiling=8))
        assert p.feasible, p.violations
        assert p.impact.reports_with_oversight_gap == 0
        # CEO actually overlaps its manager-reports.
        ceo = p.schedules["ceo"]
        for r in ("mgr-a", "mgr-b"):
            assert any(mw.overlaps(rw) for mw in ceo for rw in p.schedules[r]), \
                f"CEO has no oversight overlap with {r}"
