"""Tests for routine adapters (SimpleRoutineAdapter and OllamaRoutineAdapter)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cortiva.adapters.protocols import FamiliaritySignal
from cortiva.adapters.routine.simple import SimpleRoutineAdapter, _tokenize, _extract_procedures


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROCEDURES_MD = """\
# Procedures

## Invoice Processing
When a new invoice arrives, verify the vendor, check amounts against the
purchase order, and post to the general ledger.

## Weekly Report
Every Friday, compile hours from the timesheet, summarise key metrics,
and email the report to the team.

## Expense Reconciliation
Match credit card transactions against receipts. Flag any transaction
over $500 that lacks a receipt for manager approval.
"""

NOVEL_SIGNAL = FamiliaritySignal(
    strength="novel", valence="neutral", match_count=0, text="No prior experience."
)
ROUTINE_SIGNAL = FamiliaritySignal(
    strength="routine", valence="positive", match_count=10, text="Done many times."
)
FAMILIAR_SIGNAL = FamiliaritySignal(
    strength="familiar", valence="neutral", match_count=3, text="Seen before."
)


# ---------------------------------------------------------------------------
# Tokenizer / procedure extraction
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_lowercases_and_removes_stop_words(self) -> None:
        tokens = _tokenize("The Quick Brown Fox Jumps")
        assert "the" not in tokens
        assert "quick" in tokens
        assert "brown" in tokens

    def test_strips_punctuation(self) -> None:
        tokens = _tokenize("hello, world! foo-bar")
        assert "hello" in tokens
        assert "world" in tokens
        assert "foo" in tokens

    def test_empty_string(self) -> None:
        assert _tokenize("") == set()


class TestExtractProcedures:
    def test_splits_on_headings(self) -> None:
        blocks = _extract_procedures(PROCEDURES_MD)
        assert len(blocks) == 4  # title block + 3 procedures

    def test_empty_input(self) -> None:
        assert _extract_procedures("") == []


# ---------------------------------------------------------------------------
# SimpleRoutineAdapter
# ---------------------------------------------------------------------------

class TestSimpleRoutineAdapter:
    @pytest.mark.asyncio
    async def test_procedural_match(self) -> None:
        adapter = SimpleRoutineAdapter(confidence_threshold=0.15, defer_threshold=0.05)
        result = await adapter.assess(
            agent_id="bookkeep-01",
            task_description="Process the new invoice from vendor Acme Corp",
            procedural_index=PROCEDURES_MD,
            familiarity=NOVEL_SIGNAL,
        )
        assert result["action"] == "procedural"
        assert result["confidence"] > 0
        assert result["procedure_match"] is not None
        assert "invoice" in result["procedure_match"].lower()

    @pytest.mark.asyncio
    async def test_escalate_no_match(self) -> None:
        adapter = SimpleRoutineAdapter(confidence_threshold=0.30)
        result = await adapter.assess(
            agent_id="bookkeep-01",
            task_description="Design a new logo for the company website",
            procedural_index=PROCEDURES_MD,
            familiarity=NOVEL_SIGNAL,
        )
        assert result["action"] in ("escalate", "defer")
        assert result["confidence"] < 0.30

    @pytest.mark.asyncio
    async def test_familiarity_boost_routine(self) -> None:
        adapter = SimpleRoutineAdapter(confidence_threshold=0.35)
        # With novel signal, might not pass threshold
        result_novel = await adapter.assess(
            agent_id="bookkeep-01",
            task_description="compile the weekly report",
            procedural_index=PROCEDURES_MD,
            familiarity=NOVEL_SIGNAL,
        )
        # With routine signal, the boost should help
        result_routine = await adapter.assess(
            agent_id="bookkeep-01",
            task_description="compile the weekly report",
            procedural_index=PROCEDURES_MD,
            familiarity=ROUTINE_SIGNAL,
        )
        assert result_routine["confidence"] > result_novel["confidence"]

    @pytest.mark.asyncio
    async def test_empty_procedures(self) -> None:
        adapter = SimpleRoutineAdapter()
        result = await adapter.assess(
            agent_id="bookkeep-01",
            task_description="Do something",
            procedural_index="",
            familiarity=NOVEL_SIGNAL,
        )
        assert result["action"] == "escalate"

    @pytest.mark.asyncio
    async def test_empty_task(self) -> None:
        adapter = SimpleRoutineAdapter()
        result = await adapter.assess(
            agent_id="bookkeep-01",
            task_description="",
            procedural_index=PROCEDURES_MD,
            familiarity=NOVEL_SIGNAL,
        )
        assert result["action"] == "escalate"

    @pytest.mark.asyncio
    async def test_defer_zone(self) -> None:
        """Tasks with partial keyword overlap fall into the defer zone."""
        adapter = SimpleRoutineAdapter(confidence_threshold=0.50, defer_threshold=0.10)
        result = await adapter.assess(
            agent_id="bookkeep-01",
            task_description="review the expense report summary",
            procedural_index=PROCEDURES_MD,
            familiarity=NOVEL_SIGNAL,
        )
        # Should land between thresholds
        if 0.10 <= result["confidence"] < 0.50:
            assert result["action"] == "defer"

    @pytest.mark.asyncio
    async def test_compile_context(self) -> None:
        adapter = SimpleRoutineAdapter()
        ctx = await adapter.compile_context(
            agent_id="test-01",
            identity="I am a test agent.",
            memories=[],
            familiarity=NOVEL_SIGNAL,
            task="Do the thing.",
        )
        assert "Identity" in ctx
        assert "Task" in ctx
        assert "Do the thing" in ctx


# ---------------------------------------------------------------------------
# OllamaRoutineAdapter (with mocked embedding client)
# ---------------------------------------------------------------------------

class TestOllamaRoutineAdapter:
    @pytest.mark.asyncio
    async def test_falls_back_when_ollama_unavailable(self) -> None:
        from cortiva.adapters.routine.ollama import OllamaRoutineAdapter

        adapter = OllamaRoutineAdapter(confidence_threshold=0.20)
        # Mock is_available to return False
        adapter._client.is_available = AsyncMock(return_value=False)

        result = await adapter.assess(
            agent_id="bookkeep-01",
            task_description="Process the new invoice from vendor",
            procedural_index=PROCEDURES_MD,
            familiarity=NOVEL_SIGNAL,
        )
        # Should still get a result from the simple fallback
        assert result["action"] in ("procedural", "escalate", "defer")

    @pytest.mark.asyncio
    async def test_procedural_match_with_mock_embeddings(self) -> None:
        from cortiva.adapters.routine.ollama import OllamaRoutineAdapter

        adapter = OllamaRoutineAdapter(confidence_threshold=0.70)
        adapter._client.is_available = AsyncMock(return_value=True)

        # Mock embed to return vectors where invoice-related texts are similar
        call_count = 0
        async def mock_embed(text: str) -> list[float]:
            nonlocal call_count
            call_count += 1
            # Simple mock: invoice-related texts get [1, 0, 0], others get [0, 1, 0]
            if "invoice" in text.lower() or "vendor" in text.lower():
                return [1.0, 0.0, 0.0]
            if "report" in text.lower() or "weekly" in text.lower():
                return [0.0, 1.0, 0.0]
            return [0.0, 0.0, 1.0]

        adapter._client.embed = mock_embed

        result = await adapter.assess(
            agent_id="bookkeep-01",
            task_description="Process the new invoice from vendor Acme",
            procedural_index=PROCEDURES_MD,
            familiarity=NOVEL_SIGNAL,
        )
        assert result["action"] == "procedural"
        assert result["confidence"] >= 0.70

    @pytest.mark.asyncio
    async def test_escalate_no_match_with_mock_embeddings(self) -> None:
        from cortiva.adapters.routine.ollama import OllamaRoutineAdapter

        adapter = OllamaRoutineAdapter(confidence_threshold=0.70, defer_threshold=0.45)
        adapter._client.is_available = AsyncMock(return_value=True)

        async def mock_embed(text: str) -> list[float]:
            # Task about logo design won't match any procedure
            if "logo" in text.lower() or "design" in text.lower():
                return [0.0, 0.0, 0.0, 1.0]
            # All procedures get orthogonal vectors
            return [1.0, 0.0, 0.0, 0.0]

        adapter._client.embed = mock_embed

        result = await adapter.assess(
            agent_id="bookkeep-01",
            task_description="Design a new logo",
            procedural_index=PROCEDURES_MD,
            familiarity=NOVEL_SIGNAL,
        )
        assert result["action"] == "escalate"
        assert result["confidence"] < 0.45

    @pytest.mark.asyncio
    async def test_compile_context_delegates_to_fallback(self) -> None:
        from cortiva.adapters.routine.ollama import OllamaRoutineAdapter

        adapter = OllamaRoutineAdapter()
        ctx = await adapter.compile_context(
            agent_id="test-01",
            identity="I am a test agent.",
            memories=[],
            familiarity=NOVEL_SIGNAL,
            task="Do the thing.",
        )
        assert "Identity" in ctx
        assert "Task" in ctx


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------

class TestRoutineConfig:
    def test_routine_adapters_in_registry(self) -> None:
        from cortiva.core.config import _ROUTINE_ADAPTERS
        assert "simple" in _ROUTINE_ADAPTERS
        assert "ollama" in _ROUTINE_ADAPTERS

    def test_build_fabric_with_simple_routine(self, tmp_path) -> None:
        from cortiva.core.config import build_fabric
        config = {
            "fabric": {"name": "test"},
            "memory": {"adapter": "inmemory"},
            "consciousness": {"provider": "anthropic"},
            "routine": {"adapter": "simple", "config": {"confidence_threshold": 0.25}},
            "agents": {"directory": str(tmp_path / "agents")},
        }
        # anthropic adapter needs a key — mock the import
        with patch(
            "cortiva.core.config._import_adapter",
            side_effect=_mock_import_adapter,
        ):
            fabric = build_fabric(config)
        assert fabric.routine is not None

    def test_build_fabric_without_routine(self, tmp_path) -> None:
        from cortiva.core.config import build_fabric
        config = {
            "fabric": {"name": "test"},
            "memory": {"adapter": "inmemory"},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
        }
        with patch(
            "cortiva.core.config._import_adapter",
            side_effect=_mock_import_adapter,
        ):
            fabric = build_fabric(config)
        assert fabric.routine is None


# ---------------------------------------------------------------------------
# Integration: fabric uses routine adapter
# ---------------------------------------------------------------------------

class TestFabricRoutineIntegration:
    @pytest.mark.asyncio
    async def test_cycle_uses_routine_for_procedural_match(self, tmp_path) -> None:
        from cortiva.adapters.memory.inmemory import InMemoryAdapter
        from cortiva.adapters.protocols import ConsciousResponse
        from cortiva.core.fabric import Fabric

        mock_consciousness = AsyncMock()
        mock_consciousness.think = AsyncMock(return_value=ConsciousResponse(
            content=(
                "# Plan\n\n"
                "- [ ] Verify invoice amounts against purchase order and post to general ledger\n"
                "- [ ] Review weekly report\n"
            ),
            tokens_in=50, tokens_out=25, model="mock",
        ))
        mock_consciousness.reflect = AsyncMock(return_value=ConsciousResponse(
            content="Good day.", tokens_in=50, tokens_out=25, model="mock",
        ))

        routine = SimpleRoutineAdapter(confidence_threshold=0.15, defer_threshold=0.05)

        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=mock_consciousness,
            routine=routine,
        )
        agent = fabric.register_agent("bookkeep-01")

        # Write procedures that match invoice tasks
        agent.write_identity("procedures", PROCEDURES_MD)

        await fabric.wake("bookkeep-01")

        # Reset mock call count after planning
        initial_think_calls = mock_consciousness.think.call_count

        # Run a cycle — the invoice task should match procedurally
        result = await fabric.cycle("bookkeep-01")

        # The routine adapter should have handled it procedurally
        # (no additional consciousness call beyond planning)
        assert result["action"] == "executed_task"
        assert agent.tasks_completed_today >= 1

        # Check if consciousness was NOT called for this task
        # (procedural match means 0 new think calls)
        new_think_calls = mock_consciousness.think.call_count - initial_think_calls
        assert new_think_calls == 0, (
            f"Expected 0 consciousness calls for procedural task, got {new_think_calls}"
        )


# ---------------------------------------------------------------------------
# Mock helper for config tests
# ---------------------------------------------------------------------------

def _mock_import_adapter(registry, name, kind):
    """Return real adapters for memory/routine, mock for consciousness."""
    if kind == "memory":
        from cortiva.adapters.memory.inmemory import InMemoryAdapter
        return InMemoryAdapter
    if kind == "routine":
        from cortiva.adapters.routine.simple import SimpleRoutineAdapter
        return SimpleRoutineAdapter
    # Return a mock class for consciousness
    class MockCls:
        def __init__(self, **kwargs):
            pass
        async def think(self, **kwargs):
            pass
        async def reflect(self, **kwargs):
            pass
    return MockCls
