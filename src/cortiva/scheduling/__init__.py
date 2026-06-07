"""Workforce scheduling — the optimiser tool the AR Scheduler operates.

The scheduler is a deterministic, constraint-respecting optimiser exposed
as a tool. An agent (the AR Scheduler) sets objectives/weights and feeds
signals; the tool produces a *feasible* rota — it can never emit a
schedule that violates the hard invariants, so the agent steers without
being able to break the workforce.
"""

from cortiva.scheduling.optimizer import (
    AgentSpec,
    Constraints,
    ImpactPreview,
    Objectives,
    RoleType,
    ScheduleProposal,
    Signals,
    WorkWindow,
    optimize_schedule,
    windows_to_schedule_config,
)

__all__ = [
    "AgentSpec",
    "Constraints",
    "ImpactPreview",
    "Objectives",
    "RoleType",
    "ScheduleProposal",
    "Signals",
    "WorkWindow",
    "optimize_schedule",
    "windows_to_schedule_config",
]
