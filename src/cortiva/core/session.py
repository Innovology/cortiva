"""
Session management for Cortiva agents.

A session represents a single wake cycle's conversation history. Turns
accumulate as the agent plans, executes, and reflects. A rolling buffer
with configurable limits prevents unbounded context growth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Turn:
    """A single turn in the session conversation."""

    role: str  # "agent", "system", or "user"
    phase: str  # "plan", "execute", "reflect", "replan"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())
    token_estimate: int = 0

    def __post_init__(self) -> None:
        if self.token_estimate == 0:
            self.token_estimate = len(self.content) // 4


@dataclass
class Session:
    """Conversation session for a single agent wake cycle.

    Maintains a rolling buffer of turns with eviction by both max turns
    and max tokens. Older turns are dropped first when limits are reached.

    Each session is bound to a specific agent to prevent context
    cross-contamination between agents sharing a fabric.
    """

    agent_id: str
    max_turns: int = 50
    max_tokens: int = 32_000
    turns: list[Turn] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())
    ended_at: str | None = None

    def add_turn(self, role: str, phase: str, content: str) -> Turn:
        """Append a turn and evict old turns if limits are exceeded."""
        turn = Turn(role=role, phase=phase, content=content)
        self.turns.append(turn)
        self._evict()
        return turn

    def _evict(self) -> None:
        """Remove oldest turns until both limits are satisfied."""
        # Evict by turn count
        while len(self.turns) > self.max_turns:
            self.turns.pop(0)

        # Evict by token budget
        while self.total_tokens > self.max_tokens and len(self.turns) > 1:
            self.turns.pop(0)

    @property
    def total_tokens(self) -> int:
        """Sum of estimated tokens across all turns."""
        return sum(t.token_estimate for t in self.turns)

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def validate_agent(self, agent_id: str) -> None:
        """Guard against cross-contamination. Raises ValueError if agent_id mismatches."""
        if agent_id != self.agent_id:
            raise ValueError(
                f"Session belongs to agent '{self.agent_id}', "
                f"not '{agent_id}'. Context cross-contamination prevented."
            )

    def to_context_string(self) -> str:
        """Render the session as a text block suitable for LLM context injection."""
        if not self.turns:
            return ""
        lines: list[str] = ["## Conversation History", ""]
        for turn in self.turns:
            prefix = f"[{turn.role}/{turn.phase}]"
            lines.append(f"{prefix} {turn.content}")
            lines.append("")
        return "\n".join(lines)

    def end(self) -> None:
        """Mark the session as ended."""
        self.ended_at = datetime.now(tz=UTC).isoformat()


class SessionManager:
    """Manages sessions for all agents in a fabric.

    At most one active session per agent. Sessions are started on wake
    and ended on sleep.
    """

    def __init__(
        self,
        default_max_turns: int = 50,
        default_max_tokens: int = 32_000,
    ) -> None:
        self.default_max_turns = default_max_turns
        self.default_max_tokens = default_max_tokens
        self._sessions: dict[str, Session] = {}

    def start_session(self, agent_id: str) -> Session:
        """Start a new session for an agent, replacing any existing one."""
        session = Session(
            agent_id=agent_id,
            max_turns=self.default_max_turns,
            max_tokens=self.default_max_tokens,
        )
        self._sessions[agent_id] = session
        return session

    def get_session(self, agent_id: str) -> Session | None:
        """Return the active session for an agent, or None."""
        return self._sessions.get(agent_id)

    def end_session(self, agent_id: str) -> Session | None:
        """End and remove the active session for an agent."""
        session = self._sessions.pop(agent_id, None)
        if session is not None:
            session.end()
        return session

    def add_turn(
        self, agent_id: str, role: str, phase: str, content: str
    ) -> Turn | None:
        """Add a turn to an agent's active session.

        Returns the Turn if a session exists, None otherwise.
        """
        session = self._sessions.get(agent_id)
        if session is None:
            return None
        return session.add_turn(role, phase, content)

    @property
    def active_sessions(self) -> list[str]:
        """List agent IDs with active sessions."""
        return list(self._sessions.keys())
