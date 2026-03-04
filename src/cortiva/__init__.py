"""
Cortiva — The organisational fabric for autonomous agent teams.
"""

__version__ = "0.1.0-dev"

from cortiva.adapters.protocols import (
    AgentResponse,
    ChannelAdapter,
    ConsciousnessAdapter,
    ConsciousResponse,
    FamiliaritySignal,
    MemoryAdapter,
    MemoryRecord,
    Message,
    Priority,
    RoutineAdapter,
    TerminalAgentAdapter,
    ToolCapabilities,
)
from cortiva.core.agent import Agent, AgentState, Task, TaskQueue
from cortiva.core.budget import (
    AgentBudgetStatus,
    BackendType,
    BudgetApproval,
    ConsciousnessBudgetManager,
)
from cortiva.core.fabric import Fabric
from cortiva.core.reflection import ReflectionResult, ReflectionSuffix, parse_reflection_suffix

__all__ = [
    "Agent",
    "AgentState",
    "AgentBudgetStatus",
    "BackendType",
    "BudgetApproval",
    "ConsciousnessBudgetManager",
    "Task",
    "TaskQueue",
    "AgentResponse",
    "Fabric",
    "MemoryAdapter",
    "ConsciousnessAdapter",
    "RoutineAdapter",
    "ChannelAdapter",
    "TerminalAgentAdapter",
    "MemoryRecord",
    "FamiliaritySignal",
    "ConsciousResponse",
    "Message",
    "Priority",
    "ToolCapabilities",
    "ReflectionSuffix",
    "ReflectionResult",
    "parse_reflection_suffix",
]
