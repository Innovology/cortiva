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
                "w_overtime": {
                    "type": "number",
                    "description": "Weight: relieve overworked agents.",
                },
                "w_blocked": {
                    "type": "number",
                    "description": "Weight: protect manager oversight.",
                },
                "w_spread": {
                    "type": "number",
                    "description": "Weight: keep people overlapping (lower to spread).",
                },
                "w_peak": {
                    "type": "number",
                    "description": "Weight: leave headroom under the ceiling.",
                },
                "w_preference": {
                    "type": "number",
                    "description": "Weight: honour preferred start times.",
                },
                "apply": {
                    "type": "boolean",
                    "description": "true to apply the rota, false for a dry-run preview.",
                },
            },
            "required": ["capacity_ceiling"],
        },
    },
}

REBALANCE_NODES_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "rebalance_nodes",
        "description": (
            "Produce a plan to reshuffle agents between compute nodes "
            "(e.g. relieve a pressured Mini-2 by moving eligible agents to "
            "Mini-1) using the infrastructure team's node metrics. The plan "
            "is feasible by construction — it only ever moves a *sleeping* "
            "agent, never places an agent on a node below its deployment "
            "grade, never breaches a target's slot capacity or RAM headroom, "
            "respects a per-agent move cooldown, and caps moves per cycle. "
            "By default this is advisory (returns the plan); set apply=true "
            "to request execution once the executor is enabled."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ram_headroom_gb": {
                    "type": "number",
                    "description": "GB of RAM a target node must retain after a move (default 4).",
                },
                "max_moves": {
                    "type": "integer",
                    "description": "Maximum agents to relocate in one cycle (default 3).",
                },
                "pressure_threshold": {
                    "type": "number",
                    "description": (
                        "SRE pressure (0..1) at/above which a node is treated as pressured"
                        " (default 0.85)."
                    ),
                },
                "apply": {
                    "type": "boolean",
                    "description": (
                        "true to request execution of the plan, false (default) for an advisory"
                        " plan only."
                    ),
                },
            },
            "required": [],
        },
    },
}

SCHEDULE_HEALTH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "schedule_health",
        "description": (
            "Measure how RESPONSIVE the current workforce rota is — your eyes "
            "before you tune it. Returns a 0-100 responsiveness score plus "
            "ranked hotspots: hours with nobody awake (coverage gaps), reports "
            "who never overlap their manager (blocked waiting), peers who never "
            "overlap (handoffs serialise), and chronic overtime. Read this, "
            "pick the worst hotspot, then optimise that ONE role's schedule. "
            "Measures only — it changes nothing."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

RECOMMEND_SCHEDULE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "recommend_schedule",
        "description": (
            "Optimise ONE role's schedule for overall company responsiveness — "
            "the steady-state tweak. Holding everyone else fixed, it finds the "
            "re-timing of the target role (or, by default, the role behind the "
            "worst schedule-health hotspot) that most raises the responsiveness "
            "score, and reports the change + the gain. Set apply=true to enact "
            "just that one role's new schedule. Tune the company one role at a "
            "time, repeatedly — don't re-rota everyone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Agent id to re-time. Omit to auto-pick the worst hotspot's role."
                    ),
                },
                "apply": {
                    "type": "boolean",
                    "description": (
                        "true to enact the recommended single-role change,"
                        " false (default) to preview."
                    ),
                },
            },
            "required": [],
        },
    },
}

CULTURE_HEALTH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "culture_health",
        "description": (
            "Measure how HEALTHY the company culture currently is — your eyes "
            "on how the workforce actually feels. Reads each agent's rolling "
            "emotional state and the diversity of voice across the org, and "
            "returns a 0-100 culture-health score plus ranked hotspots: who is "
            "distressed or at burnout risk, who's operating in fear, who's "
            "disengaged, whose voice goes unheard, and whether the org is "
            "drifting toward a monoculture. Read this, find who's struggling, "
            "then decide the intervention yourself — it measures only and "
            "changes nothing."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

EFFICIENCY_REVIEW_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "efficiency_review",
        "description": (
            "Measure how efficiently the workforce is performing over time — "
            "your eyes as the Workforce Performance Analyst. Returns a per-agent "
            "read (throughput, quality, cost-efficiency, sustainability), a "
            "0-100 composite + its trend vs last review, and ranked hotspots: "
            "who's declining, who's at risk (low quality or burning out), who's "
            "a standout. Read it, then reason about WHY and act — the score is a "
            "ranking aid, not the verdict. Measures only; changes nothing."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# Tool name -> the ReflectionSuffix field it populates.
_TOOL_TO_SUFFIX_FIELD = {
    "optimize_schedule": "optimize_schedule",
    "rebalance_nodes": "rebalance_nodes",
    "schedule_health": "schedule_health",
    "recommend_schedule": "recommend_schedule",
    "culture_health": "culture_health",
    "efficiency_review": "efficiency_review",
}


def tools_for_agent(
    agent_id: str,
    *,
    scheduling_authorised: set[str],
    culture_authorised: set[str] | None = None,
    performance_authorised: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the tool schemas an agent is allowed to call.

    Authority-scoped: only scheduling-authorised agents are offered the rota
    optimiser, the node rebalancer, the schedule-health readout, and the
    single-role recommendation; only culture-authorised agents are offered the
    culture-health readout; only performance-authorised agents are offered the
    workforce-efficiency review — so the model isn't tempted to call a tool it
    can't use.
    """
    tools: list[dict[str, Any]] = []
    if agent_id in scheduling_authorised:
        tools.append(OPTIMIZE_SCHEDULE_TOOL)
        tools.append(REBALANCE_NODES_TOOL)
        tools.append(SCHEDULE_HEALTH_TOOL)
        tools.append(RECOMMEND_SCHEDULE_TOOL)
    if culture_authorised and agent_id in culture_authorised:
        tools.append(CULTURE_HEALTH_TOOL)
    if performance_authorised and agent_id in performance_authorised:
        tools.append(EFFICIENCY_REVIEW_TOOL)
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
