"""Agent-efficiency measurement — the Workforce Performance Analyst's *eyes*.

Turns the signals the workforce already produces into a per-agent efficiency
read over a period, with a trend vs the agent's own past and ranked hotspots
(who's declining, who's at risk, who's a standout). Four dimensions, each from
real data:

* **throughput**   — useful output per active hour (tasks completed / hours)
* **quality**      — escalation rate ↓ + prediction accuracy ↑ (Myelin)
* **cost-efficiency** — output per £ of true cost (the 4-bucket COGS)
* **sustainability** — working within hours + a healthy felt state (efficient
  is not the same as burning out)

It returns a 0-100 composite per agent as a *ranking aid*, but the components
and the trend are what matter — the analyst reasons over them. The composite
must never become the verdict (reason, don't compute), and the dimensions are
chosen so they can't all be gamed at once (raw task-count alone moves
throughput but tanks quality/sustainability).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Soft targets for the absolute→0..1 mapping of the count-based dimensions.
# Tunable; the analyst recalibrates from observed history.
_TARGET_TASKS_PER_HOUR = 2.0
_TARGET_TASKS_PER_GBP = 5.0

# Composite weights (sum 1.0). Quality leads — output that needs rework or
# escalation isn't efficient.
_W_QUALITY = 0.35
_W_THROUGHPUT = 0.30
_W_SUSTAINABILITY = 0.20
_W_COST = 0.15

_DECLINE_PTS = 10.0  # composite drop that flags a "declining" hotspot
_AT_RISK = 0.40  # quality/sustainability floor below which an agent is at risk
_STANDOUT = 75.0  # composite at/above which an agent is a standout


@dataclass
class AgentEfficiencyInput:
    """One agent's raw signals for the period being assessed."""

    agent_id: str
    name: str = ""
    tasks_completed: int = 0
    tasks_escalated: int = 0
    active_hours: float = 0.0
    scheduled_hours: float = 7.5
    prediction_accuracy: float | None = None  # 0..1 (Myelin); None = unmeasured
    cost_gbp: float = 0.0
    satisfaction: float = 0.0  # -1..1 (emotion engine)
    frustration: float = 0.0  # -1..1
    prior_score: float | None = None  # composite from the previous period


@dataclass
class AgentEfficiency:
    agent_id: str
    name: str = ""
    throughput: float = 0.0  # tasks per active hour (raw, displayed)
    quality: float = 0.0  # 0..1
    cost_efficiency: float = 0.0  # tasks per £ (raw, displayed)
    sustainability: float = 0.0  # 0..1
    score: float = 0.0  # 0..100 composite (ranking aid, not the verdict)
    trend: float = 0.0  # composite delta vs the agent's prior period

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "throughput": round(self.throughput, 2),
            "quality": round(self.quality, 3),
            "cost_efficiency": round(self.cost_efficiency, 2),
            "sustainability": round(self.sustainability, 3),
            "score": round(self.score, 1),
            "trend": round(self.trend, 1),
        }


@dataclass
class EfficiencyHotspot:
    kind: str  # "declining" | "at_risk" | "standout"
    agent_id: str
    severity: float
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent_id": self.agent_id,
            "severity": round(self.severity, 2),
            "detail": self.detail,
        }


@dataclass
class WorkforceEfficiency:
    per_agent: list[AgentEfficiency] = field(default_factory=list)
    hotspots: list[EfficiencyHotspot] = field(default_factory=list)
    mean_score: float = 0.0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_agent": [a.to_dict() for a in self.per_agent],
            "hotspots": [h.to_dict() for h in self.hotspots],
            "mean_score": round(self.mean_score, 1),
            "summary": self.summary,
        }


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _score_one(r: AgentEfficiencyInput) -> AgentEfficiency:
    hours = max(r.active_hours, 0.0)
    throughput = r.tasks_completed / hours if hours > 0.1 else 0.0
    done, esc = r.tasks_completed, r.tasks_escalated
    escalation_rate = esc / (done + esc) if (done + esc) > 0 else 0.0
    handled = 1.0 - escalation_rate
    # Quality: not-escalating + (when measured) predicting outcomes well. With
    # no prediction signal, don't penalise for missing data — use handled alone.
    if r.prediction_accuracy is None:
        quality = _clamp01(handled)
    else:
        quality = _clamp01(0.5 * handled + 0.5 * _clamp01(r.prediction_accuracy))
    cost_eff = r.tasks_completed / r.cost_gbp if r.cost_gbp > 0.01 else 0.0
    # Sustainability: within scheduled hours + a non-negative felt balance.
    overrun = max(0.0, hours - max(r.scheduled_hours, 0.1)) / max(r.scheduled_hours, 0.1)
    mood = _clamp01(0.5 + 0.5 * (r.satisfaction - r.frustration))
    sustainability = _clamp01((1.0 - min(1.0, overrun)) * 0.6 + mood * 0.4)

    tput_n = _clamp01(throughput / _TARGET_TASKS_PER_HOUR)
    cost_n = _clamp01(cost_eff / _TARGET_TASKS_PER_GBP)
    score = 100.0 * (
        _W_QUALITY * quality
        + _W_THROUGHPUT * tput_n
        + _W_SUSTAINABILITY * sustainability
        + _W_COST * cost_n
    )
    trend = (score - r.prior_score) if r.prior_score is not None else 0.0
    return AgentEfficiency(
        agent_id=r.agent_id, name=r.name or r.agent_id,
        throughput=throughput, quality=quality, cost_efficiency=cost_eff,
        sustainability=sustainability, score=score, trend=trend,
    )


def assess_workforce_efficiency(
    records: list[AgentEfficiencyInput],
) -> WorkforceEfficiency:
    """Measure per-agent efficiency for the period + flag who needs attention.

    Pure + deterministic. Each record carries one agent's signals for the
    period (and optionally its prior composite, for the trend). Returns a
    :class:`WorkforceEfficiency` ranked worst-trend first in the hotspots, so
    the analyst looks at the agents moving the wrong way before the standouts.
    """
    out = WorkforceEfficiency()
    scored = [_score_one(r) for r in records]
    scored.sort(key=lambda a: a.score, reverse=True)
    out.per_agent = scored
    if scored:
        out.mean_score = sum(a.score for a in scored) / len(scored)

    hotspots: list[EfficiencyHotspot] = []
    for a in scored:
        if a.trend <= -_DECLINE_PTS:
            hotspots.append(EfficiencyHotspot(
                "declining", a.agent_id, abs(a.trend),
                f"{a.name}'s efficiency fell {abs(a.trend):.0f} pts to "
                f"{a.score:.0f}/100 — find out why before it sets in.",
            ))
        if a.quality < _AT_RISK or a.sustainability < _AT_RISK:
            why = "quality" if a.quality < _AT_RISK else "sustainability"
            hotspots.append(EfficiencyHotspot(
                "at_risk", a.agent_id, (1.0 - min(a.quality, a.sustainability)) * 20,
                f"{a.name} at risk on {why} "
                f"(quality {a.quality:.2f}, sustainability {a.sustainability:.2f}) — "
                f"efficient output that needs rework or burns the agent out isn't real.",
            ))
        if a.score >= _STANDOUT and a.trend >= 0:
            hotspots.append(EfficiencyHotspot(
                "standout", a.agent_id, a.score,
                f"{a.name} performing strongly ({a.score:.0f}/100"
                + (f", +{a.trend:.0f}" if a.trend > 0 else "") + ") — learn from how.",
            ))
    # Worst things first: declining + at-risk outrank standouts.
    order = {"declining": 0, "at_risk": 1, "standout": 2}
    hotspots.sort(key=lambda h: (order.get(h.kind, 9), -h.severity))
    out.hotspots = hotspots

    declining = sum(1 for h in hotspots if h.kind == "declining")
    at_risk = sum(1 for h in hotspots if h.kind == "at_risk")
    standout = sum(1 for h in hotspots if h.kind == "standout")
    top = hotspots[0].detail if hotspots else "no agents need attention"
    out.summary = (
        f"Workforce efficiency {out.mean_score:.0f}/100 mean across "
        f"{len(scored)} agent(s) — {declining} declining, {at_risk} at risk, "
        f"{standout} standout. Top: {top}"
    )
    return out
