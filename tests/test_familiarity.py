"""Tests for the familiarity engine."""

from __future__ import annotations

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.core.familiarity import (
    FamiliarityEngine,
    _build_text,
    _classify_strength,
    _infer_valence,
)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

class TestClassifyStrength:
    def test_novel(self) -> None:
        assert _classify_strength(0) == "novel"

    def test_vague(self) -> None:
        assert _classify_strength(1) == "vague_recognition"

    def test_familiar(self) -> None:
        assert _classify_strength(2) == "familiar"
        assert _classify_strength(4) == "familiar"

    def test_routine(self) -> None:
        assert _classify_strength(5) == "routine"
        assert _classify_strength(20) == "routine"


class TestInferValence:
    def test_empty_memories(self) -> None:
        assert _infer_valence([]) == "neutral"

    def test_positive(self) -> None:
        from cortiva.adapters.protocols import MemoryRecord
        memories = [
            MemoryRecord(id="1", content="Task completed success", agent_id="a"),
            MemoryRecord(id="2", content="Great outcome approved", agent_id="a"),
        ]
        assert _infer_valence(memories) == "positive"

    def test_cautious(self) -> None:
        from cortiva.adapters.protocols import MemoryRecord
        memories = [
            MemoryRecord(id="1", content="Task fail error bug", agent_id="a"),
            MemoryRecord(id="2", content="Problem with wrong output", agent_id="a"),
            MemoryRecord(id="3", content="Normal task done", agent_id="a"),
        ]
        assert _infer_valence(memories) == "cautious"

    def test_negative(self) -> None:
        from cortiva.adapters.protocols import MemoryRecord
        memories = [
            MemoryRecord(id="1", content="Task fail", agent_id="a"),
        ]
        assert _infer_valence(memories) == "negative"

    def test_neutral(self) -> None:
        from cortiva.adapters.protocols import MemoryRecord
        memories = [
            MemoryRecord(id="1", content="processed the data", agent_id="a"),
        ]
        assert _infer_valence(memories) == "neutral"


class TestBuildText:
    def test_novel(self) -> None:
        text = _build_text("novel", "neutral", 0)
        assert "entirely new" in text

    def test_routine_positive(self) -> None:
        text = _build_text("routine", "positive", 8)
        assert "routine" in text
        assert "8 similar" in text
        assert "positive" in text

    def test_familiar_cautious(self) -> None:
        text = _build_text("familiar", "cautious", 3)
        assert "familiar" in text
        assert "caution" in text


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------

class TestFamiliarityEngine:
    @pytest.mark.asyncio
    async def test_novel_task(self) -> None:
        mem = InMemoryAdapter()
        engine = FamiliarityEngine(mem)

        signal = await engine.assess("agent-01", "something completely new")
        assert signal.strength == "novel"
        assert signal.match_count == 0
        assert signal.retrieved == []

    @pytest.mark.asyncio
    async def test_routine_task(self) -> None:
        mem = InMemoryAdapter()
        # Store enough similar memories to cross the routine threshold
        for i in range(6):
            await mem.store(
                "agent-01",
                f"Processed monthly invoice #{i}",
                tags=["invoice"],
                importance=5.0,
            )

        engine = FamiliarityEngine(mem)
        signal = await engine.assess("agent-01", "invoice")
        assert signal.strength == "routine"
        assert signal.match_count >= 5

    @pytest.mark.asyncio
    async def test_familiar_task(self) -> None:
        mem = InMemoryAdapter()
        await mem.store("agent-01", "weekly report compiled", importance=5.0)
        await mem.store("agent-01", "weekly report sent to team", importance=5.0)

        engine = FamiliarityEngine(mem)
        signal = await engine.assess("agent-01", "weekly report")
        assert signal.strength == "familiar"
        assert signal.match_count == 2

    @pytest.mark.asyncio
    async def test_vague_recognition(self) -> None:
        mem = InMemoryAdapter()
        await mem.store("agent-01", "handled a refund request once", importance=5.0)

        engine = FamiliarityEngine(mem)
        signal = await engine.assess("agent-01", "refund")
        assert signal.strength == "vague_recognition"
        assert signal.match_count == 1

    @pytest.mark.asyncio
    async def test_valence_from_negative_memories(self) -> None:
        mem = InMemoryAdapter()
        await mem.store("agent-01", "API call fail error", importance=5.0)

        engine = FamiliarityEngine(mem)
        signal = await engine.assess("agent-01", "API call")
        assert signal.valence == "negative"

    @pytest.mark.asyncio
    async def test_namespace_isolation(self) -> None:
        mem = InMemoryAdapter()
        await mem.store("agent-01", "processed invoice", importance=5.0)

        engine = FamiliarityEngine(mem)
        signal = await engine.assess("agent-02", "invoice")
        assert signal.strength == "novel"
        assert signal.match_count == 0

    @pytest.mark.asyncio
    async def test_min_importance_filter(self) -> None:
        mem = InMemoryAdapter()
        await mem.store("agent-01", "low importance invoice note", importance=1.0)

        engine = FamiliarityEngine(mem, min_importance=3.0)
        signal = await engine.assess("agent-01", "invoice")
        assert signal.strength == "novel"

    @pytest.mark.asyncio
    async def test_retrieved_memories_attached(self) -> None:
        mem = InMemoryAdapter()
        await mem.store("agent-01", "did the thing before", importance=5.0)

        engine = FamiliarityEngine(mem)
        signal = await engine.assess("agent-01", "thing")
        assert len(signal.retrieved) == 1
        assert "thing" in signal.retrieved[0].content
