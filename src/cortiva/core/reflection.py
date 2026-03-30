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
    )

    return ReflectionResult(clean_content=clean_content, suffix=suffix)
