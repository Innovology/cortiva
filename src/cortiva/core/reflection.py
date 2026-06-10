"""
Reflection suffix parsing for Cortiva task execution.

After an agent executes a task via consciousness.think(), the response
may include a structured JSON suffix delimited by ``---REFLECTION---``.
This module extracts that suffix into actionable metadata (learnings,
prediction errors, procedure updates, inter-agent messages, escalations).

If the delimiter or valid JSON is absent, parsing is graceful — the
original content is returned unchanged with suffix=None.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

REFLECTION_DELIMITER = "\n---REFLECTION---\n"


@dataclass
class ReflectionSuffix:
    """Structured metadata extracted from a reflection suffix."""

    outcome: str | None = None
    learned: str | None = None
    prediction_error: str | None = None
    procedure_update: str | None = None
    messages: list[dict[str, str]] = field(default_factory=list)
    escalation: str | None = None
    delegate: list[dict[str, Any]] = field(default_factory=list)
    """Work assignments to create for subordinates."""
    complete_assignment: str | None = None
    """Assignment ID to mark as completed."""
    shared_learning: str | None = None
    """Knowledge to write to the org-wide shared memory tier."""
    schedule: dict[str, Any] = field(default_factory=dict)
    """Self-scheduling requests: overtime, early sleep, alarms, reminders."""
    deep_think: str | None = None
    """A question the agent wants frontier-model help with — hard
    reasoning it can't resolve on the local model, or a second opinion
    on its own conclusion. The runtime subshells to the ``claude`` CLI
    (claude_code_deep_think skill) and feeds the answer back into the
    agent's memory. Budget-gated; this is the expensive path."""
    hire: dict[str, Any] | None = None
    """A request to bring on a new team member: ``{"role": ...,
    "department": ..., "justification": ...}``. Only honoured for
    agents with hiring authority (the CEO commands, the COO provisions).
    The runtime generates the new colleague and boots them."""
    email: dict[str, Any] | None = None
    """A request to SEND an email: ``{"to": "<address>", "subject": "...",
    "body": "...", "in_reply_to": "<message_id>"}``. The runtime queues it
    to the agent's outbox; the node sends it via Resend as the agent's own
    ``<first-name>@<workforce-domain>`` address. Used to reply to received
    mail, email a colleague, or email a founder (manager first)."""
    document: dict[str, Any] | None = None
    """A request to SAVE a document to the company document store:
    ``{"title": "...", "content": "...", "visibility": "private|department|
    org", "department": "<dept, optional>", "filename": "<optional>",
    "tags": [..], "description": "..."}``. The runtime queues it to the
    agent's outbox; the node hands it to HQ, which stores the bytes in the
    org's object store (MinIO) with the agent as owner. Use it to publish a
    report, a record, or anything a colleague may need to read later."""
    optimize_schedule: dict[str, Any] | None = None
    """A request to run the workforce rota optimiser and apply the result.
    Only honoured for agents with scheduling authority (the AR Scheduler /
    Head of AR / COO). Optional keys steer the optimiser without letting
    the agent hand-edit rows: ``capacity_ceiling``, ``day_start``,
    ``day_end``, and objective weights ``w_peak``/``w_blocked``/
    ``w_overtime``/``w_spread``/``w_preference``, plus ``apply`` (default
    true; false = dry-run preview only). The tool guarantees feasibility;
    the runtime refuses to apply an infeasible proposal."""
    schedule_health: dict[str, Any] | None = None
    """A request to MEASURE the current rota's responsiveness (coverage gaps,
    oversight/peer overlap, overtime) and record a ranked-hotspot readout.
    Only honoured for agents with scheduling authority. Measures only — the
    AR Scheduler reads it, then optimises one role. Payload is ignored."""
    efficiency_review: dict[str, Any] | None = None
    """A request to MEASURE workforce efficiency over time (throughput,
    quality, cost-efficiency, sustainability per agent + trend + ranked
    hotspots). Only honoured for agents with performance authority (the
    Workforce Performance Analyst / Head of AR / COO). Measures only — the
    analyst reads it, reasons about why, then acts. Payload is ignored."""
    culture_health: dict[str, Any] | None = None
    """A request to MEASURE company culture health from the workforce's felt
    state (emotions) and diversity of voice, recording a 0-100 score + ranked
    hotspots (distress, burnout, fear, disengagement, unheard voices,
    monoculture). Only honoured for agents with culture authority (the People &
    Culture Lead / Head of AR / COO). Measures only — she reads it, then
    decides the intervention. Payload is ignored."""
    recommend_schedule: dict[str, Any] | None = None
    """A request to optimise ONE role's schedule for company responsiveness:
    ``{"target": "<agent_id>"?, "apply": <bool>}``. Scheduling-authority only.
    Holds everyone else fixed and re-times just the target (or the worst
    hotspot's role); ``apply`` enacts that single-role change. The
    steady-state tweak — one role at a time, repeatedly."""
    rebalance_nodes: dict[str, Any] | None = None
    """A request to plan a reshuffle of agents between compute nodes from
    the infra team's metrics: ``{"ram_headroom_gb": ..., "max_moves": ...,
    "pressure_threshold": ..., "apply": <bool>}``. Only honoured for agents
    with scheduling authority. The planner is feasible by construction — it
    only moves *sleeping* agents, never below an agent's deployment grade,
    never past a target's slots/RAM headroom, respects a move cooldown, and
    caps moves per cycle. Phase 1 is advisory (``apply`` ignored until the
    executor is enabled)."""


@dataclass
class ReflectionResult:
    """Result of parsing a consciousness response for reflection data."""

    clean_content: str
    suffix: ReflectionSuffix | None = None


def parse_reflection_suffix(text: str) -> ReflectionResult:
    """Parse a consciousness response, extracting any reflection suffix.

    Splits on ``REFLECTION_DELIMITER``. If the delimiter is present,
    the text after it is parsed as JSON (with optional markdown code
    fences stripped). On any parse error the suffix is ``None`` and
    the full original text is returned as ``clean_content``.
    """
    if not text or REFLECTION_DELIMITER not in text:
        return ReflectionResult(clean_content=text)

    parts = text.split(REFLECTION_DELIMITER, 1)
    clean_content = parts[0]
    raw_json = parts[1].strip()

    # Strip optional markdown code fences (```json ... ``` or ``` ... ```)
    raw_json = re.sub(r"^```(?:json)?\s*\n?", "", raw_json)
    raw_json = re.sub(r"\n?```\s*$", "", raw_json)
    raw_json = raw_json.strip()

    try:
        data: Any = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError):
        return ReflectionResult(clean_content=text)

    if not isinstance(data, dict):
        return ReflectionResult(clean_content=text)

    messages_raw = data.get("messages", [])
    messages = messages_raw if isinstance(messages_raw, list) else []

    delegate_raw = data.get("delegate", [])
    delegate = delegate_raw if isinstance(delegate_raw, list) else []

    suffix = ReflectionSuffix(
        outcome=data.get("outcome"),
        learned=data.get("learned"),
        prediction_error=data.get("prediction_error"),
        procedure_update=data.get("procedure_update"),
        messages=messages,
        escalation=data.get("escalation"),
        delegate=delegate,
        complete_assignment=data.get("complete_assignment"),
        shared_learning=data.get("shared_learning"),
        schedule=data.get("schedule") or {},
        deep_think=data.get("deep_think"),
        hire=data.get("hire") if isinstance(data.get("hire"), dict) else None,
        email=data.get("email") if isinstance(data.get("email"), dict) else None,
        document=data.get("document") if isinstance(data.get("document"), dict) else None,
        optimize_schedule=(
            data.get("optimize_schedule")
            if isinstance(data.get("optimize_schedule"), dict)
            else None
        ),
        rebalance_nodes=(
            data.get("rebalance_nodes")
            if isinstance(data.get("rebalance_nodes"), dict)
            else None
        ),
        recommend_schedule=(
            data.get("recommend_schedule")
            if isinstance(data.get("recommend_schedule"), dict)
            else None
        ),
        culture_health=(
            data.get("culture_health")
            if isinstance(data.get("culture_health"), dict)
            else None
        ),
        efficiency_review=(
            data.get("efficiency_review")
            if isinstance(data.get("efficiency_review"), dict)
            else None
        ),
        schedule_health=(
            data.get("schedule_health")
            if isinstance(data.get("schedule_health"), dict)
            else None
        ),
    )

    return ReflectionResult(clean_content=clean_content, suffix=suffix)
