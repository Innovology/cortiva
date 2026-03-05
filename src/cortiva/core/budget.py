"""
Consciousness Budget Manager.

Tracks per-agent consciousness usage by backend type, enforces priorities,
walks fallback chains, and degrades gracefully when budgets are exhausted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BackendType(Enum):
    """Types of consciousness backend."""
    TERMINAL = "terminal"
    API = "api"
    LOCAL = "local"


@dataclass
class BackendBudget:
    """Per-backend budget tracker."""
    backend: BackendType
    calls_used: int = 0
    calls_limit: int = 0
    tokens_used: int = 0
    tokens_limit: int = 0
    window_seconds: int = 0
    window_start: float = field(default_factory=time.monotonic)

    @property
    def calls_remaining(self) -> int:
        if self.calls_limit == 0:
            return 0
        return max(0, self.calls_limit - self.calls_used)

    @property
    def is_exhausted(self) -> bool:
        if self.calls_limit > 0 and self.calls_used >= self.calls_limit:
            return True
        if self.tokens_limit > 0 and self.tokens_used >= self.tokens_limit:
            return True
        return False

    def check_window_reset(self) -> bool:
        """Reset counters if the tumbling window has elapsed. Returns True if reset."""
        if self.window_seconds <= 0:
            return False
        now = time.monotonic()
        if now - self.window_start >= self.window_seconds:
            self.calls_used = 0
            self.tokens_used = 0
            self.window_start = now
            return True
        return False

    def record_usage(self, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.calls_used += 1
        self.tokens_used += tokens_in + tokens_out


@dataclass
class BudgetApproval:
    """Result of a budget request."""
    approved: bool
    backend: BackendType | None = None
    fallback_used: bool = False
    reason: str = ""


@dataclass
class AgentBudgetStatus:
    """Snapshot of an agent's budget state for CLI display."""
    agent_id: str
    backends: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_calls: int = 0
    total_tokens: int = 0
    task_attempts: int = 0
    consciousness_calls: int = 0
    escalation_ratio: float = 0.0
    priority_counts: dict[str, int] = field(default_factory=dict)
    exhausted: bool = False


class ConsciousnessBudgetManager:
    """Manages consciousness budgets across agents and backend types."""

    def __init__(
        self,
        default_backend: BackendType = BackendType.API,
        fallback_chain: list[BackendType] | None = None,
        backend_configs: dict[BackendType, dict[str, Any]] | None = None,
    ):
        self.default_backend = default_backend
        self.fallback_chain = fallback_chain or [default_backend]
        self.backend_configs = backend_configs or {}
        self._agents: dict[str, dict[BackendType, BackendBudget]] = {}
        self._task_attempts: dict[str, int] = {}
        self._consciousness_calls: dict[str, int] = {}
        self._priority_counts: dict[str, dict[str, int]] = {}

    def register_agent(self, agent_id: str) -> None:
        """Create per-backend budgets for an agent from config."""
        budgets: dict[BackendType, BackendBudget] = {}
        for backend_type in self.fallback_chain:
            cfg = self.backend_configs.get(backend_type, {})
            budgets[backend_type] = BackendBudget(
                backend=backend_type,
                calls_limit=cfg.get("calls_limit", 0),
                tokens_limit=cfg.get("tokens_limit", 0),
                window_seconds=cfg.get("window_seconds", 0),
            )
        self._agents[agent_id] = budgets
        self._task_attempts[agent_id] = 0
        self._consciousness_calls[agent_id] = 0
        self._priority_counts[agent_id] = {}

    def reset_agent(self, agent_id: str) -> None:
        """Reset counters for an agent (called on wake)."""
        if agent_id not in self._agents:
            return
        for budget in self._agents[agent_id].values():
            budget.calls_used = 0
            budget.tokens_used = 0
            budget.window_start = time.monotonic()
        self._task_attempts[agent_id] = 0
        self._consciousness_calls[agent_id] = 0
        self._priority_counts[agent_id] = {}

    def request_budget(
        self,
        agent_id: str,
        priority: str = "normal",
    ) -> BudgetApproval:
        """Walk the fallback chain and find an available backend.

        CRITICAL priority is always approved if any capacity remains.
        """
        if agent_id not in self._agents:
            return BudgetApproval(approved=False, reason="Agent not registered")

        # Track priority
        counts = self._priority_counts[agent_id]
        counts[priority] = counts.get(priority, 0) + 1

        is_critical = priority == "critical"
        budgets = self._agents[agent_id]

        for i, backend_type in enumerate(self.fallback_chain):
            budget = budgets.get(backend_type)
            if budget is None:
                continue

            budget.check_window_reset()

            if not budget.is_exhausted:
                self._consciousness_calls[agent_id] = (
                    self._consciousness_calls.get(agent_id, 0) + 1
                )
                return BudgetApproval(
                    approved=True,
                    backend=backend_type,
                    fallback_used=i > 0,
                )

        # All backends exhausted — CRITICAL still gets through if any has calls_limit
        if is_critical:
            for backend_type in self.fallback_chain:
                budget = budgets.get(backend_type)
                if budget is not None and budget.calls_limit > 0:
                    self._consciousness_calls[agent_id] = (
                        self._consciousness_calls.get(agent_id, 0) + 1
                    )
                    return BudgetApproval(
                        approved=True,
                        backend=backend_type,
                        fallback_used=True,
                        reason="Critical override: budget exhausted",
                    )

        return BudgetApproval(
            approved=False,
            reason="All backends exhausted",
        )

    def record_usage(
        self,
        agent_id: str,
        backend: BackendType,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Track actual usage after a consciousness call."""
        if agent_id not in self._agents:
            return
        budget = self._agents[agent_id].get(backend)
        if budget is not None:
            budget.record_usage(tokens_in, tokens_out)

    def record_task_attempt(self, agent_id: str) -> None:
        """Record a task execution attempt for escalation ratio."""
        self._task_attempts[agent_id] = (
            self._task_attempts.get(agent_id, 0) + 1
        )

    def escalation_ratio(self, agent_id: str) -> float:
        """Consciousness calls / task attempts."""
        attempts = self._task_attempts.get(agent_id, 0)
        if attempts == 0:
            return 0.0
        calls = self._consciousness_calls.get(agent_id, 0)
        return calls / attempts

    def agent_status(self, agent_id: str) -> AgentBudgetStatus:
        """Snapshot of an agent's budget state."""
        if agent_id not in self._agents:
            return AgentBudgetStatus(agent_id=agent_id)

        budgets = self._agents[agent_id]
        backends: dict[str, dict[str, Any]] = {}
        total_calls = 0
        total_tokens = 0
        exhausted = True

        for backend_type, budget in budgets.items():
            budget.check_window_reset()
            backends[backend_type.value] = {
                "calls_used": budget.calls_used,
                "calls_limit": budget.calls_limit,
                "calls_remaining": budget.calls_remaining,
                "tokens_used": budget.tokens_used,
                "tokens_limit": budget.tokens_limit,
                "is_exhausted": budget.is_exhausted,
            }
            total_calls += budget.calls_used
            total_tokens += budget.tokens_used
            if not budget.is_exhausted:
                exhausted = False

        return AgentBudgetStatus(
            agent_id=agent_id,
            backends=backends,
            total_calls=total_calls,
            total_tokens=total_tokens,
            task_attempts=self._task_attempts.get(agent_id, 0),
            consciousness_calls=self._consciousness_calls.get(agent_id, 0),
            escalation_ratio=self.escalation_ratio(agent_id),
            priority_counts=dict(self._priority_counts.get(agent_id, {})),
            exhausted=exhausted,
        )

    def all_status(self) -> dict[str, AgentBudgetStatus]:
        """Snapshot of all agents' budget state."""
        return {
            agent_id: self.agent_status(agent_id)
            for agent_id in self._agents
        }
