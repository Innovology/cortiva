"""Node rebalancer — the AR Scheduler's tool for moving agents between
nodes based on infrastructure metrics from the SRE/infra team.

Given per-node health (RAM headroom, capacity, an optional SRE pressure
signal) and the current agent→node placement, it returns a *plan* of
moves that relieves pressured nodes by relocating eligible agents to
nodes with headroom.

Like the rota optimiser, **hard invariants are enforced by construction** —
a returned move can never:

* relocate an agent that is **awake** (only sleeping agents move — a
  moving agent's state must be consistent),
* place an agent on a node whose **grade is below the agent's** (Grade-1
  agents never land on a Grade-0 node),
* exceed the target node's **slot capacity** or eat its **RAM headroom**,
* move an agent still within its **cooldown** (anti-thrash), or
* exceed **max_moves** per cycle.

Phase 1 is advisory: ``plan_rebalance`` returns the plan; it never moves
anything. The executor (Phase 2) consumes a plan and performs the moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NodeState:
    node_id: str
    grade: int  # deployment tier: hosts agents of grade <= this
    ram_free_gb: float
    ram_total_gb: float
    agents_deployed: int
    agent_slots: int
    name: str = ""
    pressure: float = 0.0  # SRE signal 0..1 (higher = more saturated)

    def free_slots(self) -> int:
        return max(0, self.agent_slots - self.agents_deployed)


@dataclass
class AgentState:
    agent_id: str
    grade: int
    current_node: str
    asleep: bool
    name: str = ""
    last_moved_hours_ago: float = 1e9  # large => not recently moved


@dataclass
class Move:
    agent_id: str
    from_node: str
    to_node: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "from_node": self.from_node,
            "to_node": self.to_node,
            "reason": self.reason,
        }


@dataclass
class RebalancePlan:
    moves: list[Move] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "moves": [m.to_dict() for m in self.moves],
            "skipped": self.skipped,
            "summary": self.summary,
        }


def _is_pressured(n: NodeState, ram_headroom_gb: float, pressure_threshold: float) -> bool:
    """A node is pressured if it's low on RAM, over its SRE pressure
    threshold, or oversubscribed on slots."""
    return (
        n.ram_free_gb < ram_headroom_gb
        or n.pressure >= pressure_threshold
        or n.agents_deployed > n.agent_slots
    )


def _can_host(
    target: NodeState, agent: AgentState, ram_headroom_gb: float, est_agent_gb: float
) -> bool:
    """Can `target` take `agent` without breaching grade, slots, or RAM?"""
    if target.node_id == agent.current_node:
        return False
    if target.grade < agent.grade:  # Grade-1 never onto a Grade-0 node
        return False
    if target.free_slots() <= 0:  # capacity
        return False
    # leave headroom after the move (estimate the agent's marginal cost)
    if target.ram_free_gb - est_agent_gb < ram_headroom_gb:
        return False
    return True


def plan_rebalance(
    nodes: list[NodeState],
    agents: list[AgentState],
    *,
    ram_headroom_gb: float = 4.0,
    max_moves: int = 3,
    cooldown_hours: float = 6.0,
    pressure_threshold: float = 0.85,
    est_agent_gb: float = 1.0,
) -> RebalancePlan:
    """Produce an advisory rebalance plan from infra metrics + placement.

    Moves eligible (sleeping, off-cooldown) agents off pressured nodes onto
    grade-compatible nodes with headroom, most-pressured source first,
    most-headroom target first, up to ``max_moves``.
    """
    plan = RebalancePlan()

    # Working copies of mutable capacity so a plan stays self-consistent
    # across multiple moves in one pass.
    free_slots = {n.node_id: n.free_slots() for n in nodes}
    ram_free = {n.node_id: n.ram_free_gb for n in nodes}

    pressured = sorted(
        (n for n in nodes if _is_pressured(n, ram_headroom_gb, pressure_threshold)),
        key=lambda n: (-n.pressure, n.ram_free_gb),  # worst first
    )
    if not pressured:
        plan.summary = "All nodes within headroom — no moves needed."
        return plan

    for src in pressured:
        # candidate agents on this pressured node, most-movable first
        candidates = [a for a in agents if a.current_node == src.node_id]
        for agent in candidates:
            if len(plan.moves) >= max_moves:
                break
            if not agent.asleep:
                plan.skipped.append(
                    {"agent_id": agent.agent_id, "reason": "awake — only sleeping agents can move"}
                )
                continue
            if agent.last_moved_hours_ago < cooldown_hours:
                plan.skipped.append(
                    {"agent_id": agent.agent_id, "reason": f"in cooldown ({cooldown_hours}h)"}
                )
                continue
            # pick the best grade-compatible target with headroom
            targets = sorted(
                (
                    n
                    for n in nodes
                    if n.node_id != src.node_id
                    and n.grade >= agent.grade
                    and free_slots.get(n.node_id, 0) > 0
                    and ram_free.get(n.node_id, 0) - est_agent_gb >= ram_headroom_gb
                ),
                key=lambda n: -ram_free.get(n.node_id, 0),  # most headroom first
            )
            if not targets:
                plan.skipped.append(
                    {"agent_id": agent.agent_id, "reason": "no grade-compatible node with headroom"}
                )
                continue
            tgt = targets[0]
            plan.moves.append(
                Move(
                    agent_id=agent.agent_id,
                    from_node=src.node_id,
                    to_node=tgt.node_id,
                    reason=(
                        f"{src.name or src.node_id[:8]} pressured "
                        f"(ram_free={src.ram_free_gb:.0f}G, pressure={src.pressure:.2f}); "
                        f"{tgt.name or tgt.node_id[:8]} has headroom"
                    ),
                )
            )
            # update working capacity so the next move sees the new state
            free_slots[tgt.node_id] -= 1
            ram_free[tgt.node_id] -= est_agent_gb
            ram_free[src.node_id] += est_agent_gb
        if len(plan.moves) >= max_moves:
            break

    if plan.moves:
        plan.summary = f"{len(plan.moves)} move(s) proposed to relieve pressured node(s)."
    else:
        plan.summary = (
            "Node(s) pressured but no eligible agent to move (asleep + grade-fit + headroom)."
        )
    return plan
