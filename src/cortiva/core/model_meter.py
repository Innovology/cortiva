"""Model meter — how many times an agent asked a model today, and a line
that puts the count in front of her.

"Myelin is designed so that it is a storage optimised for agent thinking,
context, awareness, planning… there should never be an event in the LLM
that an agent needs to brain dump everything… hundreds of small asks that
build and adjust the persisted knowledge graph" (founder, 2026-11-06).

The fabric counts every consciousness call per agent per day in
``today/model_calls.json`` (``{date, count, by_type}``), by call type —
plan, replan, execute, reflect — and the count goes into the agent's
context next to the soft daily budget, so the agent (and the operator over
IPC) can see when thinking is being spent on re-asking what Myelin already
holds. Frontier escalations (deep_think) are counted separately in
``today/external_calls.json`` and shown alongside. Nothing here blocks a
call: the budget is a felt limit, like a human's attention, not a breaker.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

METER_FILE = "model_calls.json"
EXTERNAL_FILE = "external_calls.json"
DEFAULT_BUDGET = 80
NEAR_BUDGET = 0.75


def budget() -> int:
    """Soft daily budget of model asks per agent (``CORTIVA_MODEL_CALL_BUDGET``)."""
    try:
        return max(1, int(os.environ.get("CORTIVA_MODEL_CALL_BUDGET", DEFAULT_BUDGET)))
    except ValueError:
        return DEFAULT_BUDGET


def _path(agent_dir: Path) -> Path:
    return Path(agent_dir) / "today" / METER_FILE


def read(agent_dir: Path, *, today: str | None = None) -> dict[str, Any]:
    """Today's meter; a fresh one when the file is missing or from another day."""
    today = today or date.today().isoformat()
    try:
        raw = json.loads(_path(agent_dir).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("date") == today:
            raw.setdefault("count", 0)
            raw.setdefault("by_type", {})
            return raw
    except (OSError, ValueError):
        pass
    return {"date": today, "count": 0, "by_type": {}}


def bump(agent_dir: Path, call_type: str, *, today: str | None = None) -> dict[str, Any]:
    """Count one model ask of ``call_type``; returns the updated meter."""
    m = read(agent_dir, today=today)
    kind = (call_type or "default").strip() or "default"
    m["count"] = int(m.get("count") or 0) + 1
    by_type = m.get("by_type") or {}
    by_type[kind] = int(by_type.get(kind) or 0) + 1
    m["by_type"] = by_type
    try:
        p = _path(agent_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(m), encoding="utf-8")
    except OSError:
        pass
    return m


def add_tokens(
    agent_dir: Path,
    call_type: str,
    tokens_in: int,
    tokens_out: int,
    *,
    today: str | None = None,
) -> dict[str, Any]:
    """Account the tokens an ask cost, by call type — the cost of thinking,
    not just its count. ``tokens_in`` is the context the ask carried."""
    m = read(agent_dir, today=today)
    kind = (call_type or "default").strip() or "default"
    m["tokens_in"] = int(m.get("tokens_in") or 0) + max(0, int(tokens_in or 0))
    m["tokens_out"] = int(m.get("tokens_out") or 0) + max(0, int(tokens_out or 0))
    by_tokens = m.get("tokens_by_type") or {}
    by_tokens[kind] = int(by_tokens.get(kind) or 0) + max(0, int(tokens_in or 0))
    m["tokens_by_type"] = by_tokens
    try:
        p = _path(agent_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(m), encoding="utf-8")
    except OSError:
        pass
    return m


def _k(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def external_count(agent_dir: Path, *, today: str | None = None) -> int:
    """Frontier (Claude) escalations today, from the existing external_calls.json."""
    today = today or date.today().isoformat()
    try:
        raw = json.loads((Path(agent_dir) / "today" / EXTERNAL_FILE).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("date") == today:
            return int(raw.get("count") or 0)
    except (OSError, ValueError):
        pass
    return 0


def context_line(agent_dir: Path, *, today: str | None = None) -> str:
    """One line for the agent's context — empty before the first ask."""
    m = read(agent_dir, today=today)
    n = int(m.get("count") or 0)
    if n <= 0:
        return ""
    b = budget()
    by_type = m.get("by_type") or {}
    parts = ", ".join(f"{k} {v}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]))
    ext = external_count(agent_dir, today=today)
    ext_s = f" + {ext} Claude escalation{'s' if ext != 1 else ''}" if ext else ""
    tin = int(m.get("tokens_in") or 0)
    tok_s = f", ≈{_k(tin)} tokens of context read" if tin else ""
    line = (
        f"🧮 Model asks today: {n} of a soft budget of {b} ({parts}{tok_s}){ext_s}. "
        "Myelin holds your plan, your inbox grounding and the team board — a wake, "
        "a resumed plan or a filed note is not an ask. Spend asks on work the world "
        "will see."
    )
    if n >= b:
        line += (
            " You are over today's budget: finish what is in hand from what you already "
            "know; do not open new lines of thought until something arrives."
        )
    elif n >= NEAR_BUDGET * b:
        line += " You are near today's budget: prefer doing to re-deciding."
    return line
