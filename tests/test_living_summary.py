"""Tests for Living Summary auto-regeneration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse, MemoryRecord
from cortiva.core.living_summary import LivingSummaryRegenerator, _extract_themes


# ---------------------------------------------------------------------------
# _extract_themes
# ---------------------------------------------------------------------------


class TestExtractThemes:
    def test_extracts_recurring_words(self) -> None:
        memories = [
            MemoryRecord(id="1", content="Invoice processing for vendor Acme", agent_id="a"),
            MemoryRecord(id="2", content="Invoice verification for vendor Beta", agent_id="a"),
            MemoryRecord(id="3", content="Invoice reconciliation vendor Gamma", agent_id="a"),
        ]
        themes = _extract_themes(memories)
        assert "invoice" in themes
        assert "vendor" in themes

    def test_filters_stop_words(self) -> None:
        memories = [
            MemoryRecord(id="1", content="The task was completed", agent_id="a"),
            MemoryRecord(id="2", content="The task was done", agent_id="a"),
        ]
        themes = _extract_themes(memories)
        # "the", "was", "task", "completed" are stop words
        assert "the" not in themes

    def test_empty_memories(self) -> None:
        assert _extract_themes([]) == []

    def test_single_occurrence_filtered(self) -> None:
        memories = [
            MemoryRecord(id="1", content="unique word here", agent_id="a"),
        ]
        themes = _extract_themes(memories)
        assert len(themes) == 0


# ---------------------------------------------------------------------------
# LivingSummaryRegenerator
# ---------------------------------------------------------------------------


class TestLivingSummaryRegenerator:
    def _make_regen(self, memory=None, consciousness=None):
        memory = memory or InMemoryAdapter()
        consciousness = consciousness or AsyncMock()
        return LivingSummaryRegenerator(
            memory=memory, consciousness=consciousness,
        )

    @pytest.mark.asyncio
    async def test_gather_experience_empty(self) -> None:
        regen = self._make_regen()
        exp = await regen.gather_experience("agent-01")
        assert exp["key_memories"] == []
        assert exp["learnings"] == []
        assert exp["task_count"] == 0

    @pytest.mark.asyncio
    async def test_gather_experience_with_data(self) -> None:
        memory = InMemoryAdapter()
        await memory.store("a", "Task: Process invoice. Outcome: done", tags=["task"], importance=7.0)
        await memory.store("a", "Task: Review report. Outcome: done", tags=["task"], importance=7.0)
        await memory.store("a", "learned to verify amounts first", tags=["learning"], importance=8.0)

        regen = self._make_regen(memory=memory)
        exp = await regen.gather_experience("a")
        assert exp["task_count"] == 2
        assert len(exp["learnings"]) >= 1

    @pytest.mark.asyncio
    async def test_build_prompt_includes_identity(self) -> None:
        regen = self._make_regen()
        agent = MagicMock()
        agent.read_identity.return_value = "# I am a bookkeeper"

        prompt = regen.build_regeneration_prompt(
            agent,
            current_identity="# I am a bookkeeper",
            day_summary="Tasks completed: 5",
            experience={
                "key_memories": [],
                "learnings": [],
                "themes": [],
                "task_count": 5,
                "terminal_task_count": 2,
                "escalated_count": 1,
            },
        )
        assert "I am a bookkeeper" in prompt
        assert "Tasks completed: 5" in prompt
        assert "Experience Stats" in prompt

    @pytest.mark.asyncio
    async def test_build_prompt_includes_memories(self) -> None:
        regen = self._make_regen()
        agent = MagicMock()

        memories = [
            MemoryRecord(id="1", content="Handled complex invoice", agent_id="a", importance=8.0),
        ]
        prompt = regen.build_regeneration_prompt(
            agent,
            current_identity="# Agent",
            day_summary="summary",
            experience={
                "key_memories": memories,
                "learnings": [],
                "themes": ["invoice", "vendor"],
                "task_count": 0,
                "terminal_task_count": 0,
                "escalated_count": 0,
            },
        )
        assert "Handled complex invoice" in prompt
        assert "invoice, vendor" in prompt

    @pytest.mark.asyncio
    async def test_regenerate_returns_content(self) -> None:
        memory = InMemoryAdapter()
        await memory.store("a", "Task: Important work. Outcome: done", tags=["task"], importance=7.0)

        consciousness = AsyncMock()
        consciousness.reflect.return_value = ConsciousResponse(
            content="# Updated Identity\n\nI am an experienced agent.",
            model="test",
        )

        regen = LivingSummaryRegenerator(memory=memory, consciousness=consciousness)

        agent = MagicMock()
        agent.id = "a"
        agent.read_identity.return_value = "# Old identity"

        result = await regen.regenerate(agent, "day summary")
        assert result is not None
        assert "experienced agent" in result
        consciousness.reflect.assert_called_once()

    @pytest.mark.asyncio
    async def test_regenerate_skips_when_no_experience(self) -> None:
        memory = InMemoryAdapter()
        consciousness = AsyncMock()

        regen = LivingSummaryRegenerator(memory=memory, consciousness=consciousness)

        agent = MagicMock()
        agent.id = "a"
        agent.read_identity.return_value = "# New agent"

        result = await regen.regenerate(agent, "day summary")
        assert result is None
        consciousness.reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_regenerate_handles_empty_response(self) -> None:
        memory = InMemoryAdapter()
        await memory.store("a", "Task: work. Outcome: ok", tags=["task"], importance=7.0)

        consciousness = AsyncMock()
        consciousness.reflect.return_value = ConsciousResponse(content="", model="test")

        regen = LivingSummaryRegenerator(memory=memory, consciousness=consciousness)

        agent = MagicMock()
        agent.id = "a"
        agent.read_identity.return_value = "# Agent"

        result = await regen.regenerate(agent, "summary")
        assert result is None


# ---------------------------------------------------------------------------
# Fabric integration — sleep uses regenerator
# ---------------------------------------------------------------------------


class TestFabricLivingSummaryIntegration:
    def _make_fabric(self, tmp_path):
        from cortiva.core.fabric import Fabric

        memory = InMemoryAdapter()

        class StubConsciousness:
            async def think(self, **kw):
                return ConsciousResponse(content="- [ ] Plan item", model="stub")
            async def reflect(self, **kw):
                return ConsciousResponse(
                    content="# Updated Identity\n\nI've grown today.",
                    model="stub",
                )

        return Fabric(
            agents_dir=tmp_path / "agents",
            memory=memory,
            consciousness=StubConsciousness(),
        )

    @pytest.mark.asyncio
    async def test_sleep_regenerates_identity(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        agent = fabric.register_agent("agent-01")

        # Store some experience so regeneration isn't skipped
        await fabric.memory.store(
            "agent-01", "Task: Did important work. Outcome: success",
            tags=["task"], importance=7.0,
        )

        from cortiva.core.agent import AgentState
        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        await fabric.sleep("agent-01")

        # Identity should have been updated
        identity = agent.read_identity("identity")
        assert "grown today" in identity

    @pytest.mark.asyncio
    async def test_sleep_writes_journal(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        agent = fabric.register_agent("agent-01")

        await fabric.memory.store(
            "agent-01", "Task: work. Outcome: ok",
            tags=["task"], importance=7.0,
        )

        from cortiva.core.agent import AgentState
        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        await fabric.sleep("agent-01")

        journal = agent.journal_path()
        assert journal.exists()
        content = journal.read_text()
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_sleep_skips_regen_for_new_agent(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        agent = fabric.register_agent("agent-01")

        # No experience stored — regen should be skipped
        original_identity = agent.read_identity("identity")

        from cortiva.core.agent import AgentState
        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        await fabric.sleep("agent-01")

        # Identity should NOT have been updated (no experience)
        identity = agent.read_identity("identity")
        assert identity == original_identity
