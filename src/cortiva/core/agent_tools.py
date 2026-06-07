"""Native tool schemas for agent actions.

The agent's structured actions (run the rota optimiser, etc.) are exposed
to the model as real OpenAI-style function tools instead of being coaxed
out of a ``---REFLECTION---`` prose suffix. The model returns validated
``tool_calls``; we overlay those onto the existing ``ReflectionSuffix``
fields so the downstream handlers (``_process_reflection`` ->
``_run_schedule_optimization`` etc.) are unchanged.

Tool ``name``s match ReflectionSuffix action fields, so adding a new
tool-callable action is just a schema here + (already-existing) handler.
"""

from __future__ import annotations

from typing import Any

OPTIMIZE_SCHEDULE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "optimize_schedule",
        "description": (
            "Run the workforce rota optimiser and (by default) apply the "
            "result. The tool guarantees a feasible rota — it never breaches "
            "the capacity ceiling, overruns an agent's hour budget, leaves a "
            "report without manager oversight, or starves a role. Call this "
            "to actually change the schedule; describing it in prose does "
            "nothing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "capacity_ceiling": {
                    "type": "integer",
                    "description": "Max agents concurrently on-shift in any slot.",
                },
                "day_start": {"type": "number", "description": "Earliest hour (0-24)."},
                "day_end": {"type": "number", "description": "Latest hour (0-24)."},
                "w_overtime": {"type": "number", "description": "Weight: relieve overworked agents."},
                "w_blocked": {"type": "number", "description": "Weight: protect manager oversight."},
                "w_spread": {"type": "number", "description": "Weight: keep people overlapping (lower to spread)."},
                "w_peak": {"type": "number", "description": "Weight: leave headroom under the ceiling."},
                "w_preference": {"type": "number", "description": "Weight: honour preferred start times."},
                "apply": {
                    "type": "boolean",
                    "description": "true to apply the rota, false for a dry-run preview.",
                },
            },
            "required": ["capacity_ceiling"],
        },
    },
}

# Tool name -> the ReflectionSuffix field it populates.
_TOOL_TO_SUFFIX_FIELD = {
    "optimize_schedule": "optimize_schedule",
}


def tools_for_agent(agent_id: str, *, scheduling_authorised: set[str]) -> list[dict[str, Any]]:
    """Return the tool schemas an agent is allowed to call.

    Authority-scoped: only scheduling-authorised agents are offered the
    rota optimiser, so the model isn't tempted to call a tool it can't use.
    """
    tools: list[dict[str, Any]] = []
    if agent_id in scheduling_authorised:
        tools.append(OPTIMIZE_SCHEDULE_TOOL)
    return tools


def apply_tool_calls_to_suffix(suffix: Any, tool_calls: list[dict[str, Any]]) -> Any:
    """Overlay native tool_calls onto a ReflectionSuffix.

    tool_calls take precedence over anything the prose suffix carried for
    the same action — they're the structured, validated source.
    """
    for call in tool_calls or []:
        name = call.get("name", "")
        args = call.get("arguments")
        field = _TOOL_TO_SUFFIX_FIELD.get(name)
        if field and isinstance(args, dict):
            setattr(suffix, field, args)
    return suffix
