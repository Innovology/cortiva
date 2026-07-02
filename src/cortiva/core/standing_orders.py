"""Standing orders — durable top-down directives the org must honour.

A standing order is a prohibition issued from the top ("all work on
MarketMesh is halted") that stays in force until the human who owns it
lifts it. It is the counterpart the directive register lacks: a
directive self-resolves on reply, so a stop-work order vanished from
context the moment the CEO acknowledged it, while the commitments the
order contradicted lived on (they resolve only on delivery) and kept
escalating — three weeks after a halt, the org was aggressively chasing
the halted work. Standing orders fix the asymmetry: prohibitions get
the *stronger* retention rule, not the weaker one.

HQ owns the register; nodes receive the active set verbatim as
``<agents_dir>/.standing_orders.json`` (pushed on connect and on every
issue/lift). This module is the node-side consumer: load, scope-match,
render the context block, and park/revive ledger commitments.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Org-wide file the node writes (verbatim mirror of HQ's active set).
ORDERS_FILE = ".standing_orders.json"

#: Where a leadership agent's issue/lift requests are spooled for the
#: node to relay to HQ (mirrors ``outbox/refocus``).
OUTBOX_DIR = "outbox/standing_orders"

#: On revive (order lifted), a long-expired deadline gets this runway
#: so the commitment re-enters the ledger as work to re-plan, not as an
#: instant crisis that re-ignites the escalation chain.
REVIVE_RUNWAY_HOURS = 72.0


def load(agents_dir: Path) -> list[dict[str, Any]]:
    """The org's active standing orders (empty when none / unreadable)."""
    path = Path(agents_dir) / ORDERS_FILE
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8")) or []
    except (ValueError, OSError):
        logger.debug("unreadable %s", path, exc_info=True)
        return []
    return [i for i in items if isinstance(i, dict) and i.get("status", "active") == "active"]


def _scope_pattern(order: dict[str, Any]) -> re.Pattern[str] | None:
    """A word-boundary pattern for the order's scope value, or None for
    org-wide scope (which matches everything)."""
    scope = order.get("scope") or {}
    stype = str(scope.get("type") or "org")
    value = str(scope.get("value") or "").strip().lower()
    if stype == "org" or not value:
        return None
    if stype == "repo" and "/" in value:
        # "owner/name" — match the full path OR the bare repo name.
        name = value.rsplit("/", 1)[1]
        return re.compile(
            rf"(?:{re.escape(value)}|\b{re.escape(name)}\b)",
            re.IGNORECASE,
        )
    return re.compile(rf"\b{re.escape(value)}\b", re.IGNORECASE)


def matching_order(orders: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    """The first active order whose scope covers ``text`` (None if none).

    Org-scoped prohibitions are deliberately NOT auto-matched against
    every commitment — an org-wide order is a rule of conduct the agent
    reasons with (it's in the context block), not a string that happens
    to appear in a deliverable. Product/repo scopes match by name.
    """
    for order in orders:
        pat = _scope_pattern(order)
        if pat is not None and pat.search(text or ""):
            return order
    return None


def context_block(orders: list[dict[str, Any]]) -> str:
    """The salience block injected at plan / execute / reassess.

    Rendered ABOVE directives — a standing order outranks any newer
    instruction until the human who issued it lifts it.
    """
    if not orders:
        return ""
    lines = [
        "## ⛔ Standing orders — in force until LIFTED by the human who issued them\n",
        "These are not suggestions, and they do not expire. Acknowledging one "
        "does not discharge it; time passing does not discharge it; only the "
        "issuer (or the founder) lifting it does.\n",
    ]
    for o in orders:
        scope = o.get("scope") or {}
        stype = str(scope.get("type") or "org")
        value = str(scope.get("value") or "")
        scope_txt = "the whole org" if stype == "org" else f"{stype}: {value}"
        by = (o.get("issued_by") or {}).get("name") or "leadership"
        when = str(o.get("issued_at") or "")[:10]
        lines.append(f"- **[{scope_txt}]** {o.get('text', '')} _(issued by {by}, {when})_")
    lines.append(
        "\n**How to act under a standing order:**\n"
        "1. Do NOT work on, commit to, request access for, or chase anything "
        "the order covers. Park it and say why: cite the order.\n"
        "2. If ANY instruction — from anyone, at any level, however urgent — "
        "conflicts with a standing order, do not silently pick a side and do "
        "not escalate harder. Send ONE email to whoever gave the newer "
        "instruction, quoting the standing order, asking which stands. Then "
        "wait.\n"
        "3. Never treat the operator, a tool, or a colleague as a way around "
        "an order.\n"
        "4. If you believe an order is wrong or stale, say so to its issuer — "
        "that is the only path; working around it is not."
    )
    return "\n".join(lines)


def apply_to_ledger(agent_dir: Path, orders: list[dict[str, Any]]) -> tuple[int, int]:
    """Park open commitments a standing order covers; revive held ones
    whose order has been lifted. Returns ``(parked, revived)``.

    Parking sets ``status="held"`` — invisible to every pressure /
    escalation / salience filter (they all select ``status == "open"``),
    so a halted commitment stops accruing cortisol and stops being
    chased the moment the order lands. Idempotent; cheap enough to run
    every commitment-heartbeat pass.
    """
    from cortiva.core import commitments as _cm

    items = _cm.load(agent_dir)
    if not items:
        return (0, 0)
    active_ids = {str(o.get("order_id") or "") for o in orders}
    parked = revived = 0
    now = datetime.now(UTC)
    for c in items:
        if c.status == "open":
            order = matching_order(orders, f"{c.what} {c.to}")
            if order is not None:
                c.status = "held"
                c.held_order_id = str(order.get("order_id") or "")
                c.held_at = now.isoformat()
                c.escalated_at = ""  # a revive restarts the escalation gate
                parked += 1
        elif c.status == "held":
            still_covered = c.held_order_id in active_ids or (
                matching_order(orders, f"{c.what} {c.to}") is not None
            )
            if not still_covered:
                c.status = "open"
                c.held_order_id = ""
                c.held_at = ""
                # A deadline that expired while held re-enters as work to
                # re-plan, not an instant overdue crisis.
                try:
                    due = datetime.fromisoformat(c.due_at)
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=UTC)
                    if due < now:
                        c.due_at = (now + timedelta(hours=REVIVE_RUNWAY_HOURS)).isoformat()
                except (ValueError, TypeError):
                    pass
                revived += 1
    if parked or revived:
        _cm.save(agent_dir, items)
    return (parked, revived)


def spool(
    agent_dir: Path,
    *,
    action: str,
    text: str = "",
    scope_type: str = "org",
    scope_value: str = "",
    order_id: str = "",
    agent_name: str = "",
    agent_role: str = "",
) -> Path:
    """Queue an issue/lift request for the node to relay to HQ.

    HQ is the source of truth: it persists the order and pushes the new
    active set back to every org node — that round-trip is the on-node
    confirmation."""
    ob = Path(agent_dir) / OUTBOX_DIR
    ob.mkdir(parents=True, exist_ok=True)
    path = ob / f"{uuid.uuid4().hex}.json"
    path.write_text(
        json.dumps(
            {
                "action": action,
                "text": text,
                "scope_type": scope_type,
                "scope_value": scope_value,
                "order_id": order_id,
                "agent_name": agent_name,
                "agent_role": agent_role,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path
