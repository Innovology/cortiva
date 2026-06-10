"""Workforce analytics — the AR function's instruments on the workforce.

The Workforce Performance Analyst (an AR role) measures how efficiently the
agents are working over time, the way the AR Scheduler measures the rota and
the People & Culture Lead measures culture. This package is the **measurement**
half — a pure, deterministic readout of per-agent efficiency (throughput,
quality, cost-efficiency, sustainability) with trend + ranked hotspots. The
*judgement* — is this agent improving, and why — stays with the analyst
(reason over the data; don't let a single number be the verdict).
"""

from cortiva.workforce.efficiency import (
    AgentEfficiency,
    AgentEfficiencyInput,
    EfficiencyHotspot,
    WorkforceEfficiency,
    assess_workforce_efficiency,
)

__all__ = [
    "AgentEfficiency",
    "AgentEfficiencyInput",
    "EfficiencyHotspot",
    "WorkforceEfficiency",
    "assess_workforce_efficiency",
]
