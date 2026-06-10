"""Schedule-health measurement — the AR Scheduler's *eyes* on the rota.

Scheduling is a continuous stewardship job: the AR Scheduler measures how
responsive the current rota makes the company, then tweaks one role at a
time to improve it (see ``optimize_schedule`` for placement). This module
is the **measurement** half — a pure, deterministic readout of the *current*
schedule, scored on **responsiveness**:

* **coverage** — hours of the day with nobody awake (the company can't
  answer then),
* **oversight overlap** — every report should share time with its manager,
  or it sits blocked waiting on a decision,
* **collaboration overlap** — peers (agents sharing a manager) should share
  some time, or work serialises across handoff gaps,
* **chronic overtime** — a secondary drag signal.

It returns a 0–100 responsiveness score plus **hotspots**: a ranked list of
which role to look at next and why. The AR Scheduler reads this, picks the
top hotspot, and optimises that one role. The verdict stays hers — this only
tells her where it hurts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cortiva.scheduling.optimizer import AgentSpec, RoleType, Signals, WorkWindow


@dataclass
class CoverageGap:
    start_h: float
    end_h: float

    def to_dict(self) -> dict[str, Any]:
        return {"start_h": self.start_h, "end_h": self.end_h}


@dataclass
class Hotspot:
    """One thing worth fixing, and the role to act on."""

    kind: str  # "coverage" | "oversight" | "isolation" | "overtime"
    agent_id: str  # the role to optimise to address it ("" for coverage)
    severity: float  # higher = worse; used to rank
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent_id": self.agent_id,
            "severity": round(self.severity, 2),
            "detail": self.detail,
        }


@dataclass
class ScheduleHealth:
    responsiveness_score: float = 100.0  # 0..100, higher = more responsive
    uncovered_hours: float = 0.0
    coverage_gaps: list[CoverageGap] = field(default_factory=list)
    oversight_gaps: list[dict[str, str]] = field(default_factory=list)  # report→manager
    isolated_agents: list[str] = field(default_factory=list)
    chronic_overtime: list[str] = field(default_factory=list)
    # Model contention: how many agents are awake at the busiest moment vs how
    # many the node's local model can serve at once. Overlap helps
    # responsiveness; too much overlap queues everyone on one model. This is
    # the tension the AR Scheduler trades.
    peak_concurrency: int = 0  # most agents awake in any one slot
    model_concurrency: int | None = None  # comfortable concurrent serve count (None = unmeasured)
    contended_hours: float = 0.0  # hours where awake-count exceeds model capacity
    hotspots: list[Hotspot] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "responsiveness_score": round(self.responsiveness_score, 1),
            "uncovered_hours": round(self.uncovered_hours, 1),
            "coverage_gaps": [g.to_dict() for g in self.coverage_gaps],
            "oversight_gaps": self.oversight_gaps,
            "isolated_agents": self.isolated_agents,
            "chronic_overtime": self.chronic_overtime,
            "peak_concurrency": self.peak_concurrency,
            "model_concurrency": self.model_concurrency,
            "contended_hours": round(self.contended_hours, 1),
            "hotspots": [h.to_dict() for h in self.hotspots],
            "summary": self.summary,
        }


# Responsiveness penalties (points off 100). Coverage + oversight dominate —
# they are what make the company answer fast; collaboration + overtime are
# softer drags.
_PEN_PER_UNCOVERED_HOUR = 4.0
_PEN_PER_OVERSIGHT_GAP = 8.0
_PEN_PER_ISOLATED = 3.0
_PEN_PER_OVERTIME = 2.0
_CHRONIC_OVERTIME_H = 2.0
# Per agent-hour of model contention (an agent awake in a slot whose awake-count
# already exceeds what the local model serves at once → it queues). Lower than a
# coverage gap: contention slows the company, a gap stops it answering at all.
_PEN_PER_CONTENTION_AGENT_HOUR = 1.5


def _occupancy(schedules: dict[str, list[WorkWindow]], slot_minutes: int) -> list[int]:
    """Count agents on-shift per slot across a 24h clock (windows wrap)."""
    n = int(round(24 * 60 / slot_minutes))
    occ = [0] * n
    for windows in schedules.values():
        for w in windows:
            start = int(round((w.start_h % 24) * 60 / slot_minutes))
            length = max(1, int(round(w.length_h * 60 / slot_minutes)))
            for k in range(length):
                occ[(start + k) % n] += 1
    return occ


def _overlaps_any(a_windows: list[WorkWindow], b_windows: list[WorkWindow]) -> bool:
    return any(aw.overlaps(bw) for aw in a_windows for bw in b_windows)


def assess_schedule_health(
    agents: list[AgentSpec],
    schedules: dict[str, list[WorkWindow]],
    *,
    signals: Signals | None = None,
    slot_minutes: int = 30,
    model_concurrency: int | None = None,
) -> ScheduleHealth:
    """Measure how responsive the *current* rota makes the company.

    Pure + deterministic. ``schedules`` is the live rota (windows per
    agent); ``agents`` carries the org (manager/reports). Returns a
    :class:`ScheduleHealth` with a responsiveness score + ranked hotspots
    telling the AR Scheduler which single role to optimise next.

    ``model_concurrency`` (when known, from the node's measured contention
    history) is how many agents the local model serves at once before they
    queue. Awake-count beyond it is **contention** — penalised, so the AR
    Scheduler trades the overlap that helps responsiveness against the
    overlap that just makes everyone queue on one model.
    """
    sig = signals or Signals()
    by_id = {a.agent_id: a for a in agents}
    health = ScheduleHealth()
    health.model_concurrency = model_concurrency
    hotspots: list[Hotspot] = []

    # --- Coverage: hours with nobody awake ---------------------------------
    occ = _occupancy(schedules, slot_minutes)
    slot_h = slot_minutes / 60.0
    health.peak_concurrency = max(occ) if occ else 0
    gap_start: int | None = None
    for i in range(len(occ) + 1):
        empty = i < len(occ) and occ[i] == 0
        if empty and gap_start is None:
            gap_start = i
        elif not empty and gap_start is not None:
            g = CoverageGap(round(gap_start * slot_h, 2), round(i * slot_h, 2))
            health.coverage_gaps.append(g)
            health.uncovered_hours += g.end_h - g.start_h
            hotspots.append(
                Hotspot(
                    "coverage",
                    "",
                    (g.end_h - g.start_h) * _PEN_PER_UNCOVERED_HOUR,
                    f"No one awake {g.start_h:05.2f}–{g.end_h:05.2f} — "
                    f"the company can't answer then.",
                )
            )
            gap_start = None

    # --- Oversight: every report shares time with its manager --------------
    for a in agents:
        if not a.reports:
            continue
        mw = schedules.get(a.agent_id, [])
        for r in a.reports:
            if r not in by_id:
                continue
            rw = schedules.get(r, [])
            if rw and not _overlaps_any(mw, rw):
                health.oversight_gaps.append({"report": r, "manager": a.agent_id})
                hotspots.append(
                    Hotspot(
                        "oversight",
                        a.agent_id,
                        _PEN_PER_OVERSIGHT_GAP,
                        f"{r} never overlaps manager {a.agent_id} — blocked waiting "
                        f"on decisions. Shift {a.agent_id} toward {r}'s window.",
                    )
                )

    # --- Collaboration: peers (shared manager) should share time -----------
    peers: dict[str, list[str]] = {}
    for a in agents:
        if a.manager:
            peers.setdefault(a.manager, []).append(a.agent_id)
    isolated_seen: set[str] = set()
    for sibs in peers.values():
        if len(sibs) < 2:
            continue
        for aid in sibs:
            aw = schedules.get(aid, [])
            if not aw:
                continue
            has_peer_overlap = any(
                other != aid and _overlaps_any(aw, schedules.get(other, [])) for other in sibs
            )
            if not has_peer_overlap and aid not in isolated_seen:
                isolated_seen.add(aid)
                health.isolated_agents.append(aid)
                hotspots.append(
                    Hotspot(
                        "isolation",
                        aid,
                        _PEN_PER_ISOLATED,
                        f"{aid} shares no time with its peers — handoffs serialise. "
                        f"Nudge its window toward the team's.",
                    )
                )

    # --- Chronic overtime (secondary drag) ---------------------------------
    for aid, ot in sig.overtime_hours.items():
        if ot >= _CHRONIC_OVERTIME_H:
            health.chronic_overtime.append(aid)
            hotspots.append(
                Hotspot(
                    "overtime",
                    aid,
                    ot * _PEN_PER_OVERTIME,
                    f"{aid} in chronic overtime ({ot:.1f}h) — under-resourced for its load.",
                )
            )

    # --- Model contention: overlap beyond what the model serves at once ----
    contention_penalty = 0.0
    if model_concurrency and model_concurrency > 0:
        # Excess awake-agents per slot, converted to agent-hours of queueing.
        excess_agent_hours = sum(max(0, c - model_concurrency) for c in occ) * slot_h
        health.contended_hours = round(
            sum(slot_h for c in occ if c > model_concurrency), 2
        )
        contention_penalty = excess_agent_hours * _PEN_PER_CONTENTION_AGENT_HOUR
        if health.contended_hours > 0:
            # Point the AR Scheduler at a role to stagger out of the busiest
            # window — prefer an IC (managers are load-bearing for oversight).
            peak_slot = max(range(len(occ)), key=lambda i: occ[i])
            peak_h = round(peak_slot * slot_h, 2)
            movable = ""
            for a in agents:
                if a.role_type != RoleType.MANAGER and any(
                    w.start_h % 24 <= peak_h < w.start_h % 24 + w.length_h
                    for w in schedules.get(a.agent_id, [])
                ):
                    movable = a.agent_id
                    break
            hotspots.append(
                Hotspot(
                    "contention",
                    movable,
                    contention_penalty,
                    f"{health.peak_concurrency} agents awake at ~{peak_h:05.2f} but the "
                    f"local model serves ~{model_concurrency} at once — the rest queue "
                    f"({health.contended_hours:.1f}h contended)."
                    + (f" Stagger {movable} out to ease it." if movable else
                       " Stagger a non-manager role out, or add model capacity."),
                )
            )

    # --- Score + rank ------------------------------------------------------
    penalty = (
        health.uncovered_hours * _PEN_PER_UNCOVERED_HOUR
        + len(health.oversight_gaps) * _PEN_PER_OVERSIGHT_GAP
        + len(health.isolated_agents) * _PEN_PER_ISOLATED
        + sum(min(sig.overtime_hours.get(a, 0.0), 8.0) for a in health.chronic_overtime)
        * _PEN_PER_OVERTIME
        + contention_penalty
    )
    health.responsiveness_score = max(0.0, min(100.0, 100.0 - penalty))
    health.hotspots = sorted(hotspots, key=lambda h: -h.severity)

    top = health.hotspots[0].detail if health.hotspots else "no issues found"
    cap_note = (
        f", peak {health.peak_concurrency} awake vs ~{model_concurrency} served"
        f" ({health.contended_hours:.1f}h contended)"
        if model_concurrency
        else f", peak {health.peak_concurrency} awake"
    )
    health.summary = (
        f"Responsiveness {health.responsiveness_score:.0f}/100 — "
        f"{health.uncovered_hours:.1f}h uncovered, {len(health.oversight_gaps)} "
        f"oversight gap(s), {len(health.isolated_agents)} isolated, "
        f"{len(health.chronic_overtime)} in overtime{cap_note}. Top: {top}"
    )
    return health


# ---------------------------------------------------------------------------
# Single-role recommendation — the steady-state tweak
# ---------------------------------------------------------------------------


@dataclass
class ScheduleRecommendation:
    """A proposed re-timing of ONE role to raise overall responsiveness."""

    target: str = ""
    current_windows: list[WorkWindow] = field(default_factory=list)
    recommended_windows: list[WorkWindow] = field(default_factory=list)
    score_before: float = 0.0
    score_after: float = 0.0
    rationale: str = ""

    @property
    def delta(self) -> float:
        return round(self.score_after - self.score_before, 1)

    def to_dict(self) -> dict[str, Any]:
        def wins(ws: list[WorkWindow]) -> list[dict[str, float]]:
            return [{"start_h": w.start_h % 24, "end_h": w.end_h} for w in ws]

        return {
            "target": self.target,
            "current_windows": wins(self.current_windows),
            "recommended_windows": wins(self.recommended_windows),
            "score_before": round(self.score_before, 1),
            "score_after": round(self.score_after, 1),
            "delta": self.delta,
            "rationale": self.rationale,
        }


def _shift(windows: list[WorkWindow], offset_h: float) -> list[WorkWindow]:
    """Shift a role's windows by ``offset_h`` (preserving each length)."""
    return [
        WorkWindow((w.start_h + offset_h) % 24, (w.start_h + offset_h) % 24 + w.length_h)
        for w in windows
    ]


def recommend_schedule_change(
    agents: list[AgentSpec],
    schedules: dict[str, list[WorkWindow]],
    *,
    target: str | None = None,
    signals: Signals | None = None,
    slot_minutes: int = 30,
    model_concurrency: int | None = None,
) -> ScheduleRecommendation:
    """Recommend a re-timing of ONE role that most improves responsiveness.

    The AR Scheduler's steady-state move: hold everyone else's schedule
    fixed, pick the role to tune (the worst hotspot's role by default, or an
    explicit ``target``), and search re-timings of just that role — shifting
    its window(s) around the clock — for the one that maximises the overall
    responsiveness score (the same metric :func:`assess_schedule_health`
    computes). Returns the proposed windows + the score delta + a rationale.
    Pure + deterministic; it recommends, it doesn't apply.
    """
    base = assess_schedule_health(
        agents, schedules, signals=signals, slot_minutes=slot_minutes,
        model_concurrency=model_concurrency,
    )
    if target is None:
        target = next((h.agent_id for h in base.hotspots if h.agent_id), None)

    rec = ScheduleRecommendation(
        target=target or "",
        current_windows=list(schedules.get(target, [])) if target else [],
        recommended_windows=list(schedules.get(target, [])) if target else [],
        score_before=base.responsiveness_score,
        score_after=base.responsiveness_score,
    )
    if not target or target not in schedules or not schedules[target]:
        rec.rationale = "No single-role tweak available (no rota / no hotspot owner)."
        return rec

    cur = schedules[target]
    step_h = slot_minutes / 60.0
    best_score = base.responsiveness_score
    best_windows = cur
    # Try shifting this role's whole shift around the clock, slot by slot.
    n_steps = int(round(24 / step_h))
    for k in range(n_steps):
        offset = k * step_h
        if offset == 0:
            continue
        trial_windows = _shift(cur, offset)
        trial = dict(schedules)
        trial[target] = trial_windows
        score = assess_schedule_health(
            agents, trial, signals=signals, slot_minutes=slot_minutes,
            model_concurrency=model_concurrency,
        ).responsiveness_score
        # Strictly better only (ties keep the current schedule → no churn).
        if score > best_score + 1e-9:
            best_score = score
            best_windows = trial_windows

    rec.recommended_windows = best_windows
    rec.score_after = best_score
    if best_windows is cur or rec.delta <= 0:
        rec.recommended_windows = cur
        rec.score_after = base.responsiveness_score
        rec.rationale = (
            f"{target}'s schedule is already near-optimal for responsiveness "
            f"({base.responsiveness_score:.0f}/100); no re-timing improves it."
        )
    else:
        old = ",".join(f"{w.start_h % 24:04.1f}-{w.end_h % 24:04.1f}" for w in cur)
        new = ",".join(f"{w.start_h % 24:04.1f}-{w.end_h % 24:04.1f}" for w in best_windows)
        rec.rationale = (
            f"Re-time {target} from [{old}] to [{new}] → responsiveness "
            f"{base.responsiveness_score:.0f}→{best_score:.0f} (+{rec.delta}). "
            f"Closes the worst gap while holding every other role fixed."
        )
    return rec
