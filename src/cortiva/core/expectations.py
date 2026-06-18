"""Expectations ledger — deliverables the agent is WAITING ON from someone
else (the other side of a commitment).

A *commitment* is something you owe (see ``commitments.py``) — it drives
cortisol, overtime, escalation. An *expectation* is the mirror: something
someone owes YOU. It must not feel like cortisol — being owed a thing isn't
the stress of owing one. It's a quieter signal: vigilance, and mild
frustration if the deadline is near (or past) and you've heard nothing —
the cue to chase, not to pull an all-nighter.

So the pressure model here is deliberately different from commitments. There
is no work-remaining/effort (it isn't your work). What matters is:

    overdue-or-imminent  AND  silent  ->  chase

An expectation resolves when the awaited thing arrives — modelled, like the
directive register, as an inbound message from the person who owes it landing
after the expectation was opened — or when the agent explicitly marks it
received / drops it.

This module is PURE (JSON file + math only) so it's unit-testable and the
node's extraction pass and the fabric's salience/neuro both reuse it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

EXPECTATIONS_FILENAME = "expectations.json"

# An expectation becomes a "chase" once it's within this window of its due
# time (imminent) or past it — and nothing has arrived. Before that it sits
# dormant: you don't badger someone about a thing that isn't due yet.
_CHASE_LEAD_HOURS = 6.0

# Drop an expectation that's been unmet this long past due — at some point
# it's stale, not worth chasing (it self-cleans, like the directive TTL).
_STALE_DAYS = 14.0

_EOD_HOUR = 17


@dataclass
class Expectation:
    """Something the agent is waiting to receive from someone, by a date."""

    id: str
    sender: str = ""  # who owes it to the agent (email or name)
    what: str = ""  # what's awaited
    due_at: str = ""  # ISO 8601
    status: str = "open"  # open | received | dropped | withdrawn
    created_at: str = ""
    received_at: str = ""
    chased_at: str = ""  # last time we surfaced a chase (idempotent nudges)
    original_due: str = ""
    reschedule_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sender": self.sender,
            "what": self.what,
            "due_at": self.due_at,
            "status": self.status,
            "created_at": self.created_at,
            "received_at": self.received_at,
            "chased_at": self.chased_at,
            "original_due": self.original_due,
            "reschedule_count": self.reschedule_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Expectation:
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex),
            sender=str(d.get("sender") or ""),
            what=str(d.get("what") or ""),
            due_at=str(d.get("due_at") or ""),
            status=str(d.get("status") or "open"),
            created_at=str(d.get("created_at") or ""),
            received_at=str(d.get("received_at") or ""),
            chased_at=str(d.get("chased_at") or ""),
            original_due=str(d.get("original_due") or ""),
            reschedule_count=int(d.get("reschedule_count") or 0),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


def parse_due(value: Any) -> str:
    """Normalise a due value to ISO (bare date → EOD). '' if unparseable —
    an expectation with no date is still tracked, it just never goes 'overdue'."""
    import re

    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.isoformat()
    s = str(value or "").strip()
    if not s:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s = f"{s}T{_EOD_HOUR:02d}:00:00"
    try:
        dt = datetime.fromisoformat(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()
    except ValueError:
        return ""


def _due_dt(e: Expectation) -> datetime | None:
    if not e.due_at:
        return None
    try:
        dt = datetime.fromisoformat(e.due_at)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def hours_to_due(e: Expectation, now: datetime | None = None) -> float:
    due = _due_dt(e)
    if due is None:
        return float("inf")
    return (due - _now(now)).total_seconds() / 3600.0


def is_overdue(e: Expectation, now: datetime | None = None) -> bool:
    return e.status == "open" and hours_to_due(e, now) <= 0.0


def should_chase(e: Expectation, now: datetime | None = None) -> bool:
    """True when it's worth chasing: open, and imminent (within the lead
    window) or already overdue. Dormant before that — don't nag early."""
    if e.status != "open":
        return False
    h = hours_to_due(e, now)
    if h == float("inf"):
        return False  # no date → nothing to be late against
    return h <= _CHASE_LEAD_HOURS


def chase_pressure(expectations: list[Expectation], now: datetime | None = None) -> float:
    """A small [0,1] 'someone owes me and it's late' signal for the chase
    register — NOT cortisol. Scales with how many are due-and-silent and how
    overdue the worst one is, but caps low: this is irritation, not panic."""
    chasing = [e for e in expectations if should_chase(e, now)]
    if not chasing:
        return 0.0
    worst_overdue_h = max((max(0.0, -hours_to_due(e, now)) for e in chasing), default=0.0)
    # 0 at due time, ~0.5 a day overdue, saturating; + a touch per extra item.
    base = min(0.6, worst_overdue_h / 48.0)
    return min(1.0, base + 0.1 * (len(chasing) - 1))


def summarise(expectations: list[Expectation], now: datetime | None = None) -> dict[str, Any]:
    opens = [e for e in expectations if e.status == "open"]
    chasing = [e for e in opens if should_chase(e, now)]
    chasing.sort(key=lambda e: hours_to_due(e, now))
    top = chasing[0] if chasing else None
    return {
        "open": len(opens),
        "to_chase": len(chasing),
        "overdue": sum(1 for e in opens if is_overdue(e, now)),
        "chase_pressure": round(chase_pressure(opens, now), 3),
        "top_from": top.sender if top else "",
        "top_what": top.what if top else "",
        "top_due_at": top.due_at if top else "",
    }


# ---------------------------------------------------------------------------
# Ledger IO + mutations
# ---------------------------------------------------------------------------


def _path(agent_dir: Path) -> Path:
    return Path(agent_dir) / EXPECTATIONS_FILENAME


def load(agent_dir: Path) -> list[Expectation]:
    p = _path(agent_dir)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) or []
    except (ValueError, OSError):
        return []
    return [Expectation.from_dict(d) for d in raw if isinstance(d, dict)]


def save(agent_dir: Path, expectations: list[Expectation]) -> None:
    try:
        _path(agent_dir).write_text(
            json.dumps([e.to_dict() for e in expectations], indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def register(
    agent_dir: Path,
    *,
    sender: str,
    what: str,
    due: Any,
    now: datetime | None = None,
) -> Expectation:
    """Create + persist a new expectation. Idempotent on (sender, what, due)."""
    items = load(agent_dir)
    due_iso = parse_due(due)
    key = (str(sender).strip().lower(), str(what).strip().lower(), due_iso)
    for e in items:
        if (
            e.sender.strip().lower(),
            e.what.strip().lower(),
            e.due_at,
        ) == key and e.status == "open":
            return e
    e = Expectation(
        id=uuid.uuid4().hex,
        sender=str(sender or "").strip(),
        what=str(what or "").strip(),
        due_at=due_iso,
        created_at=_now(now).isoformat(),
        original_due=due_iso,
    )
    items.append(e)
    save(agent_dir, items)
    return e


def update(
    agent_dir: Path,
    *,
    expectation_id: str = "",
    due: Any = None,
    withdrawn: Any = None,
    received: Any = None,
    now: datetime | None = None,
) -> Expectation | None:
    """Update an expectation by id: reschedule (``due``), or close it as
    ``withdrawn`` (the ask was pulled) / ``received`` (it arrived). Returns the
    updated expectation or None. Reschedule keeps ``original_due`` + bumps the
    count so a chronically-slipping promise from someone else is visible."""
    items = load(agent_dir)
    target = None
    cid = (expectation_id or "").strip()
    for e in items:
        if cid and (e.id == cid or e.id.startswith(cid)):
            target = e
            break
    if target is None and not cid:
        opens = [e for e in items if e.status == "open"]
        target = opens[0] if opens else None
    if target is None:
        return None
    if due is not None:
        new_due = parse_due(due)
        if new_due and new_due != target.due_at:
            if not target.original_due:
                target.original_due = target.due_at
            target.due_at = new_due
            target.reschedule_count += 1
    if withdrawn:
        target.status = "withdrawn"
    if received:
        target.status = "received"
        target.received_at = _now(now).isoformat()
    save(agent_dir, items)
    return target


def mark_received(
    agent_dir: Path, expectation_id: str, now: datetime | None = None
) -> Expectation | None:
    items = load(agent_dir)
    for e in items:
        if e.id == expectation_id or e.id.startswith(expectation_id):
            e.status = "received"
            e.received_at = _now(now).isoformat()
            save(agent_dir, items)
            return e
    return None


def resolve_from_inbox(
    agent_dir: Path,
    *,
    senders_seen: dict[str, float],
    now: datetime | None = None,
) -> int:
    """Auto-resolve + self-clean. An open expectation is *received* once a
    message from its sender has landed since it opened (mirrors how a directive
    resolves when the agent replies). Long-overdue-unmet ones are dropped.

    ``senders_seen`` maps a normalised sender address -> latest inbound epoch
    seconds (the caller builds this from the agent's inbox, where the heavy
    lifting / file access lives). Returns the number resolved or dropped.
    """
    import re

    def _addr(s: str) -> str:
        m = re.search(r"[\w.+-]+@[\w.-]+", s or "")
        return m.group(0).lower() if m else (s or "").strip().lower()

    items = load(agent_dir)
    n = _now(now)
    changed = 0
    for e in items:
        if e.status != "open":
            continue
        try:
            opened = datetime.fromisoformat(e.created_at)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            opened = n
        seen_t = senders_seen.get(_addr(e.sender), 0.0)
        if seen_t and seen_t >= opened.timestamp():
            e.status = "received"
            e.received_at = n.isoformat()
            changed += 1
            continue
        due = _due_dt(e)
        if due is not None and (n - due) > timedelta(days=_STALE_DAYS):
            e.status = "dropped"
            changed += 1
    if changed:
        save(agent_dir, items)
    return changed
