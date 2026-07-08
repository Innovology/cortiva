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

# A normal rota day is ~7.5h of a 24h wall clock ≈ 0.3. Below this a promise
# still fits inside normal working hours; between this and AT_RISK_UTILISATION
# it NO LONGER fits a normal day but IS still winnable by pulling overtime
# (drink_coffee extends the working day); at/above AT_RISK it exceeds literal
# wall-clock and overtime can't save it — that's when escalation is honest.
NORMAL_PACE_UTILISATION = 0.3

# Count-load: carrying many open promises is its own pressure (the "too many
# balls in the air" cortisol), independent of any single deadline. Below the
# comfort line it adds nothing; it ramps to full across the span above it.
_COUNT_COMFORT = 4
_COUNT_SPAN = 11.0  # comfort+span = ~15 open -> full count-load
_COUNT_WEIGHT = 0.4  # how much count-load can add to felt pressure [0,1]


@dataclass
class Commitment:
    """One promise the agent has made, with a deadline and an effort estimate."""

    id: str
    to: str = ""  # who it's owed to (email or name)
    what: str = ""  # the deliverable, in the agent's words
    due_at: str = ""  # ISO 8601; the deadline
    effort_hours: float = 1.0  # the agent's own sizing of the whole job
    progress: float = 0.0  # self-reported fraction [0,1] (fallback)
    subtasks: list[dict[str, Any]] = field(default_factory=list)
    """``[{"desc": str, "done": bool}, ...]`` — when present, progress is
    derived from these (objective) rather than the self-reported float."""
    status: str = "open"  # open | held | delivered | missed | withdrawn
    held_order_id: str = ""  # standing order that parked this (status=held)
    held_at: str = ""  # when it was parked
    created_at: str = ""
    delivered_at: str = ""
    artifact: str = ""  # optional proof link (URL / doc id / commit)
    claimed_delivered_at: str = ""  # agent SAID delivered but no artifact found yet
    verification: str = ""  # last reality-test result (evidence, or why unproven)
    escalated_at: str = ""  # set once we've escalated it (idempotent)
    original_due: str = ""  # the FIRST deadline committed to (kept across reschedules)
    reschedule_count: int = 0
    last_reschedule_by: str = ""  # "counterparty" (legit relaxation) | "owner" (self-push)

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
            "held_order_id": self.held_order_id,
            "held_at": self.held_at,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "artifact": self.artifact,
            "claimed_delivered_at": self.claimed_delivered_at,
            "verification": self.verification,
            "escalated_at": self.escalated_at,
            "original_due": self.original_due,
            "reschedule_count": self.reschedule_count,
            "last_reschedule_by": self.last_reschedule_by,
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
            held_order_id=str(d.get("held_order_id") or ""),
            held_at=str(d.get("held_at") or ""),
            created_at=str(d.get("created_at") or ""),
            delivered_at=str(d.get("delivered_at") or ""),
            artifact=str(d.get("artifact") or ""),
            claimed_delivered_at=str(d.get("claimed_delivered_at") or ""),
            verification=str(d.get("verification") or ""),
            escalated_at=str(d.get("escalated_at") or ""),
            original_due=str(d.get("original_due") or ""),
            reschedule_count=int(d.get("reschedule_count") or 0),
            last_reschedule_by=str(d.get("last_reschedule_by") or ""),
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


def _norm(s: str) -> str:
    """Normalise a 'to' / 'what' for dedup: lowercased, punctuation-stripped,
    whitespace-collapsed — so 'Action the directive: Org chart' and 'action the
    directive  org chart.' collapse to the same key."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return " ".join(s.split())


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
# Delivery evidence — the "test before you trust" gate
#
# A commitment is only DONE when something real went out into the world; an
# agent's say-so ("I delivered the org chart") is a self-report it can — and
# does — confabulate. So before we let "delivered" stand, we test reality: is
# there a real artifact backing the claim? Evidence is either an explicit
# artifact reference that looks like a real link / PR / commit / document, or
# a real email in the agent's own SENT record, addressed to the very person
# the commitment is owed to, sent AFTER the commitment was made. No evidence →
# the claim doesn't close it; it's surfaced back so the agent actually delivers.
# ---------------------------------------------------------------------------

# A reference is "real" if it carries a locator: a URL, a GitHub PR/issue, a
# commit sha, or a document filename. Bare prose ("done", "sent it", "see
# above") is NOT an artifact — that's exactly the confabulation we're guarding.
_ARTIFACT_RE = re.compile(
    r"https?://|github\.com|gitlab\.com|/pull/|/issues/|#\d{1,6}\b|"
    r"\b[0-9a-f]{7,40}\b|\b\S+\.(?:pdf|md|docx?|xlsx?|csv|pptx?|txt|json)\b",
    re.IGNORECASE,
)


def _looks_like_artifact(s: str) -> bool:
    s = (s or "").strip()
    return len(s) >= 6 and bool(_ARTIFACT_RE.search(s))


def _addr(s: str) -> str:
    m = re.search(r"[\w.+-]+@[\w.-]+", s or "")
    return m.group(0).lower() if m else (s or "").strip().lower()


def _sent_email_evidence(agent_dir: Path, c: Commitment) -> str:
    """Look for a real sent email to this commitment's counterparty, posted
    after the commitment was made. Returns a short human description, or ''.

    The node moves successfully-sent mail from ``outbox/email/`` to
    ``outbox/email/sent/`` (and failures to ``failed/``). We only count the
    SENT folder — a draft still sitting in the outbox, or one that bounced to
    failed/, is not delivery. The match is by recipient address OR a loose name
    match (commitments are often owed "to Alex" rather than to an address)."""
    sent = Path(agent_dir) / "outbox" / "email" / "sent"
    if not sent.is_dir():
        return ""
    to_addr = _addr(c.to)
    to_name = (c.to or "").strip().lower().split("@")[0].split()[0] if c.to else ""
    try:
        made = datetime.fromisoformat(c.created_at) if c.created_at else None
        if made and made.tzinfo is None:
            made = made.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        made = None
    for p in sorted(sent.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        recips = " ".join(str(d.get(k) or "") for k in ("to", "cc")).lower()
        if to_addr and to_addr in recips:
            hit = True
        elif to_name and len(to_name) >= 3 and to_name in recips:
            hit = True
        else:
            hit = False
        if not hit:
            continue
        # Posted after the commitment was made? (best-effort — file mtime is the
        # backstop when the payload carries no parseable timestamp).
        when = None
        try:
            when = datetime.fromisoformat(str(d.get("queued_at") or d.get("sent_at") or ""))
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            try:
                when = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
            except OSError:
                when = None
        if made and when and when < made:
            continue  # an older email to the same person isn't THIS delivery
        subj = str(d.get("subject") or "").strip()
        return f"email to {c.to}" + (f' — "{subj[:60]}"' if subj else "")
    return ""


def delivery_evidence(agent_dir: Path, c: Commitment) -> tuple[bool, str]:
    """Test reality for a 'delivered' claim. Returns (has_evidence, description).

    Evidence is (a) an explicit artifact reference that looks like a real
    locator (URL / PR / commit / document), or (b) a real sent email to the
    counterparty after the commitment was made. No evidence → the agent SAID
    done but nothing went out."""
    art = (c.artifact or "").strip()
    if _looks_like_artifact(art):
        return True, f"artifact: {art}"
    ev = _sent_email_evidence(agent_dir, c)
    if ev:
        return True, ev
    return False, ""


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


def overtime_can_save(c: Commitment, now: datetime | None = None) -> bool:
    """True when this promise no longer fits normal working hours but IS still
    winnable by extending the day (overtime) — the band where the right
    response is a deliberate choice (push / renegotiate / escalate), never a
    bare "I don't have enough time" complaint. Overdue is past saving; U ≥
    AT_RISK exceeds literal wall-clock, which overtime can't add to."""
    if c.status != "open" or is_overdue(c, now):
        return False
    u = required_utilisation(c, now)
    return NORMAL_PACE_UTILISATION <= u < AT_RISK_UTILISATION


def count_load(commitments: list[Commitment]) -> float:
    """Pressure from the sheer NUMBER of open promises in [0,1] — the
    'too many balls in the air' load, independent of any single deadline.
    Zero up to the comfort line, ramping to full across the span above it."""
    n_open = sum(1 for c in commitments if c.status == "open")
    return _clamp01((n_open - _COUNT_COMFORT) / _COUNT_SPAN)


def felt_pressure(commitments: list[Commitment], now: datetime | None = None) -> float:
    """Aggregate neuro pressure in [0,1] across all open commitments.

    Two sources, blended: the scariest single deadline (max U + a damped tail
    of the rest), AND the count-load of carrying a big stack at all. So a lone
    impossible deadline reads high, a pile of merely-tight ones reads high, and
    just holding many open promises (even if none is urgent yet) carries real
    background pressure that nudges the agent to deliver/decline rather than
    keep taking on more.
    """
    us = sorted((required_utilisation(c, now) for c in commitments), reverse=True)
    us = [u for u in us if u > 0.0]
    deadline_term = 0.0
    if us:
        deadline_term = min(us[0], 1.0) + 0.25 * min(sum(us[1:]), 1.0)
    return _clamp01(deadline_term + _COUNT_WEIGHT * count_load(commitments))


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
    """Create + persist a new commitment.

    Dedup is on (to, what) for any OPEN commitment, REGARDLESS of due — the org
    chart got registered 11× because the model re-stated the same promise with
    a slightly different deadline each cycle and the old (to, what, due) key let
    every variant through. The same promise to the same person is the same
    commitment; re-registering it just reschedules the existing one (keeping its
    id, progress, created_at), it never spawns a copy.

    Dates are NOT silently rewritten — a stated deadline is the agent's word and
    the ledger surfaces it honestly (overdue reads as overdue). The agent's
    *clock* is grounded at the reasoning surface instead (the salience block and
    the reconcile prompt both stamp the real 'now'), so it states the right date
    in the first place rather than having one imposed after the fact."""
    commitments = load(agent_dir)
    now_dt = _now(now)
    due_iso = parse_due(due)
    to_n = _norm(to)
    what_n = _norm(what)
    for c in commitments:
        if c.status == "open" and _norm(c.to) == to_n and _norm(c.what) == what_n:
            # Same promise — reschedule the existing one rather than duplicate.
            if due_iso and due_iso != c.due_at:
                if not c.original_due:
                    c.original_due = c.due_at
                c.due_at = due_iso
            save(agent_dir, commitments)
            return c
    c = Commitment(
        id=uuid.uuid4().hex,
        to=str(to or "").strip(),
        what=str(what or "").strip(),
        due_at=due_iso,
        effort_hours=_safe_float(effort_hours, 1.0),
        subtasks=[{"desc": str(s), "done": False} for s in (subtasks or [])],
        created_at=now_dt.isoformat(),
        original_due=due_iso,
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
    withdrawn: Any = None,
    subtasks_done: list[str] | None = None,
    effort_hours: Any = None,
    artifact: str = "",
    due: Any = None,
    reschedule_by: str = "owner",
    require_evidence: bool = False,
    agent_dir_for_evidence: Path | None = None,
    now: datetime | None = None,
) -> Commitment | None:
    """Update a commitment by id (or, if no id, the most pressing open one).
    Returns the updated commitment, or None if not found.

    - ``delivered=True`` is the ONLY thing that marks a commitment DONE — and
      when ``require_evidence`` is set, it only closes the commitment if reality
      backs the claim (a real artifact reference or a sent email to the
      counterparty). Without evidence it stays OPEN with a ``verification`` note
      so the agent is told "you said done but nothing went out".
    - ``withdrawn=True`` closes it because the requester pulled it (e.g. "no
      rush, the meeting moved") — a clean, no-penalty close, NOT a failure and
      never an escalation.
    - ``due`` reschedules it; ``reschedule_by`` records who moved it:
      "counterparty" = the person it's owed to relaxed it (legitimate, no
      scar) vs "owner" = the agent pushed its own deadline (the suspect case;
      we keep ``original_due`` and bump ``reschedule_count`` so chronic
      slippage is visible).
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
        if new_due and new_due != target.due_at:
            if not target.original_due:
                target.original_due = target.due_at
            target.due_at = new_due
            target.reschedule_count += 1
            target.last_reschedule_by = (
                "counterparty" if str(reschedule_by).lower().startswith("counter") else "owner"
            )
            # A moved deadline re-opens the escalation gate — a fresh clock.
            target.escalated_at = ""
    if withdrawn:
        target.status = "withdrawn"
    if delivered:
        # Verify-before-trust: a 'delivered' claim only closes the commitment
        # if reality backs it — a real artifact reference or a real sent email
        # to the counterparty. Without evidence we DON'T close it; we record
        # that the agent claimed delivery and why it isn't proven, so the next
        # cycle surfaces "you said done but nothing went out" and the agent
        # actually delivers. (require_evidence is off by default so the pure
        # unit-tested math path is unchanged; the fabric handler turns it on.)
        ev_dir = agent_dir_for_evidence or agent_dir
        has_ev, desc = delivery_evidence(ev_dir, target) if require_evidence else (True, "")
        if has_ev:
            target.status = "delivered"
            target.delivered_at = _now(now).isoformat()
            target.claimed_delivered_at = ""
            target.verification = desc or "delivered"
            if not target.subtasks and target.progress < 1.0:
                target.progress = 1.0
        else:
            # Stays OPEN. Record the unverified claim for the salience block.
            target.claimed_delivered_at = _now(now).isoformat()
            target.verification = (
                "claimed delivered but NO artifact found — no sent email to "
                f"{target.to or 'the recipient'} and no document/PR/link attached"
            )
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
