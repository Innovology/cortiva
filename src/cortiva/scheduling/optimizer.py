"""Deterministic workforce-rota optimiser.

This is the *tool* the AR Scheduler operates. Given a workforce (with org
relationships), hard constraints, weighted objectives, and current signals
(overtime, blocked-wait, complaints, infra saturation), it returns a
``ScheduleProposal``: a set of work windows per agent plus a dry-run impact
preview and per-agent rationale.

Design contract — the agent steers, the tool stays safe:

* **Hard invariants are enforced by construction.** A returned proposal
  with ``feasible=True`` is guaranteed to satisfy every hard constraint:
  no agent over its hour budget, every manager's windows overlap each of
  its reports, per-slot concurrency never exceeds the capacity ceiling,
  no agent starved, all windows inside the allowed day span. If the tool
  cannot satisfy the ceiling it returns ``feasible=False`` with the
  violations listed — and the apply path refuses to apply it. The agent
  tunes weights; it can never produce a company-breaking rota.
* **Deterministic.** Same inputs → same output (no randomness; ties broken
  by index). Fully unit-testable offline.

The agent's judgement lives in *which signals it emphasises* (the
``Objectives`` weights) and *when* it re-optimises — not in hand-editing
rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RoleType(str, Enum):
    """How an agent is scheduled.

    * ``IC`` — one continuous focus block (flow, context continuity).
    * ``MANAGER`` — several short availability windows spread across the
      day so they blanket their reports (oversight).
    """

    IC = "ic"
    MANAGER = "manager"


@dataclass
class AgentSpec:
    """An agent as the optimiser sees it."""

    agent_id: str
    role_type: RoleType = RoleType.IC
    manager: str | None = None
    reports: list[str] = field(default_factory=list)
    budget_hours: float = 8.0
    # Soft preference: an hour the agent works best from (0-23), or None.
    preferred_start: int | None = None


@dataclass
class Signals:
    """Current-state signals the optimiser reacts to.

    All optional — an empty ``Signals`` yields a sane default rota.
    """

    # agent_id -> hours of overtime recorded recently (chronic overtime is
    # surfaced as a rebalance/hire signal, not silently absorbed).
    overtime_hours: dict[str, float] = field(default_factory=dict)
    # agent_id -> hours spent blocked waiting on a manager decision.
    blocked_wait_hours: dict[str, float] = field(default_factory=dict)
    # Free-text complaints per agent (e.g. "blocked on manager mornings").
    complaints: dict[str, list[str]] = field(default_factory=dict)
    # Infra saturation per hour-of-day, 0.0 (free) .. 1.0 (saturated).
    # The effective capacity ceiling in an hour is scaled by (1 - sat).
    infra_saturation: dict[int, float] = field(default_factory=dict)


@dataclass
class Constraints:
    """Hard limits the optimiser must respect."""

    day_start_h: float = 0.0
    """Earliest hour any window may start (allows round-the-clock spread)."""
    day_end_h: float = 24.0
    """Latest hour any window may end."""
    slot_minutes: int = 30
    capacity_ceiling: int = 130
    """Max agents concurrently on-shift in any slot (inference capacity)."""
    manager_windows: int = 4
    manager_window_len_h: float = 2.0
    ic_block_len_h: float = 8.0


@dataclass
class Objectives:
    """Soft objective weights — this is the agent's steering wheel."""

    w_peak: float = 1.0
    """Penalise high peak concurrency (cost / herd)."""
    w_blocked: float = 2.0
    """Penalise reports left without manager coverage."""
    w_overtime: float = 1.5
    """Bias overloaded agents toward less-contended, better-covered slots."""
    w_spread: float = 0.5
    """Penalise spreading the day wider than necessary (keep people overlapping)."""
    w_preference: float = 0.5
    """Reward honouring preferred start times."""


@dataclass
class WorkWindow:
    start_h: float
    end_h: float

    @property
    def length_h(self) -> float:
        return self.end_h - self.start_h

    def overlaps(self, other: WorkWindow) -> bool:
        return self.start_h < other.end_h and other.start_h < self.end_h


@dataclass
class ImpactPreview:
    """What applying this proposal would do — shown before anything changes."""

    peak_concurrency: int = 0
    capacity_ceiling: int = 0
    total_scheduled_hours: float = 0.0
    spread_span_h: float = 0.0
    """Wall-clock span from first window start to last window end."""
    reports_with_oversight_gap: int = 0
    max_blocked_report_gap_h: float = 0.0
    predicted_overtime_hours: float = 0.0
    chronic_overtime_agents: list[str] = field(default_factory=list)
    """Agents whose overtime scheduling can't fix — a rebalance/hire signal."""

    def to_dict(self) -> dict:
        return {
            "peak_concurrency": self.peak_concurrency,
            "capacity_ceiling": self.capacity_ceiling,
            "total_scheduled_hours": round(self.total_scheduled_hours, 1),
            "spread_span_h": round(self.spread_span_h, 1),
            "reports_with_oversight_gap": self.reports_with_oversight_gap,
            "max_blocked_report_gap_h": round(self.max_blocked_report_gap_h, 1),
            "predicted_overtime_hours": round(self.predicted_overtime_hours, 1),
            "chronic_overtime_agents": list(self.chronic_overtime_agents),
        }


@dataclass
class ScheduleProposal:
    """The optimiser's output."""

    schedules: dict[str, list[WorkWindow]]
    feasible: bool
    impact: ImpactPreview
    violations: list[str] = field(default_factory=list)
    rationale: dict[str, str] = field(default_factory=dict)
    summary: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slots(c: Constraints) -> int:
    return int(round((c.day_end_h - c.day_start_h) * 60 / c.slot_minutes))


def _slot_to_hour(c: Constraints, slot: int) -> float:
    return c.day_start_h + slot * c.slot_minutes / 60.0


def _hour_to_slot(c: Constraints, hour: float) -> int:
    return int(round((hour - c.day_start_h) * 60 / c.slot_minutes))


def _effective_ceiling(c: Constraints, signals: Signals, slot: int) -> float:
    """Capacity ceiling for a slot, scaled down by infra saturation."""
    hour = int(_slot_to_hour(c, slot)) % 24
    sat = signals.infra_saturation.get(hour, 0.0)
    return c.capacity_ceiling * max(0.0, 1.0 - sat)


def _block_slots(c: Constraints, length_h: float) -> int:
    return max(1, int(round(length_h * 60 / c.slot_minutes)))


# ---------------------------------------------------------------------------
# The optimiser
# ---------------------------------------------------------------------------


def optimize_schedule(
    agents: list[AgentSpec],
    *,
    constraints: Constraints | None = None,
    objectives: Objectives | None = None,
    signals: Signals | None = None,
) -> ScheduleProposal:
    """Produce a feasible, optimised rota for ``agents``.

    Placement strategy (greedy + a coverage pass — deterministic):

    1. Place individual contributors first as one focus block each,
       choosing the start that keeps every covered slot under the
       (saturation-adjusted) ceiling and minimises the resulting peak,
       biased toward clustering early and honouring preferences. Overloaded
       agents (overtime signal) are biased toward less-contended slots.
    2. Place each manager's availability windows across the span of their
       reports so that every report is covered (the oversight invariant)
       and the largest uncovered gap is minimised.
    3. Validate every hard invariant; compute the impact preview.
    """
    c = constraints or Constraints()
    obj = objectives or Objectives()
    sig = signals or Signals()

    n_slots = _slots(c)
    load = [0.0] * n_slots  # per-slot concurrency
    schedules: dict[str, list[WorkWindow]] = {}
    rationale: dict[str, str] = {}

    ics = [a for a in agents if a.role_type == RoleType.IC]
    managers = [a for a in agents if a.role_type == RoleType.MANAGER]
    # Managers are placed after ICs (they need reports' spans first) and add
    # load. Reserve worst-case manager headroom in the IC ceiling so the
    # combined peak can never breach, whatever the coverage repair does.
    mgr_reserve = len(managers)

    # --- 1. Place ICs (the bulk) as single focus blocks --------------------
    # Order: most-overloaded first (they get first pick of good slots), then
    # by preferred_start, then by id for determinism.
    def _ic_key(a: AgentSpec) -> tuple:
        ot = sig.overtime_hours.get(a.agent_id, 0.0)
        pref = a.preferred_start if a.preferred_start is not None else 99
        return (-ot, pref, a.agent_id)

    for a in sorted(ics, key=_ic_key):
        block = _block_slots(c, min(a.budget_hours, c.ic_block_len_h))
        start_slot = _best_block_start(
            c, obj, sig, load, block, a, n_slots, mgr_reserve,
        )
        _add_block(load, start_slot, block)
        w = WorkWindow(
            start_h=_slot_to_hour(c, start_slot),
            end_h=_slot_to_hour(c, start_slot + block),
        )
        schedules[a.agent_id] = [w]
        rationale[a.agent_id] = _ic_rationale(a, w, sig)

    # --- 2. Place manager availability windows to cover reports -----------
    for a in sorted(managers, key=lambda m: m.agent_id):
        windows = _place_manager_windows(c, obj, sig, load, a, schedules)
        for w in windows:
            _add_block(load, _hour_to_slot(c, w.start_h),
                       _block_slots(c, w.length_h))
        schedules[a.agent_id] = windows
        rationale[a.agent_id] = _manager_rationale(a, windows, schedules)

    # --- 3. Validate invariants + impact ----------------------------------
    violations = _validate(c, agents, schedules, load)
    impact = _impact(c, agents, schedules, load, sig)
    feasible = not violations

    summary = (
        f"{len(agents)} agents scheduled — peak {impact.peak_concurrency}/"
        f"{c.capacity_ceiling} concurrent, {impact.total_scheduled_hours:.0f}h "
        f"total, spread {impact.spread_span_h:.1f}h, "
        f"{impact.reports_with_oversight_gap} oversight gaps. "
        f"{'FEASIBLE' if feasible else 'INFEASIBLE: ' + '; '.join(violations[:3])}"
    )

    return ScheduleProposal(
        schedules=schedules,
        feasible=feasible,
        impact=impact,
        violations=violations,
        rationale=rationale,
        summary=summary,
    )


def _best_block_start(
    c: Constraints,
    obj: Objectives,
    sig: Signals,
    load: list[float],
    block: int,
    agent: AgentSpec,
    n_slots: int,
    reserve: int = 0,
) -> int:
    """Pick the start slot for an IC block that best fits the objectives.

    ``reserve`` is headroom withheld from the ceiling for managers placed
    later, so the combined peak can never breach.
    """
    best_slot = 0
    best_cost = float("inf")
    overloaded = sig.overtime_hours.get(agent.agent_id, 0.0) > 0.0
    pref_slot = (
        _hour_to_slot(c, float(agent.preferred_start))
        if agent.preferred_start is not None
        else None
    )

    for s in range(0, max(1, n_slots - block + 1)):
        # How far over the (saturation-adjusted) ceiling this start would
        # push any covered slot, and how close to the ceiling it runs.
        breach = 0.0
        headroom_pressure = 0.0
        for t in range(s, s + block):
            new_load = load[t] + 1
            ceil_t = max(1.0, _effective_ceiling(c, sig, t) - reserve)
            if new_load > ceil_t:
                breach += new_load - ceil_t
            elif ceil_t > 0:
                headroom_pressure += new_load / ceil_t  # 0..1 per slot
        # Cost: ceiling breaches dominate (hard cap). Then CLUSTERING —
        # prefer earlier starts so people overlap for collaboration; we
        # only spread when the ceiling forces it. w_peak softly prefers
        # leaving headroom under the ceiling. Then preference, then
        # overtime relief (an overloaded agent biases to quieter slots).
        cost = (
            breach * 1000.0
            + obj.w_spread * s
            + obj.w_peak * headroom_pressure * 0.1
        )
        if pref_slot is not None:
            cost += obj.w_preference * abs(s - pref_slot)
        if overloaded:
            cost += obj.w_overtime * sum(load[s:s + block]) * 0.01
        if cost < best_cost:
            best_cost = cost
            best_slot = s
    return best_slot


def _place_manager_windows(
    c: Constraints,
    obj: Objectives,
    sig: Signals,
    load: list[float],
    manager: AgentSpec,
    schedules: dict[str, list[WorkWindow]],
) -> list[WorkWindow]:
    """Place a manager's availability windows to blanket their reports.

    Guarantees the oversight invariant: every report with a window has at
    least one manager window overlapping it. Windows are spread across the
    reports' active span to minimise the largest uncovered gap.
    """
    report_windows = [
        w
        for r in manager.reports
        for w in schedules.get(r, [])
    ]
    n = max(1, int(round(min(manager.budget_hours, c.manager_windows
                             * c.manager_window_len_h) / c.manager_window_len_h)))
    wlen = c.manager_window_len_h

    if not report_windows:
        # No reports placed yet — fall back to a clustered block at day start.
        start = c.day_start_h
        return [WorkWindow(start, min(start + min(manager.budget_hours,
                                                  c.ic_block_len_h), c.day_end_h))]

    span_start = min(w.start_h for w in report_windows)
    span_end = max(w.end_h for w in report_windows)
    span_start = max(span_start, c.day_start_h)
    span_end = min(span_end, c.day_end_h)

    # Evenly distribute n window-centres across [span_start, span_end].
    windows: list[WorkWindow] = []
    if n == 1:
        centres = [(span_start + span_end) / 2]
    else:
        step = (span_end - span_start) / n
        centres = [span_start + step * (i + 0.5) for i in range(n)]
    for cen in centres:
        start = max(c.day_start_h, min(cen - wlen / 2, c.day_end_h - wlen))
        windows.append(WorkWindow(start, start + wlen))

    # Coverage repair: ensure every report window is overlapped by at least
    # one manager window; if not, nudge the nearest manager window onto it.
    for rw in report_windows:
        if any(mw.overlaps(rw) for mw in windows):
            continue
        rmid = (rw.start_h + rw.end_h) / 2
        nearest = min(
            windows,
            key=lambda mw: abs((mw.start_h + mw.end_h) / 2 - rmid),
        )
        new_start = max(c.day_start_h, min(rmid - wlen / 2, c.day_end_h - wlen))
        nearest.start_h = new_start
        nearest.end_h = new_start + wlen

    return windows


def _add_block(load: list[float], start: int, block: int) -> None:
    for t in range(start, min(start + block, len(load))):
        load[t] += 1


def _validate(
    c: Constraints,
    agents: list[AgentSpec],
    schedules: dict[str, list[WorkWindow]],
    load: list[float],
) -> list[str]:
    """Return a list of hard-invariant violations (empty == feasible)."""
    v: list[str] = []
    by_id = {a.agent_id: a for a in agents}

    # 1. Budget + 4. no starvation + 5. within day span
    for a in agents:
        ws = schedules.get(a.agent_id, [])
        total = sum(w.length_h for w in ws)
        if total > a.budget_hours + 1e-6:
            v.append(f"{a.agent_id}: scheduled {total:.1f}h > budget {a.budget_hours:.1f}h")
        if total <= 0:
            v.append(f"{a.agent_id}: starved (no windows)")
        for w in ws:
            if w.start_h < c.day_start_h - 1e-6 or w.end_h > c.day_end_h + 1e-6:
                v.append(f"{a.agent_id}: window {w.start_h:.1f}-{w.end_h:.1f} outside day span")

    # 2. Oversight: every manager window-set overlaps each report
    for a in agents:
        if a.role_type != RoleType.MANAGER:
            continue
        mws = schedules.get(a.agent_id, [])
        for r in a.reports:
            rws = schedules.get(r, [])
            if not rws:
                continue
            if not any(mw.overlaps(rw) for mw in mws for rw in rws):
                v.append(f"{a.agent_id}: no oversight overlap with report {r}")

    # 3. Capacity ceiling
    peak = max(load) if load else 0
    if peak > c.capacity_ceiling:
        v.append(f"peak concurrency {int(peak)} > ceiling {c.capacity_ceiling}")

    return v


def _impact(
    c: Constraints,
    agents: list[AgentSpec],
    schedules: dict[str, list[WorkWindow]],
    load: list[float],
    sig: Signals,
) -> ImpactPreview:
    imp = ImpactPreview(capacity_ceiling=c.capacity_ceiling)
    imp.peak_concurrency = int(max(load)) if load else 0
    imp.total_scheduled_hours = sum(
        w.length_h for ws in schedules.values() for w in ws
    )
    all_w = [w for ws in schedules.values() for w in ws]
    if all_w:
        imp.spread_span_h = max(w.end_h for w in all_w) - min(w.start_h for w in all_w)

    # Oversight gaps + worst uncovered report gap
    for a in agents:
        if a.role_type != RoleType.MANAGER:
            continue
        mws = schedules.get(a.agent_id, [])
        for r in a.reports:
            rws = schedules.get(r, [])
            if not rws:
                continue
            covered = any(mw.overlaps(rw) for mw in mws for rw in rws)
            if not covered:
                imp.reports_with_oversight_gap += 1
                gap = min(rw.length_h for rw in rws)
                imp.max_blocked_report_gap_h = max(imp.max_blocked_report_gap_h, gap)

    # Overtime: chronic overtime can't be fixed by timing — surface it.
    imp.predicted_overtime_hours = sum(sig.overtime_hours.values())
    imp.chronic_overtime_agents = sorted(
        aid for aid, h in sig.overtime_hours.items() if h >= 2.0
    )
    return imp


def windows_to_schedule_config(
    windows: list[WorkWindow], *, replan_per_window: bool = True,
) -> dict[str, str]:
    """Convert windows to the framework Scheduler's config format.

    Each window becomes a wake time (start) and a sleep time (end).
    Multiple windows → comma-separated times. Hours are taken mod 24 so a
    window crossing midnight is expressed correctly.
    """
    def fmt(h: float) -> str:
        total_min = int(round(h * 60)) % (24 * 60)
        return f"{total_min // 60:02d}:{total_min % 60:02d}"

    wake = ",".join(fmt(w.start_h) for w in sorted(windows, key=lambda w: w.start_h))
    sleep = ",".join(fmt(w.end_h) for w in sorted(windows, key=lambda w: w.start_h))
    cfg = {"wake": wake, "sleep": sleep}
    if replan_per_window and len(windows) > 1:
        # A mid-window replan keeps multi-window agents responsive.
        cfg["replan"] = ",".join(
            fmt((w.start_h + w.end_h) / 2) for w in sorted(windows, key=lambda w: w.start_h)
        )
    return cfg


def _ic_rationale(a: AgentSpec, w: WorkWindow, sig: Signals) -> str:
    bits = [f"IC focus block {w.start_h:04.1f}-{w.end_h:04.1f}"]
    if sig.overtime_hours.get(a.agent_id, 0.0) > 0:
        bits.append(f"placed in a quieter slot ({sig.overtime_hours[a.agent_id]:.1f}h overtime)")
    if a.preferred_start is not None:
        bits.append(f"near preferred start {a.preferred_start:02d}:00")
    return "; ".join(bits)


def _manager_rationale(
    a: AgentSpec, windows: list[WorkWindow], schedules: dict[str, list[WorkWindow]],
) -> str:
    spans = ", ".join(f"{w.start_h:04.1f}-{w.end_h:04.1f}" for w in windows)
    return (
        f"{len(windows)} availability windows ({spans}) phased to cover "
        f"{len(a.reports)} report(s)"
    )
