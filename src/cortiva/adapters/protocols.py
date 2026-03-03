"""
Cortiva adapter protocols.

Every external dependency in Cortiva is behind a Protocol interface.
Swap memory systems, LLM providers, or communication channels
without changing your agents or the framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Common data types
# ---------------------------------------------------------------------------

class Priority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class MemoryRecord:
    """A single unit of agent memory."""
    id: str
    content: str
    agent_id: str
    tags: list[str] = field(default_factory=list)
    importance: float = 5.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FamiliaritySignal:
    """The agent's gut feeling about a task, computed from memory."""
    strength: str          # "novel" | "vague_recognition" | "familiar" | "routine"
    valence: str           # "positive" | "neutral" | "cautious" | "negative"
    match_count: int       # how many similar experiences found
    text: str              # natural language description for context injection
    retrieved: list[MemoryRecord] = field(default_factory=list)


@dataclass
class ConsciousResponse:
    """Response from a conscious (LLM) invocation."""
    content: str
    reflection: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """A message between agents or between agent and human."""
    id: str
    sender: str            # agent_id or "human"
    recipient: str         # agent_id, channel name, or "human"
    content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Memory adapter protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class MemoryAdapter(Protocol):
    """
    Interface for persistent agent memory.

    Implementations: Engram, Mem0, Letta, Neo4j, SQLite, etc.
    """

    async def store(
        self,
        agent_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 5.0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        """Store a memory for an agent."""
        ...

    async def search(
        self,
        agent_id: str,
        query: str,
        *,
        limit: int = 10,
        min_importance: float = 0.0,
        tags: list[str] | None = None,
    ) -> list[MemoryRecord]:
        """Search agent memories by semantic or keyword match."""
        ...

    async def recall(
        self,
        agent_id: str,
        *,
        limit: int = 20,
        min_importance: float = 0.0,
    ) -> list[MemoryRecord]:
        """Recall the most important memories for an agent."""
        ...

    async def delete(self, agent_id: str, memory_id: str) -> bool:
        """Delete a specific memory."""
        ...


# ---------------------------------------------------------------------------
# Consciousness adapter protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class ConsciousnessAdapter(Protocol):
    """
    Interface for the conscious layer — the expensive LLM that thinks.

    Implementations: Anthropic Claude, OpenAI, local large model, etc.
    """

    async def think(
        self,
        agent_id: str,
        context: str,
        prompt: str,
        *,
        priority: Priority = Priority.NORMAL,
        max_tokens: int = 4096,
        metadata: dict[str, Any] | None = None,
    ) -> ConsciousResponse:
        """
        Invoke a moment of conscious thought.

        The context contains the agent's identity, state, and relevant
        memories assembled by the subconscious layer. The prompt is
        the specific question or task requiring thought.
        """
        ...

    async def reflect(
        self,
        agent_id: str,
        context: str,
        day_summary: str,
    ) -> ConsciousResponse:
        """
        End-of-day reflection. The conscious layer reviews the day,
        updates the Living Summary, and notes learnings.
        """
        ...


# ---------------------------------------------------------------------------
# Routine adapter protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class RoutineAdapter(Protocol):
    """
    Interface for the subconscious layer — the cheap local model
    that monitors, computes, and routes.

    Implementations: Qwen via Ollama, llama.cpp, vLLM, etc.
    """

    async def assess(
        self,
        agent_id: str,
        task_description: str,
        procedural_index: str,
        familiarity: FamiliaritySignal,
    ) -> dict[str, Any]:
        """
        Assess whether a task can be handled procedurally or needs
        escalation to the conscious layer.

        Returns a dict with:
          - "action": "procedural" | "escalate" | "defer"
          - "procedure_match": str | None
          - "confidence": float
          - "context_for_conscious": str | None
        """
        ...

    async def compile_context(
        self,
        agent_id: str,
        identity: str,
        memories: list[MemoryRecord],
        familiarity: FamiliaritySignal,
        task: str,
        additional: dict[str, str] | None = None,
    ) -> str:
        """
        Assemble the context package that the conscious layer will read.
        Combines identity, relevant memories, familiarity signal, and
        current task into a coherent prompt.
        """
        ...


# ---------------------------------------------------------------------------
# Channel adapter protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class ChannelAdapter(Protocol):
    """
    Interface for agent communication channels.

    Implementations: Slack, Discord, custom message bus, etc.
    """

    async def send(
        self,
        sender: str,
        recipient: str,
        content: str,
        *,
        channel: str | None = None,
        thread_id: str | None = None,
    ) -> Message:
        """Send a message from one agent to another or to a channel."""
        ...

    async def receive(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[Message]:
        """Check for messages addressed to this agent."""
        ...

    async def listen(
        self,
        agent_id: str,
        channels: list[str],
    ) -> None:
        """
        Subscribe to channels. Messages will be queued for the agent
        and available via receive().
        """
        ...
