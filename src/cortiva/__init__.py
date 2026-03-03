"""
Cortiva — The organisational fabric for autonomous agent teams.
"""

__version__ = "0.1.0-dev"

from cortiva.adapters.protocols import (
    ChannelAdapter,
    ConsciousnessAdapter,
    ConsciousResponse,
    FamiliaritySignal,
    MemoryAdapter,
    MemoryRecord,
    Message,
    Priority,
    RoutineAdapter,
)
from cortiva.core.agent import Agent, AgentState
from cortiva.core.fabric import Fabric

__all__ = [
    "Agent",
    "AgentState",
    "Fabric",
    "MemoryAdapter",
    "ConsciousnessAdapter",
    "RoutineAdapter",
    "ChannelAdapter",
    "MemoryRecord",
    "FamiliaritySignal",
    "ConsciousResponse",
    "Message",
    "Priority",
]
