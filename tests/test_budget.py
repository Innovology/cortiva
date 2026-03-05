"""Tests for the consciousness budget manager."""

import time
from pathlib import Path

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core.agent import AgentState
from cortiva.core.budget import (
    BackendBudget,
    BackendType,
    ConsciousnessBudgetManager,
)
from cortiva.core.config import _build_budget_manager
from cortiva.core.fabric import Fabric

# ---------------------------------------------------------------------------
# BackendBudget tests
# ---------------------------------------------------------------------------


class TestBackendBudget:
    def test_calls_remaining(self) -> None:
        b = BackendBudget(backend=BackendType.API, calls_limit=10, calls_used=3)
        assert b.calls_remaining == 7

    def test_is_exhausted_by_calls(self) -> None:
        b = BackendBudget(backend=BackendType.API, calls_limit=5, calls_used=5)
        assert b.is_exhausted is True

    def test_is_exhausted_by_tokens(self) -> None:
        b = BackendBudget(
            backend=BackendType.API, calls_limit=100, tokens_limit=1000, tokens_used=1000,
        )
        assert b.is_exhausted is True

    def test_not_exhausted(self) -> None:
        b = BackendBudget(backend=BackendType.API, calls_limit=10, calls_used=3)
        assert b.is_exhausted is False

    def test_window_reset(self) -> None:
        b = BackendBudget(
            backend=BackendType.TERMINAL,
            calls_limit=100,
            calls_used=50,
            tokens_used=500,
            window_seconds=1,
            window_start=time.monotonic() - 2,  # 2 seconds ago
        )
        assert b.check_window_reset() is True
        assert b.calls_used == 0
        assert b.tokens_used == 0

    def test_window_no_reset_within_window(self) -> None:
        b = BackendBudget(
            backend=BackendType.TERMINAL,
            calls_limit=100,
            calls_used=50,
            window_seconds=3600,
        )
        assert b.check_window_reset() is False
        assert b.calls_used == 50

    def test_record_usage(self) -> None:
        b = BackendBudget(backend=BackendType.API, calls_limit=10)
        b.record_usage(tokens_in=100, tokens_out=50)
        assert b.calls_used == 1
        assert b.tokens_used == 150


# ---------------------------------------------------------------------------
# ConsciousnessBudgetManager tests
# ---------------------------------------------------------------------------


class TestBudgetManager:
    def _make_manager(self) -> ConsciousnessBudgetManager:
        return ConsciousnessBudgetManager(
            default_backend=BackendType.API,
            fallback_chain=[BackendType.API, BackendType.LOCAL],
            backend_configs={
                BackendType.API: {"calls_limit": 5, "tokens_limit": 10000},
                BackendType.LOCAL: {"calls_limit": 100},
            },
        )

    def test_register_agent(self) -> None:
        mgr = self._make_manager()
        mgr.register_agent("agent-01")
        status = mgr.agent_status("agent-01")
        assert "api" in status.backends
        assert "local" in status.backends
        assert status.backends["api"]["calls_limit"] == 5

    def test_request_approved(self) -> None:
        mgr = self._make_manager()
        mgr.register_agent("agent-01")
        result = mgr.request_budget("agent-01", "normal")
        assert result.approved is True
        assert result.backend == BackendType.API
        assert result.fallback_used is False

    def test_request_denied_all_exhausted(self) -> None:
        mgr = ConsciousnessBudgetManager(
            default_backend=BackendType.API,
            fallback_chain=[BackendType.API],
            backend_configs={BackendType.API: {"calls_limit": 1}},
        )
        mgr.register_agent("agent-01")
        # Use up the single call
        mgr.request_budget("agent-01", "normal")
        mgr.record_usage("agent-01", BackendType.API, 10, 10)
        # Next request should be denied
        result = mgr.request_budget("agent-01", "normal")
        assert result.approved is False
        assert "exhausted" in result.reason.lower()

    def test_fallback_chain(self) -> None:
        mgr = self._make_manager()
        mgr.register_agent("agent-01")
        # Exhaust API backend
        for _ in range(5):
            mgr.request_budget("agent-01", "normal")
            mgr.record_usage("agent-01", BackendType.API, 10, 10)
        # Next request should fallback to LOCAL
        result = mgr.request_budget("agent-01", "normal")
        assert result.approved is True
        assert result.backend == BackendType.LOCAL
        assert result.fallback_used is True

    def test_critical_priority_override(self) -> None:
        mgr = ConsciousnessBudgetManager(
            default_backend=BackendType.API,
            fallback_chain=[BackendType.API],
            backend_configs={BackendType.API: {"calls_limit": 1}},
        )
        mgr.register_agent("agent-01")
        # Exhaust budget
        mgr.request_budget("agent-01", "normal")
        mgr.record_usage("agent-01", BackendType.API, 10, 10)
        # Normal should be denied
        assert mgr.request_budget("agent-01", "normal").approved is False
        # Critical should still be approved
        result = mgr.request_budget("agent-01", "critical")
        assert result.approved is True

    def test_priority_tracking(self) -> None:
        mgr = self._make_manager()
        mgr.register_agent("agent-01")
        mgr.request_budget("agent-01", "normal")
        mgr.request_budget("agent-01", "high")
        mgr.request_budget("agent-01", "critical")
        mgr.request_budget("agent-01", "normal")
        status = mgr.agent_status("agent-01")
        assert status.priority_counts == {"normal": 2, "high": 1, "critical": 1}

    def test_escalation_ratio(self) -> None:
        mgr = self._make_manager()
        mgr.register_agent("agent-01")
        # 3 task attempts, 2 consciousness calls
        mgr.record_task_attempt("agent-01")
        mgr.record_task_attempt("agent-01")
        mgr.record_task_attempt("agent-01")
        mgr.request_budget("agent-01", "normal")
        mgr.request_budget("agent-01", "normal")
        assert mgr.escalation_ratio("agent-01") == pytest.approx(2.0 / 3.0)

    def test_reset_agent(self) -> None:
        mgr = self._make_manager()
        mgr.register_agent("agent-01")
        mgr.request_budget("agent-01", "normal")
        mgr.record_usage("agent-01", BackendType.API, 100, 50)
        mgr.record_task_attempt("agent-01")

        mgr.reset_agent("agent-01")
        status = mgr.agent_status("agent-01")
        assert status.total_calls == 0
        assert status.total_tokens == 0
        assert status.task_attempts == 0
        assert status.consciousness_calls == 0

    def test_unregistered_agent(self) -> None:
        mgr = self._make_manager()
        result = mgr.request_budget("unknown", "normal")
        assert result.approved is False
        assert "not registered" in result.reason.lower()


# ---------------------------------------------------------------------------
# Fabric + budget manager integration tests
# ---------------------------------------------------------------------------


class MockConsciousness:
    """Mock consciousness adapter."""

    async def think(self, agent_id, context, prompt, **kwargs):
        if "plan" in prompt.lower() or "checklist" in prompt.lower():
            return ConsciousResponse(
                content=(
                    "# Today's Plan\n\n"
                    "- [ ] **[CRITICAL]** Review urgent tickets\n"
                    "- [ ] **[HIGH]** Process incoming messages\n"
                    "- [ ] Update weekly report\n"
                ),
                tokens_in=100,
                tokens_out=50,
                model="mock",
            )
        return ConsciousResponse(
            content=f"[{agent_id}] Completed task successfully.",
            tokens_in=100,
            tokens_out=50,
            model="mock",
        )

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(
            content=f"# {agent_id}\n\nCompleted a productive day.",
            reflection="Today went well.",
            tokens_in=200,
            tokens_out=100,
            model="mock",
        )


class TestFabricBudgetIntegration:
    def _make_fabric_with_budget(
        self, tmp_path: Path, calls_limit: int = 50
    ) -> Fabric:
        mgr = ConsciousnessBudgetManager(
            default_backend=BackendType.API,
            fallback_chain=[BackendType.API],
            backend_configs={BackendType.API: {"calls_limit": calls_limit}},
        )
        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=MockConsciousness(),
            budget_manager=mgr,
        )
        return fabric

    @pytest.mark.asyncio
    async def test_fabric_with_budget_manager_works(self, tmp_path: Path) -> None:
        fabric = self._make_fabric_with_budget(tmp_path)
        fabric.register_agent("worker-01")
        agent = await fabric.wake("worker-01")
        assert agent.state == AgentState.EXECUTING
        assert agent.task_queue is not None

        result = await fabric.cycle("worker-01")
        assert result["action"] == "executed_task"

    @pytest.mark.asyncio
    async def test_budget_exhaustion_defers_task(self, tmp_path: Path) -> None:
        # Budget of 2: 1 for wake planning, 1 for first task
        fabric = self._make_fabric_with_budget(tmp_path, calls_limit=2)
        fabric.register_agent("worker-01")
        agent = await fabric.wake("worker-01")  # uses 1

        result1 = await fabric.cycle("worker-01")  # uses 1
        assert result1["action"] == "executed_task"

        result2 = await fabric.cycle("worker-01")  # budget exhausted
        assert result2["action"] == "executed_task"
        assert agent.task_queue is not None
        assert len(agent.task_queue.exceptions) > 0
        assert agent.task_queue.exceptions[0].error == "Budget exhausted"

    @pytest.mark.asyncio
    async def test_backward_compat_without_manager(self, tmp_path: Path) -> None:
        """Fabric without budget_manager uses legacy spend_consciousness path."""
        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=MockConsciousness(),
        )
        fabric.register_agent("legacy-01", consciousness_budget=50)
        agent = await fabric.wake("legacy-01")
        assert agent.state == AgentState.EXECUTING

        result = await fabric.cycle("legacy-01")
        assert result["action"] == "executed_task"

    @pytest.mark.asyncio
    async def test_status_includes_budget(self, tmp_path: Path) -> None:
        fabric = self._make_fabric_with_budget(tmp_path)
        fabric.register_agent("worker-01")
        await fabric.wake("worker-01")

        status = fabric.status()
        assert "budget" in status
        assert "worker-01" in status["budget"]
        assert "total_calls" in status["budget"]["worker-01"]


# ---------------------------------------------------------------------------
# Config budget tests
# ---------------------------------------------------------------------------


class TestConfigBudget:
    def test_config_creates_budget_manager(self, tmp_path: Path) -> None:
        config = {
            "consciousness": {
                "provider": "anthropic",
                "budget": {
                    "daily_limit": 500,
                    "backend_type": "api",
                    "fallback_chain": ["api", "local"],
                    "api": {"calls_limit": 500, "tokens_limit": 1000000},
                    "local": {"calls_limit": 1000},
                },
            },
        }
        mgr = _build_budget_manager(config)
        assert mgr is not None
        assert mgr.default_backend == BackendType.API
        assert mgr.fallback_chain == [BackendType.API, BackendType.LOCAL]

    def test_legacy_config_backward_compat(self, tmp_path: Path) -> None:
        config = {
            "consciousness": {
                "provider": "anthropic",
                "budget": {"daily_limit": 1000},
            },
        }
        mgr = _build_budget_manager(config)
        assert mgr is not None
        assert mgr.default_backend == BackendType.API
        # Should create API backend with calls_limit = daily_limit
        mgr.register_agent("test")
        status = mgr.agent_status("test")
        assert status.backends["api"]["calls_limit"] == 1000

    def test_no_budget_section_returns_none(self) -> None:
        config = {"consciousness": {"provider": "anthropic"}}
        mgr = _build_budget_manager(config)
        assert mgr is None
