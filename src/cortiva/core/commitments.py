"""Commitments ledger — durable, deadline-aware tracking of promises an agent
has MADE (outbound), with a work-vs-time pressure model.

This is the sibling of the directive register (``directives.json``), but it
solves the opposite problem. A directive is something a superior asked the
agent to do; it resolves the moment the agent replies. A *commitment* is
something the agent promised someone ("the readout by EOD Thursday", "the 20
bug-fixes by Friday") — and it must NOT resolve when the agent replies, only
when the work is actually delivered. Replying "I'll do X by Tuesday" is the
*creation* of a commitment, not its discharge.

The pressure model is **required utilisation**:

    U = work_remaining / time_remaining

U is the fraction of the agent's remaining time it would have to spend on this
one commitment to land it. A 10-minute task due in a week has U ≈ 0; twenty
bug-fixes due in a week has a mild U; the same twenty with a day left and none
done has a high U; with two hours left, U > 1 — impossible at a normal pace,
which is the cue to pull overtime (drink coffee) and escalate.

Effort is the agent's own estimate (``effort_hours``) discounted by progress.
Progress is objective when the commitment was decomposed into subtasks
(done / total); otherwise it falls back to a self-reported ``progress`` float.

This module is PURE (no fabric/IO beyond the JSON file helpers) so it can be
unit-tested in isolation and reused by the node's wind-down logic.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

COMMITMENTS_FILENAME = "commitments.json"

# Always-on nudge shown even when the ledger is empty, so an agent registers
# the FIRST promise (the salience block can't prompt registration of a
# commitment that doesn't exist yet — the chicken-and-egg gap).
REGISTER_NUDGE = (
    "## ⏳ Commitments — track every promise you make\n"
    "Whenever you commit to a deliverable by a date — in an email, a reply, a "
    "plan ('the readout by Thursday', 'I'll cut scope by Friday', 'fix these "
    "by EOD') — **register it with `register_commitment`** (who it's for, what, "
    "the due date, your honest effort estimate, ideally a subtask breakdown). A "
    "promise you don't register is one the company can't see and you'll feel no "
    "deadline for. Registering it is what makes it real: it's tracked to "
    "delivery, its pressure rises as the deadline nears, and it's only done "
    "when you mark it delivered — not when you reply saying you'll do it."
)

# A commitment is treated as overdue the instant its deadline passes; once it
# has been overdue this long AND is still undelivered, it's archived as missed
# (honest record — a promise that was never kept), so the ledger self-cleans.
_MISS_GRACE_DAYS = 7.0

# "By EOD" with no time given resolves to this local hour.
_EOD_HOUR = 17

# Floor on time_remaining (hours) so U is finite right at / past the deadline.
_TIME_FLOOR_HOURS = 0.25

# U at/above this means "can't finish at a single-track normal pace" — the
# threshold for overtime + escalation.
AT_RISK_UTILISATION = 1.0


@dataclass
class Commitment:
    """One promise the agent has made, with a deadline and an effort estimate."""

    id: str
    to: str = ""               # who it's owed to (email or name)
    what: str = ""             # the deliverable, in the agent's words
    due_at: str = ""           # ISO 8601; the deadline
    effort_hours: float = 1.0  # the agent's own sizing of the whole job
    progress: float = 0.0      # self-reported fraction [0,1] (fallback)
    subtasks: list[dict[str, Any]] = field(default_factory=list)
    """``[{"desc": str, "done": bool}, ...]`` — when present, progress is
    derived from these (objective) rather than the self-reported float."""
    status: str = "open"       # open | delivered | missed
    created_at: str = ""
    delivered_at: str = ""
    artifact: str = ""         # optional proof link (URL / doc id / commit)
    escalated_at: str = ""     # set once we've escalated it (idempotent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "to": self.to,
            "what": self.what,
            "due_at": self.due_at,
            "effort_hours": self.effort_hours,
            "progress": self.progress,
            "subtasks": self.subtasks,
            "status": self.status,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "artifact": self.artifact,
            "escalated_at": self.escalated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Commitment:
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex),
            to=str(d.get("to") or ""),
            what=str(d.get("what") or ""),
            due_at=str(d.get("due_at") or ""),
            effort_hours=_safe_float(d.get("effort_hours"), 1.0),
            progress=_clamp01(_safe_float(d.get("progress"), 0.0)),
            subtasks=list(d.get("subtasks") or []),
            status=str(d.get("status") or "open"),
            created_at=str(d.get("created_at") or ""),
            delivered_at=str(d.get("delivered_at") or ""),
            artifact=str(d.get("artifact") or ""),
            escalated_at=str(d.get("escalated_at") or ""),
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


def parse_due(value: Any) -> str:
    """Best-effort normalise a due value to an ISO 8601 string.

    Accepts a full ISO datetime, a bare ``YYYY-MM-DD`` (assumed EOD = 17:00),
    or a datetime. Anything unparseable returns '' (a commitment with no
    deadline carries no time-pressure — it's just tracked).
    """
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.isoformat()
    s = str(value or "").strip()
    if not s:
        return ""
    # Bare date → end of that day.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s = f"{s}T{_EOD_HOUR:02d}:00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.isoformat()
    except ValueError:
        return ""


def _due_dt(c: Commitment) -> datetime | None:
    if not c.due_at:
        return None
    try:
        dt = datetime.fromisoformat(c.due_at)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Progress / pressure math
# ---------------------------------------------------------------------------


def progress_of(c: Commitment) -> float:
    """Fraction complete in [0,1]. Subtasks (objective) win over the float."""
    if c.subtasks:
        total = len(c.subtasks)
        done = sum(1 for st in c.subtasks if st.get("done"))
        return _clamp01(done / total) if total else 0.0
    return _clamp01(c.progress)


def work_remaining_hours(c: Commitment) -> float:
    """Estimated hours of work still to do = effort × (1 − progress)."""
    return max(0.0, c.effort_hours) * (1.0 - progress_of(c))


def time_remaining_hours(c: Commitment, now: datetime | None = None) -> float:
    """Hours until the deadline. Can be negative (overdue). Floored away from
    zero so utilisation stays finite right at the wire."""
    due = _due_dt(c)
    if due is None:
        return float("inf")  # no deadline → infinite time → no pressure
    return (due - _now(now)).total_seconds() / 3600.0


def is_overdue(c: Commitment, now: datetime | None = None) -> bool:
    return c.status == "open" and time_remaining_hours(c, now) <= 0.0


def required_utilisation(c: Commitment, now: datetime | None = None) -> float:
    """U = work_remaining / time_remaining — the fraction of the agent's
    remaining time this commitment alone would consume to land on time.

    Overdue + undelivered → a large finite number (work is owed and there's no
    time left). No deadline or already delivered → 0.
    """
    if c.status != "open":
        return 0.0
    work = work_remaining_hours(c)
    if work <= 0.0:
        return 0.0  # nothing left to do → no pressure even if the clock's low
    rem = time_remaining_hours(c, now)
    if rem == float("inf"):
        return 0.0
    if rem <= _TIME_FLOOR_HOURS:
        # At or past the deadline with work owed: pin to a high, finite value
        # scaled by how much is left, so "1 hour of work overdue" and "20 hours
        # overdue" still rank against each other.
        return max(AT_RISK_UTILISATION, work / _TIME_FLOOR_HOURS)
    return work / rem


def felt_pressure(commitments: list[Commitment], now: datetime | None = None) -> float:
    """Aggregate neuro pressure in [0,1] across all open commitments.

    The scariest single deadline dominates (max U), plus a damped contribution
    from everything else (many concurrent promises are their own load). A lone
    impossible deadline and a pile of merely-tight ones both read as high.
    """
    us = sorted((required_utilisation(c, now) for c in commitments), reverse=True)
    us = [u for u in us if u > 0.0]
    if not us:
        return 0.0
    top = us[0]
    rest = sum(us[1:])
    return _clamp01(min(top, 1.0) + 0.25 * min(rest, 1.0))


def summarise(commitments: list[Commitment], now: datetime | None = None) -> dict[str, Any]:
    """A compact rollup for heartbeat/manager surfaces and the neuro hook.

    Returns counts + the dominant (highest-U) open commitment, so callers can
    resolve who it's owed to (for the cortisol rank weighting) without
    re-deriving the math.
    """
    opens = [c for c in commitments if c.status == "open"]
    ranked = sorted(opens, key=lambda c: required_utilisation(c, now), reverse=True)
    top = ranked[0] if ranked else None
    return {
        "open": len(opens),
        "at_risk": sum(1 for c in opens if required_utilisation(c, now) >= AT_RISK_UTILISATION),
        "overdue": sum(1 for c in opens if is_overdue(c, now)),
        "pressure": round(felt_pressure(opens, now), 3),
        "max_utilisation": round(
            max((required_utilisation(c, now) for c in opens), default=0.0), 3
        ),
        "top_to": top.to if top else "",
        "top_what": top.what if top else "",
        "top_due_at": top.due_at if top else "",
    }


# ---------------------------------------------------------------------------
# Ledger IO + mutations
# ---------------------------------------------------------------------------


def _path(agent_dir: Path) -> Path:
    return Path(agent_dir) / COMMITMENTS_FILENAME


def load(agent_dir: Path) -> list[Commitment]:
    p = _path(agent_dir)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) or []
    except (ValueError, OSError):
        return []
    return [Commitment.from_dict(d) for d in raw if isinstance(d, dict)]


def save(agent_dir: Path, commitments: list[Commitment]) -> None:
    try:
        _path(agent_dir).write_text(
            json.dumps([c.to_dict() for c in commitments], indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def prune(commitments: list[Commitment], now: datetime | None = None) -> list[Commitment]:
    """Self-clean: archive long-overdue undelivered promises as ``missed``.

    Delivered commitments are kept (they're the proof-of-delivery record) but a
    promise that blew its deadline by more than the grace window and was never
    delivered is marked missed rather than nagging forever.
    """
    n = _now(now)
    for c in commitments:
        if c.status != "open":
            continue
        due = _due_dt(c)
        if due is None:
            continue
        if (n - due) > timedelta(days=_MISS_GRACE_DAYS):
            c.status = "missed"
    return commitments


def register(
    agent_dir: Path,
    *,
    to: str,
    what: str,
    due: Any,
    effort_hours: Any = 1.0,
    subtasks: list[str] | None = None,
    now: datetime | None = None,
) -> Commitment:
    """Create + persist a new commitment. Idempotent on (to, what, due): the
    same promise re-registered doesn't duplicate (an agent re-stating its plan
    each cycle must not spawn copies)."""
    commitments = load(agent_dir)
    due_iso = parse_due(due)
    key = (str(to).strip().lower(), str(what).strip().lower(), due_iso)
    for c in commitments:
        if (c.to.strip().lower(), c.what.strip().lower(), c.due_at) == key and c.status == "open":
            return c  # already tracking this exact promise
    c = Commitment(
        id=uuid.uuid4().hex,
        to=str(to or "").strip(),
        what=str(what or "").strip(),
        due_at=due_iso,
        effort_hours=_safe_float(effort_hours, 1.0),
        subtasks=[{"desc": str(s), "done": False} for s in (subtasks or [])],
        created_at=_now(now).isoformat(),
    )
    commitments.append(c)
    save(agent_dir, commitments)
    return c


def update(
    agent_dir: Path,
    *,
    commitment_id: str = "",
    progress: Any = None,
    delivered: Any = None,
    subtasks_done: list[str] | None = None,
    effort_hours: Any = None,
    artifact: str = "",
    due: Any = None,
    now: datetime | None = None,
) -> Commitment | None:
    """Update a commitment by id (or, if no id, the single open one / the most
    pressing open one). Returns the updated commitment, or None if not found.

    ``delivered=True`` is the ONLY thing that discharges a commitment — and it
    is the deliberate, separate act the directive register lacked.
    """
    commitments = load(agent_dir)
    target = _resolve(commitments, commitment_id, now)
    if target is None:
        return None
    if subtasks_done:
        wanted = {s.strip().lower() for s in subtasks_done}
        for st in target.subtasks:
            if str(st.get("desc", "")).strip().lower() in wanted:
                st["done"] = True
    if progress is not None:
        target.progress = _clamp01(_safe_float(progress, target.progress))
    if effort_hours is not None:
        target.effort_hours = _safe_float(effort_hours, target.effort_hours)
    if artifact:
        target.artifact = str(artifact)
    if due is not None:
        new_due = parse_due(due)
        if new_due:
            target.due_at = new_due
    if delivered:
        target.status = "delivered"
        target.delivered_at = _now(now).isoformat()
        if not target.subtasks and target.progress < 1.0:
            target.progress = 1.0
    save(agent_dir, commitments)
    return target


def _resolve(
    commitments: list[Commitment], commitment_id: str, now: datetime | None
) -> Commitment | None:
    cid = (commitment_id or "").strip()
    if cid:
        for c in commitments:
            if c.id == cid:
                return c
        # Allow a prefix match (agents often quote a short id).
        for c in commitments:
            if c.id.startswith(cid):
                return c
        return None
    opens = [c for c in commitments if c.status == "open"]
    if not opens:
        return None
    # No id given → the most pressing open commitment (highest U).
    return max(opens, key=lambda c: required_utilisation(c, now))
