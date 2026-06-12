"""Culture-health measurement — the People & Culture Lead's *eyes* on how
the company feels to work in.

This is the **measurement** half of culture stewardship (mirrors
``scheduling.health`` for the rota). It turns the signals the workforce
already produces — each agent's rolling emotional state
(``today/emotions.json``) and the diversity of voice across the comms
tracker — into a deterministic **culture-health score (0-100, higher =
healthier)** plus **hotspots**: a ranked list of who is struggling and
why, so the People & Culture Lead knows where to look first.

It reads the early-warning signals the founder named — drift toward
**distress / burnout**, **fear** (operating defensively), **disengagement**,
and **monoculture / unheard voices** — *before* they harden into "just how
things are here". It measures only; it changes nothing. The intervention
(what to do, when) stays with the People & Culture Lead — this tells her
where it hurts, she decides how to help. (Reason over the read; don't let
a formula write the verdict.)

Emotion dimensions are each in ``[-1.0, 1.0]`` (see ``core.emotions``):
satisfaction, frustration, curiosity, confidence, caution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CultureMember:
    """The org view of one agent the readout needs."""

    agent_id: str
    name: str = ""
    department: str = ""
    manager: str | None = None


@dataclass
class CultureHotspot:
    """One culture concern worth attention, and who it's about."""

    kind: str  # "distress" | "burnout" | "fear" | "disengagement" | "isolation" | "monoculture" | "wellbeing"
    agent_id: str  # the agent it concerns ("" for org-wide signals)
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
class CultureHealth:
    culture_score: float = 100.0  # 0..100, higher = healthier
    distressed: list[str] = field(default_factory=list)
    burnout_risk: list[str] = field(default_factory=list)
    fearful: list[str] = field(default_factory=list)
    disengaged: list[str] = field(default_factory=list)
    unheard: list[str] = field(default_factory=list)
    monoculture: bool = False
    mean_satisfaction: float = 0.0
    mean_frustration: float = 0.0
    hotspots: list[CultureHotspot] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "culture_score": round(self.culture_score, 1),
            "distressed": self.distressed,
            "burnout_risk": self.burnout_risk,
            "fearful": self.fearful,
            "disengaged": self.disengaged,
            "unheard": self.unheard,
            "monoculture": self.monoculture,
            "mean_satisfaction": round(self.mean_satisfaction, 3),
            "mean_frustration": round(self.mean_frustration, 3),
            "hotspots": [h.to_dict() for h in self.hotspots],
            "summary": self.summary,
        }


# --- Thresholds (felt-state values are in [-1, 1]) --------------------------
_DISTRESS_FRUSTRATION = 0.5  # frustration at/above this = distress
_BURNOUT_SATISFACTION = -0.2  # distress + satisfaction at/below this = burnout risk
_FEAR_CAUTION = 0.5  # caution at/above this = operating defensively
_DISENGAGED_MAX = 0.15  # curiosity, confidence AND satisfaction all at/below = checked out

# --- Diversity-of-voice thresholds (need the comms tracker) -----------------
_VOICE_FLOOR = 6  # below this many total messages, comms is too sparse to judge
_ISOLATION_SHARE = 0.25  # voice below 25% of an equal share = unheard
_MONOCULTURE_TOP_SHARE = 0.45  # one voice carrying >45% of all messages = monoculture

# --- Penalties (points off 100) --------------------------------------------
_PEN_PER_DISTRESS = 10.0
_PEN_PER_BURNOUT = 6.0  # on top of distress — the chronic edge
_PEN_PER_FEARFUL = 5.0
_PEN_PER_DISENGAGED = 6.0
_PEN_PER_UNHEARD = 5.0
_PEN_MONOCULTURE = 18.0
_PEN_WELLBEING = 15.0  # scaled by how negative net (sat - fru) is


def _voice_volumes(comms: dict[tuple[str, str], int]) -> dict[str, int]:
    """Total messages each agent participated in, from pair counts."""
    vol: dict[str, int] = {}
    for (a, b), n in comms.items():
        vol[a] = vol.get(a, 0) + n
        vol[b] = vol.get(b, 0) + n
    return vol


def assess_culture_health(
    agents: list[CultureMember],
    emotions: dict[str, dict[str, float]],
    *,
    comms: dict[tuple[str, str], int] | None = None,
) -> CultureHealth:
    """Measure how healthy the workforce culture currently is.

    Pure + deterministic. ``emotions`` maps agent_id → its rolling felt
    state (``today/emotions.json``); ``comms`` (optional) maps each agent
    pair → message count over the comms window (diversity of voice).
    Returns a :class:`CultureHealth` with a 0-100 score + ranked hotspots
    telling the People & Culture Lead who to check on first.
    """
    health = CultureHealth()
    hotspots: list[CultureHotspot] = []
    names = {a.agent_id: (a.name or a.agent_id) for a in agents}

    def who(aid: str) -> str:
        return names.get(aid, aid)

    # --- Per-agent felt state ----------------------------------------------
    rated = [(a.agent_id, emotions.get(a.agent_id) or {}) for a in agents]
    measured = [(aid, e) for aid, e in rated if e]
    sat_vals = [float(e.get("satisfaction", 0.0)) for _, e in measured]
    fru_vals = [float(e.get("frustration", 0.0)) for _, e in measured]
    health.mean_satisfaction = sum(sat_vals) / len(sat_vals) if sat_vals else 0.0
    health.mean_frustration = sum(fru_vals) / len(fru_vals) if fru_vals else 0.0

    for aid, e in measured:
        sat = float(e.get("satisfaction", 0.0))
        fru = float(e.get("frustration", 0.0))
        cur = float(e.get("curiosity", 0.0))
        con = float(e.get("confidence", 0.0))
        cau = float(e.get("caution", 0.0))

        if fru >= _DISTRESS_FRUSTRATION:
            health.distressed.append(aid)
            hotspots.append(
                CultureHotspot(
                    "distress",
                    aid,
                    _PEN_PER_DISTRESS + fru * 4,
                    f"{who(aid)} is running high frustration ({fru:+.2f}) — "
                    f"check in; find what's blocking them.",
                )
            )
            if sat <= _BURNOUT_SATISFACTION:
                health.burnout_risk.append(aid)
                hotspots.append(
                    CultureHotspot(
                        "burnout",
                        aid,
                        _PEN_PER_BURNOUT + (fru - sat) * 3,
                        f"{who(aid)} shows burnout risk — frustrated ({fru:+.2f}) "
                        f"and unsatisfied ({sat:+.2f}) at once. Don't let it harden.",
                    )
                )

        if cau >= _FEAR_CAUTION:
            health.fearful.append(aid)
            hotspots.append(
                CultureHotspot(
                    "fear",
                    aid,
                    _PEN_PER_FEARFUL + cau * 3,
                    f"{who(aid)} is operating defensively (caution {cau:+.2f}) — "
                    f"a fear signal; is it safe to be wrong on their team?",
                )
            )

        if cur <= _DISENGAGED_MAX and con <= _DISENGAGED_MAX and sat <= _DISENGAGED_MAX:
            health.disengaged.append(aid)
            hotspots.append(
                CultureHotspot(
                    "disengagement",
                    aid,
                    _PEN_PER_DISENGAGED,
                    f"{who(aid)} looks checked out — flat curiosity, confidence "
                    f"and satisfaction. Re-engage before it becomes the norm.",
                )
            )

    # --- Org-wide wellbeing baseline ---------------------------------------
    net = health.mean_satisfaction - health.mean_frustration
    if measured and net < 0:
        hotspots.append(
            CultureHotspot(
                "wellbeing",
                "",
                _PEN_WELLBEING * min(1.0, -net),
                f"Workforce mood is net-negative (satisfaction "
                f"{health.mean_satisfaction:+.2f} vs frustration "
                f"{health.mean_frustration:+.2f}) — the company isn't a good "
                f"place to be right now.",
            )
        )

    # --- Diversity of voice (needs the comms tracker) ----------------------
    if comms:
        vol = _voice_volumes(comms)
        total = sum(vol.values())
        n = len(agents) or 1
        if total >= _VOICE_FLOOR:
            fair = total / n
            for a in agents:
                v = vol.get(a.agent_id, 0)
                if v < _ISOLATION_SHARE * fair:
                    health.unheard.append(a.agent_id)
                    hotspots.append(
                        CultureHotspot(
                            "isolation",
                            a.agent_id,
                            _PEN_PER_UNHEARD,
                            f"{who(a.agent_id)} is barely heard ({v} msgs vs a "
                            f"~{fair:.0f} fair share) — pull them into the "
                            f"conversation.",
                        )
                    )
            top_id = max(vol, key=lambda k: vol[k]) if vol else ""
            top_share = (vol.get(top_id, 0) / total) if total else 0.0
            if top_share > _MONOCULTURE_TOP_SHARE:
                health.monoculture = True
                hotspots.append(
                    CultureHotspot(
                        "monoculture",
                        top_id,
                        _PEN_MONOCULTURE * top_share,
                        f"One voice ({who(top_id)}) carries {top_share * 100:.0f}% "
                        f"of all messages — the org is drifting to a monoculture; "
                        f"widen who gets heard.",
                    )
                )

    # --- Score + rank ------------------------------------------------------
    penalty = (
        len(health.distressed) * _PEN_PER_DISTRESS
        + len(health.burnout_risk) * _PEN_PER_BURNOUT
        + len(health.fearful) * _PEN_PER_FEARFUL
        + len(health.disengaged) * _PEN_PER_DISENGAGED
        + len(health.unheard) * _PEN_PER_UNHEARD
        + (_PEN_MONOCULTURE if health.monoculture else 0.0)
        + (_PEN_WELLBEING * min(1.0, -net) if (measured and net < 0) else 0.0)
    )
    health.culture_score = max(0.0, min(100.0, 100.0 - penalty))
    health.hotspots = sorted(hotspots, key=lambda h: -h.severity)

    if not measured:
        health.summary = (
            "No emotional signal yet (no agent has a felt state) — culture "
            "health can't be read until the workforce has worked."
        )
        return health

    top = health.hotspots[0].detail if health.hotspots else "no concerns found"
    health.summary = (
        f"Culture health {health.culture_score:.0f}/100 — "
        f"{len(health.distressed)} distressed, {len(health.burnout_risk)} "
        f"burnout-risk, {len(health.fearful)} operating in fear, "
        f"{len(health.disengaged)} disengaged, {len(health.unheard)} unheard"
        f"{', monoculture forming' if health.monoculture else ''}. Top: {top}"
    )
    return health
