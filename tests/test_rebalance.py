"""AR Scheduler node rebalancer — advisory planner invariants."""

from cortiva.scheduling.rebalance import (
    AgentState,
    NodeState,
    plan_rebalance,
)


def _nodes(m2_free=2.0, m1_free=12.0):
    # Mini-2 = grade 1 (64GB), Mini-1 = grade 0 (32GB)
    return [
        NodeState("m2", grade=1, ram_free_gb=m2_free, ram_total_gb=64,
                  agents_deployed=9, agent_slots=16, name="Mini-2", pressure=0.9),
        NodeState("m1", grade=0, ram_free_gb=m1_free, ram_total_gb=32,
                  agents_deployed=0, agent_slots=6, name="Mini-1", pressure=0.1),
    ]


def test_moves_sleeping_grade0_off_pressured_node():
    agents = [
        AgentState("dev", grade=0, current_node="m2", asleep=True),
        AgentState("ceo", grade=1, current_node="m2", asleep=True),
    ]
    plan = plan_rebalance(_nodes(), agents)
    moved = {m.agent_id for m in plan.moves}
    assert "dev" in moved            # grade-0, sleeping → moves to Mini-1
    assert "ceo" not in moved        # grade-1 can't go to grade-0 Mini-1
    m = next(m for m in plan.moves if m.agent_id == "dev")
    assert m.from_node == "m2" and m.to_node == "m1"


def test_awake_agent_is_never_moved():
    agents = [AgentState("dev", grade=0, current_node="m2", asleep=False)]
    plan = plan_rebalance(_nodes(), agents)
    assert plan.moves == []
    assert any("awake" in s["reason"] for s in plan.skipped)


def test_grade1_never_lands_on_grade0_node():
    agents = [AgentState("ceo", grade=1, current_node="m2", asleep=True)]
    plan = plan_rebalance(_nodes(), agents)
    assert plan.moves == []
    assert any("grade-compatible" in s["reason"] for s in plan.skipped)


def test_no_moves_when_no_node_pressured():
    nodes = _nodes(m2_free=30.0)  # Mini-2 not pressured + low pressure
    nodes[0].pressure = 0.2
    agents = [AgentState("dev", grade=0, current_node="m2", asleep=True)]
    plan = plan_rebalance(nodes, agents)
    assert plan.moves == []
    assert "no moves needed" in plan.summary.lower()


def test_respects_target_capacity():
    nodes = _nodes()
    nodes[1].agents_deployed = 6  # Mini-1 full (0 free slots)
    agents = [AgentState("dev", grade=0, current_node="m2", asleep=True)]
    plan = plan_rebalance(nodes, agents)
    assert plan.moves == []
    assert any("headroom" in s["reason"] for s in plan.skipped)


def test_cooldown_blocks_recent_move():
    agents = [AgentState("dev", grade=0, current_node="m2", asleep=True,
                         last_moved_hours_ago=1.0)]
    plan = plan_rebalance(_nodes(), agents, cooldown_hours=6.0)
    assert plan.moves == []
    assert any("cooldown" in s["reason"] for s in plan.skipped)


def test_max_moves_cap():
    agents = [AgentState(f"d{i}", grade=0, current_node="m2", asleep=True)
              for i in range(5)]
    plan = plan_rebalance(_nodes(m1_free=40.0), agents, max_moves=2)
    assert len(plan.moves) == 2


def test_target_ram_headroom_not_breached():
    # Mini-1 has only 4G free; headroom 4G + agent 1G can't fit
    nodes = _nodes(m1_free=4.0)
    agents = [AgentState("dev", grade=0, current_node="m2", asleep=True)]
    plan = plan_rebalance(nodes, agents, ram_headroom_gb=4.0, est_agent_gb=1.0)
    assert plan.moves == []


def test_fabric_maps_hq_snapshot_to_plan():
    """The fabric handler maps an HQ cluster-metrics snapshot onto the
    planner dataclasses and produces the same advisory plan."""
    from cortiva.core.fabric import Fabric

    snapshot = {
        "nodes": [
            {"node_id": "m2", "grade": 1, "ram_free_gb": 2.0, "ram_total_gb": 64,
             "agents_deployed": 9, "agent_slots": 16, "name": "Mini-2",
             "pressure": 0.9},
            {"node_id": "m1", "grade": 0, "ram_free_gb": 12.0, "ram_total_gb": 32,
             "agents_deployed": 0, "agent_slots": 6, "name": "Mini-1",
             "pressure": 0.1},
        ],
        "agents": [
            {"agent_id": "dev", "grade": 0, "current_node": "m2", "asleep": True},
            {"agent_id": "ceo", "grade": 1, "current_node": "m2", "asleep": True},
        ],
    }
    nodes, agents = Fabric._build_rebalance_inputs(None, snapshot)
    plan = plan_rebalance(nodes, agents)
    moved = {m.agent_id for m in plan.moves}
    assert moved == {"dev"}          # grade-0 sleeping → Mini-1; grade-1 stays
    assert plan.moves[0].from_node == "m2" and plan.moves[0].to_node == "m1"
