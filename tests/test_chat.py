"""Tests for direct agent conversation and logs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cortiva.core.agent import Agent, AgentState
from cortiva.core.chat import AgentChat, get_agent_logs


class TestAgentChat:
    def _make_agent(self, tmp_path: Path) -> Agent:
        agent_dir = tmp_path / "test-agent"
        agent_dir.mkdir()
        (agent_dir / "identity").mkdir()
        (agent_dir / "identity" / "identity.md").write_text("# Test Agent\nI am a test agent.")
        (agent_dir / "identity" / "soul.md").write_text("")
        (agent_dir / "identity" / "skills.md").write_text("")
        (agent_dir / "identity" / "responsibilities.md").write_text("")
        (agent_dir / "identity" / "procedures.md").write_text("")
        (agent_dir / "today").mkdir()
        (agent_dir / "identity" / "plan.md").write_text("")  # plan in identity for IDENTITY_FILES
        return Agent(id="test-agent", directory=agent_dir, state=AgentState.EXECUTING)

    @pytest.mark.asyncio
    async def test_send_message(self, tmp_path: Path) -> None:
        agent = self._make_agent(tmp_path)

        mock_consciousness = AsyncMock()
        mock_consciousness.think.return_value = AsyncMock(
            content="I'm working on the auth module today.",
            tokens_in=100,
            tokens_out=50,
        )

        mock_memory = AsyncMock()
        mock_memory.search.return_value = []
        mock_memory.recall.return_value = []

        chat = AgentChat(
            agent=agent,
            consciousness=mock_consciousness,
            memory=mock_memory,
        )

        response = await chat.send("What are you working on?")
        assert "auth module" in response
        assert chat.turn_count == 1

        # Verify consciousness was called with agent context
        call_args = mock_consciousness.think.call_args
        assert call_args.kwargs["agent_id"] == "test-agent"
        assert "chat" in str(call_args.kwargs.get("metadata", {}))

    @pytest.mark.asyncio
    async def test_conversation_history(self, tmp_path: Path) -> None:
        agent = self._make_agent(tmp_path)

        mock_consciousness = AsyncMock()
        mock_consciousness.think.return_value = AsyncMock(
            content="Response",
            tokens_in=50,
            tokens_out=25,
        )

        mock_memory = AsyncMock()
        mock_memory.search.return_value = []
        mock_memory.recall.return_value = []

        chat = AgentChat(
            agent=agent,
            consciousness=mock_consciousness,
            memory=mock_memory,
        )

        await chat.send("Hello")
        await chat.send("Follow up question")
        assert chat.turn_count == 2

        # Second call should include history in context
        second_call_context = mock_consciousness.think.call_args_list[1].kwargs["context"]
        assert "Direct Conversation" in second_call_context
        assert "Hello" in second_call_context


class TestGetAgentLogs:
    @pytest.mark.asyncio
    async def test_basic_logs(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "test-agent"
        agent_dir.mkdir()
        (agent_dir / "identity").mkdir()
        (agent_dir / "identity" / "identity.md").write_text("# Test Agent")
        (agent_dir / "today").mkdir()
        (agent_dir / "journal").mkdir()

        agent = Agent(id="test-agent", directory=agent_dir)

        mock_memory = AsyncMock()
        mock_memory.recall.return_value = []

        logs = await get_agent_logs(agent, mock_memory)
        assert logs["agent_id"] == "test-agent"
        assert "identity" in logs
        assert logs["recent_memories"] == []

    @pytest.mark.asyncio
    async def test_logs_with_task_queue(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "test-agent"
        agent_dir.mkdir()
        (agent_dir / "identity").mkdir()
        (agent_dir / "identity" / "identity.md").write_text("# Test")
        (agent_dir / "today").mkdir()
        (agent_dir / "journal").mkdir()

        import json
        (agent_dir / "today" / "task_queue.json").write_text(json.dumps({
            "tasks": [
                {"id": "t1", "description": "Fix bug", "status": "done"},
                {"id": "t2", "description": "Write tests", "status": "pending"},
            ],
            "summary": {"done": 1, "pending": 1},
        }))

        agent = Agent(id="test-agent", directory=agent_dir)
        mock_memory = AsyncMock()
        mock_memory.recall.return_value = []

        logs = await get_agent_logs(agent, mock_memory)
        assert logs["task_queue"] is not None
        assert len(logs["task_queue"]["tasks"]) == 2

    @pytest.mark.asyncio
    async def test_logs_with_journal(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "test-agent"
        agent_dir.mkdir()
        (agent_dir / "identity").mkdir()
        (agent_dir / "identity" / "identity.md").write_text("# Test")
        (agent_dir / "today").mkdir()
        journal_dir = agent_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "2026-04-05.md").write_text("Reflected on the day.")
        (journal_dir / "2026-04-06.md").write_text("Today was productive.")

        agent = Agent(id="test-agent", directory=agent_dir)
        mock_memory = AsyncMock()
        mock_memory.recall.return_value = []

        logs = await get_agent_logs(agent, mock_memory)
        assert len(logs["recent_journals"]) == 2
