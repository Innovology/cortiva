"""
Direct agent conversation — talk to an agent interactively.

Opens a REPL where the human's messages are sent to the agent's
consciousness adapter with the agent's full identity context loaded.
The agent responds as itself, with access to its memories, procedures,
and today's session history.

This is the equivalent of walking up to an employee's desk and having
a conversation.  The agent knows who it is, what it's working on, and
can answer questions or accept instructions.
"""

from __future__ import annotations

import logging
from typing import Any

from cortiva.adapters.protocols import ConsciousnessAdapter, MemoryAdapter, Priority
from cortiva.core.agent import Agent
from cortiva.core.context import ContextBuilder
from cortiva.core.session import SessionManager

logger = logging.getLogger("cortiva.chat")


class AgentChat:
    """Interactive conversation with an agent.

    Loads the agent's identity and memories, then sends human messages
    through the consciousness adapter with full context.

    Parameters
    ----------
    agent:
        The agent to chat with.
    consciousness:
        The LLM adapter.
    memory:
        The memory adapter (for retrieving relevant memories).
    session_manager:
        Optional session manager to include today's session history.
    """

    def __init__(
        self,
        agent: Agent,
        consciousness: ConsciousnessAdapter,
        memory: MemoryAdapter,
        session_manager: SessionManager | None = None,
    ) -> None:
        self.agent = agent
        self.consciousness = consciousness
        self.memory = memory
        self.context_builder = ContextBuilder(memory=memory)
        self.session_manager = session_manager
        self._history: list[dict[str, str]] = []

    async def send(self, message: str) -> str:
        """Send a message to the agent and get a response.

        The agent's full identity, memories, and conversation history
        are included in the context.
        """
        # Build context from agent identity + memories
        identity = self.agent.read_all_identity()
        context = await self.context_builder.build_execution_context(
            self.agent, identity, messages=[], task=message,
        )

        # Add session history if available
        if self.session_manager:
            session_text = self.session_manager.render(self.agent.id)
            if session_text:
                context = context + "\n\n---\n\n" + session_text

        # Add chat history
        if self._history:
            history_lines = ["## Direct Conversation\n"]
            for turn in self._history[-10:]:  # last 10 turns
                role = turn["role"]
                content = turn["content"]
                if len(content) > 300:
                    content = content[:300] + "..."
                history_lines.append(f"**{role}:** {content}\n")
            context = context + "\n\n---\n\n" + "\n".join(history_lines)

        # Build prompt
        prompt = (
            "A human is talking to you directly. Respond as yourself — "
            "use your identity, knowledge, and current context to answer. "
            "Be helpful, concise, and honest about what you know and "
            "don't know.\n\n"
            f"Human: {message}"
        )

        # Send to consciousness
        response = await self.consciousness.think(
            agent_id=self.agent.id,
            context=context,
            prompt=prompt,
            priority=Priority.NORMAL,
            metadata={"call_type": "chat", "interactive": True},
        )

        # Record in history
        self._history.append({"role": "Human", "content": message})
        self._history.append({"role": self.agent.id, "content": response.content})

        return response.content

    @property
    def turn_count(self) -> int:
        return len(self._history) // 2


async def get_agent_logs(
    agent: Agent,
    memory: MemoryAdapter,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Gather recent activity logs for an agent.

    Returns a dict with journal, recent memories, task queue state,
    and session turns.
    """
    result: dict[str, Any] = {
        "agent_id": agent.id,
        "state": agent.state.value,
    }

    # Journal — today's entry
    from datetime import datetime
    journal_path = agent.journal_path()
    if journal_path.exists():
        result["journal_today"] = journal_path.read_text(encoding="utf-8")[:2000]
    else:
        result["journal_today"] = None

    # Recent journal entries
    journal_dir = agent.directory / "journal"
    if journal_dir.is_dir():
        entries = sorted(journal_dir.glob("*.md"), reverse=True)[:5]
        result["recent_journals"] = [
            {"date": p.stem, "preview": p.read_text(encoding="utf-8")[:200]}
            for p in entries
        ]
    else:
        result["recent_journals"] = []

    # Task queue
    task_data = agent.read_today("task_queue.json")
    if task_data:
        import json
        try:
            result["task_queue"] = json.loads(task_data)
        except json.JSONDecodeError:
            result["task_queue"] = None
    else:
        result["task_queue"] = None

    # Exception pile
    exc_data = agent.read_today("exception_pile.json")
    if exc_data:
        import json
        try:
            result["exceptions"] = json.loads(exc_data)
        except json.JSONDecodeError:
            result["exceptions"] = []
    else:
        result["exceptions"] = []

    # Recent memories
    memories = await memory.recall(agent.id, limit=limit, min_importance=3.0)
    result["recent_memories"] = [
        {"content": m.content[:150], "importance": m.importance, "tags": m.tags}
        for m in memories
    ]

    # Identity summary
    identity_text = agent.read_identity("identity")
    result["identity"] = identity_text[:500] if identity_text else None

    # Familiarity signals
    fam_data = agent.read_today("familiarity_signals.json")
    if fam_data:
        import json
        try:
            result["familiarity"] = json.loads(fam_data)[:10]
        except json.JSONDecodeError:
            result["familiarity"] = []
    else:
        result["familiarity"] = []

    return result
