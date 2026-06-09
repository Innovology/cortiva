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

from cortiva.scheduling.optimizer import AgentSpec, Signals, WorkWindow


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
) -> ScheduleHealth:
    """Measure how responsive the *current* rota makes the company.

    Pure + deterministic. ``schedules`` is the live rota (windows per
    agent); ``agents`` carries the org (manager/reports). Returns a
    :class:`ScheduleHealth` with a responsiveness score + ranked hotspots
    telling the AR Scheduler which single role to optimise next.
    """
    sig = signals or Signals()
    by_id = {a.agent_id: a for a in agents}
    health = ScheduleHealth()
    hotspots: list[Hotspot] = []

    # --- Coverage: hours with nobody awake ---------------------------------
    occ = _occupancy(schedules, slot_minutes)
    slot_h = slot_minutes / 60.0
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

    # --- Score + rank ------------------------------------------------------
    penalty = (
        health.uncovered_hours * _PEN_PER_UNCOVERED_HOUR
        + len(health.oversight_gaps) * _PEN_PER_OVERSIGHT_GAP
        + len(health.isolated_agents) * _PEN_PER_ISOLATED
        + sum(min(sig.overtime_hours.get(a, 0.0), 8.0) for a in health.chronic_overtime)
        * _PEN_PER_OVERTIME
    )
    health.responsiveness_score = max(0.0, min(100.0, 100.0 - penalty))
    health.hotspots = sorted(hotspots, key=lambda h: -h.severity)

    top = health.hotspots[0].detail if health.hotspots else "no issues found"
    health.summary = (
        f"Responsiveness {health.responsiveness_score:.0f}/100 — "
        f"{health.uncovered_hours:.1f}h uncovered, {len(health.oversight_gaps)} "
        f"oversight gap(s), {len(health.isolated_agents)} isolated, "
        f"{len(health.chronic_overtime)} in overtime. Top: {top}"
    )
    return health
