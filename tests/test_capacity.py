"""Tests for node capacity and contention tracking."""

from __future__ import annotations

from cortiva.core.capacity import CapacityTracker, HeartbeatTiming, TaskTiming


class TestTaskTiming:
    def test_queue_wait(self) -> None:
        t = TaskTiming(agent_id="a", task_id="t1", queued_at=100.0, started_at=105.0)
        assert t.queue_wait == 5.0

    def test_execution_time(self) -> None:
        t = TaskTiming(
            agent_id="a", task_id="t1",
            started_at=100.0, finished_at=130.0,
        )
        assert t.execution_time == 30.0

    def test_no_queue_time(self) -> None:
        t = TaskTiming(agent_id="a", task_id="t1")
        assert t.queue_wait == 0.0

    def test_to_dict(self) -> None:
        t = TaskTiming(
            agent_id="a", task_id="t1",
            queued_at=100.0, started_at=105.0, finished_at=120.0,
            consciousness_wait=8.0,
        )
        d = t.to_dict()
        assert d["queue_wait_s"] == 5.0
        assert d["execution_s"] == 15.0
        assert d["consciousness_wait_s"] == 8.0


class TestHeartbeatTiming:
    def test_total_time(self) -> None:
        h = HeartbeatTiming(started_at=100.0, finished_at=110.0)
        assert h.total_time == 10.0

    def test_idle_time(self) -> None:
        h = HeartbeatTiming(
            started_at=100.0, finished_at=110.0,
            agent_timings={"a1": 4.0, "a2": 3.0},
        )
        assert h.idle_time == 3.0

    def test_to_dict(self) -> None:
        h = HeartbeatTiming(
            started_at=100.0, finished_at=110.0,
            agent_timings={"a1": 5.0},
        )
        d = h.to_dict()
        assert d["total_s"] == 10.0
        assert d["agents"]["a1"] == 5.0


class TestCapacityTracker:
    def test_task_lifecycle(self) -> None:
        ct = CapacityTracker()
        ct.task_queued("agent-1", "task-1")
        ct.task_started("agent-1", "task-1")
        timing = ct.task_finished("agent-1", "task-1", consciousness_wait=2.0)
        assert timing is not None
        assert timing.consciousness_wait == 2.0
        assert timing.queue_wait >= 0
        assert timing.execution_time >= 0

    def test_task_started_without_queue(self) -> None:
        ct = CapacityTracker()
        ct.task_started("agent-1", "task-1")
        timing = ct.task_finished("agent-1", "task-1")
        assert timing is not None
        assert timing.queue_wait == 0.0

    def test_task_finished_unknown(self) -> None:
        ct = CapacityTracker()
        assert ct.task_finished("agent-1", "unknown") is None

    def test_heartbeat_lifecycle(self) -> None:
        ct = CapacityTracker()
        ct.heartbeat_start()
        start = ct.agent_cycle_start("agent-1")
        ct.agent_cycle_end("agent-1", start)
        hb = ct.heartbeat_end()
        assert hb is not None
        assert hb.total_time >= 0
        assert "agent-1" in hb.agent_timings

    def test_heartbeat_end_without_start(self) -> None:
        ct = CapacityTracker()
        assert ct.heartbeat_end() is None

    def test_snapshot(self) -> None:
        ct = CapacityTracker()

        # Simulate some activity
        ct.heartbeat_start()
        start = ct.agent_cycle_start("agent-1")
        ct.agent_cycle_end("agent-1", start)
        ct.heartbeat_end()

        ct.task_queued("agent-1", "task-1")
        ct.task_started("agent-1", "task-1")
        ct.task_finished("agent-1", "task-1")

        snap = ct.snapshot(active_agents=1, total_agents=3)
        assert "node" in snap
        assert snap["node"]["cpu_cores"] >= 1
        assert "agents" in snap
        assert snap["agents"]["active"] == 1
        assert snap["agents"]["total"] == 3
        assert "contention" in snap
        assert "agent_share_pct" in snap
        assert "recent_tasks" in snap

    def test_history_limit(self) -> None:
        ct = CapacityTracker(max_history=5)
        for i in range(10):
            ct.task_started("a", f"t{i}")
            ct.task_finished("a", f"t{i}")
        assert len(ct._task_timings) == 5

    def test_multiple_agents_share(self) -> None:
        ct = CapacityTracker()
        ct.heartbeat_start()
        s1 = ct.agent_cycle_start("agent-1")
        ct.agent_cycle_end("agent-1", s1)
        s2 = ct.agent_cycle_start("agent-2")
        ct.agent_cycle_end("agent-2", s2)
        ct.heartbeat_end()

        snap = ct.snapshot(active_agents=2, total_agents=2)
        share = snap["agent_share_pct"]
        assert "agent-1" in share
        assert "agent-2" in share
        # Both should have some share
        assert share["agent-1"] + share["agent-2"] == pytest.approx(100.0, abs=1.0)


import pytest  # noqa: E402 — needed for approx
