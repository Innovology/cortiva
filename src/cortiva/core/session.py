"""
Agent session management — conversation continuity within a wake cycle.

Each agent gets a :class:`Session` when it wakes, which accumulates
conversation turns (plan, execute, replan, reflect) as a rolling
buffer.  This gives the consciousness adapter access to what the agent
has already thought about today, preventing the "amnesia" problem
where every LLM call is a fresh single-turn context.

Sessions are cleared when the agent sleeps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("cortiva.session")

# Maximum turns to retain in the rolling buffer.  Older turns are
# evicted first to keep total token count manageable.
DEFAULT_MAX_TURNS = 20

# Approximate character-to-token ratio (1 token ≈ 4 chars).
_CHARS_PER_TOKEN = 4


@dataclass
class Turn:
    """A single exchange in the session."""

    role: str
    """``'user'`` (the prompt sent to the LLM) or ``'assistant'``
    (the LLM response)."""

    content: str
    """The text content of the turn."""

    call_type: str = ""
    """What kind of call produced this turn: ``plan``, ``execute``,
    ``replan``, ``reflect``."""

    agent_id: str = ""
    """Which agent this turn belongs to (for validation)."""


@dataclass
class Session:
    """Rolling conversation buffer for a single agent's wake cycle.

    The session accumulates turns and can render them as a
    ``conversation_so_far`` string for injection into the LLM context.
    Older turns are evicted when the buffer exceeds ``max_turns``.
    """

    agent_id: str
    max_turns: int = DEFAULT_MAX_TURNS
    max_tokens: int = 8000
    turns: list[Turn] = field(default_factory=list)

    def add_turn(
        self,
        role: str,
        content: str,
        call_type: str = "",
    ) -> None:
        """Append a turn and evict old ones if over limit."""
        self.turns.append(Turn(
            role=role,
            content=content,
            call_type=call_type,
            agent_id=self.agent_id,
        ))
        self._evict()

    def _evict(self) -> None:
        """Remove oldest turns to stay within limits."""
        # Hard limit on number of turns
        while len(self.turns) > self.max_turns:
            self.turns.pop(0)

        # Soft limit on total token estimate
        total_chars = sum(len(t.content) for t in self.turns)
        while total_chars > self.max_tokens * _CHARS_PER_TOKEN and len(self.turns) > 2:
            removed = self.turns.pop(0)
            total_chars -= len(removed.content)

    def render(self) -> str:
        """Render the session as a context section for LLM injection.

        Returns an empty string if the session has no meaningful turns.
        """
        if len(self.turns) < 2:
            return ""

        lines = ["## Conversation Today\n"]
        for turn in self.turns:
            label = turn.call_type or turn.role
            # Truncate long turns to keep the summary scannable
            text = turn.content
            if len(text) > 500:
                text = text[:500] + "…"
            lines.append(f"**[{label}]** {text}\n")

        return "\n".join(lines)

    def clear(self) -> None:
        """Reset the session (called on sleep)."""
        self.turns.clear()


class SessionManager:
    """Manages per-agent sessions across the Fabric.

    The Fabric creates one manager and calls :meth:`start` on wake,
    :meth:`record` after each LLM call, and :meth:`end` on sleep.
    """

    def __init__(self, max_turns: int = DEFAULT_MAX_TURNS) -> None:
        self._sessions: dict[str, Session] = {}
        self._max_turns = max_turns

    def start(self, agent_id: str) -> Session:
        """Start a new session for an agent (on wake)."""
        session = Session(agent_id=agent_id, max_turns=self._max_turns)
        self._sessions[agent_id] = session
        logger.debug("Session started for %s", agent_id)
        return session

    def get(self, agent_id: str) -> Session | None:
        """Get the active session for an agent, or None."""
        return self._sessions.get(agent_id)

    def record(
        self,
        agent_id: str,
        prompt: str,
        response: str,
        call_type: str = "",
    ) -> None:
        """Record a prompt/response pair in the agent's session."""
        session = self._sessions.get(agent_id)
        if session is None:
            return
        session.add_turn("user", prompt, call_type=call_type)
        session.add_turn("assistant", response, call_type=call_type)

    def render(self, agent_id: str) -> str:
        """Render the session context for an agent.

        Returns empty string if no session exists.
        """
        session = self._sessions.get(agent_id)
        if session is None:
            return ""
        return session.render()

    def end(self, agent_id: str) -> None:
        """End and discard the session for an agent (on sleep)."""
        session = self._sessions.pop(agent_id, None)
        if session:
            session.clear()
            logger.debug("Session ended for %s", agent_id)

    def validate_agent(self, agent_id: str, context: str) -> None:
        """Verify that a context string belongs to the claimed agent.

        Raises :class:`ValueError` if the context appears to contain
        another agent's identity.  This is a safety check against
        accidental context cross-contamination in the Fabric.
        """
        session = self._sessions.get(agent_id)
        if session is None:
            return

        # Check all active sessions for other agents
        for other_id, other_session in self._sessions.items():
            if other_id == agent_id:
                continue
            # If another agent's ID appears prominently in this context
            # (in the identity header area, not just mentioned in a message),
            # flag it as suspicious
            identity_marker = f"# {other_id}"
            # Only check the first 500 chars (the identity header area)
            header = context[:500]
            if identity_marker in header:
                raise ValueError(
                    f"Context cross-contamination detected: context for "
                    f"{agent_id!r} contains identity header for {other_id!r}"
                )
