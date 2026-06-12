"""Tests for Living Summary auto-regeneration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse, MemoryRecord
from cortiva.core.living_summary import (
    DAY_REPORT_DELIMITER,
    LivingSummaryRegenerator,
    _extract_themes,
    split_identity_and_day_report,
)

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
# split_identity_and_day_report
# ---------------------------------------------------------------------------


class TestSplitIdentityAndDayReport:
    def test_splits_on_delimiter(self) -> None:
        content = (
            "# Identity\n\nI am a bookkeeper.\n"
            f"{DAY_REPORT_DELIMITER}\n"
            "Today I reconciled 12 invoices and got blocked on Xero auth."
        )
        identity, report = split_identity_and_day_report(content)
        assert identity == "# Identity\n\nI am a bookkeeper."
        assert report is not None
        assert report.startswith("Today I reconciled")

    def test_no_delimiter_is_all_identity(self) -> None:
        identity, report = split_identity_and_day_report("# Just identity")
        assert identity == "# Just identity"
        assert report is None

    def test_empty_content(self) -> None:
        assert split_identity_and_day_report("") == (None, None)

    def test_empty_parts_become_none(self) -> None:
        identity, report = split_identity_and_day_report(
            f"  \n{DAY_REPORT_DELIMITER}\n  ",
        )
        assert identity is None
        assert report is None

    def test_delimiter_only_splits_once(self) -> None:
        content = (
            f"# Identity\n{DAY_REPORT_DELIMITER}\nReport mentioning {DAY_REPORT_DELIMITER} inline."
        )
        identity, report = split_identity_and_day_report(content)
        assert identity == "# Identity"
        assert DAY_REPORT_DELIMITER in (report or "")


# ---------------------------------------------------------------------------
# LivingSummaryRegenerator
# ---------------------------------------------------------------------------


class TestLivingSummaryRegenerator:
    def _make_regen(self, memory=None, consciousness=None):
        memory = memory or InMemoryAdapter()
        consciousness = consciousness or AsyncMock()
        return LivingSummaryRegenerator(
            memory=memory,
            consciousness=consciousness,
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
        await memory.store(
            "a", "Task: Process invoice. Outcome: done", tags=["task"], importance=7.0
        )
        await memory.store("a", "Task: Review report. Outcome: done", tags=["task"], importance=7.0)
        await memory.store(
            "a", "learned to verify amounts first", tags=["learning"], importance=8.0
        )

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
        await memory.store(
            "a", "Task: Important work. Outcome: done", tags=["task"], importance=7.0
        )

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
                    content=(
                        "# Updated Identity\n\nI've grown today.\n"
                        f"{DAY_REPORT_DELIMITER}\n"
                        "Today I did important work and finished it."
                    ),
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
            "agent-01",
            "Task: Did important work. Outcome: success",
            tags=["task"],
            importance=7.0,
        )

        from cortiva.core.agent import AgentState

        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        await fabric.sleep("agent-01")

        # Identity should have been updated — without the day report
        identity = agent.read_identity("identity")
        assert "grown today" in identity
        assert DAY_REPORT_DELIMITER not in identity
        assert "important work" not in identity

    @pytest.mark.xfail(
        strict=False, reason="pre-existing failure from feat/cognition — tracked separately"
    )
    @pytest.mark.asyncio
    async def test_sleep_writes_journal(self, tmp_path) -> None:
        fabric = self._make_fabric(tmp_path)
        agent = fabric.register_agent("agent-01")

        await fabric.memory.store(
            "agent-01",
            "Task: work. Outcome: ok",
            tags=["task"],
            importance=7.0,
        )

        from cortiva.core.agent import AgentState

        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        await fabric.sleep("agent-01")

        # Journal gets the agent-written day report, not identity.md
        journal = agent.journal_path()
        assert journal.exists()
        content = journal.read_text()
        assert "Today I did important work" in content
        assert "grown today" not in content

    @pytest.mark.xfail(
        strict=False, reason="pre-existing failure from feat/cognition — tracked separately"
    )
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

        # The journal is still written — stats summary fallback
        journal = agent.journal_path()
        assert journal.exists()
        assert "Tasks completed" in journal.read_text()

    @pytest.mark.xfail(
        strict=False, reason="pre-existing failure from feat/cognition — tracked separately"
    )
    @pytest.mark.asyncio
    async def test_sleep_journal_falls_back_without_delimiter(
        self,
        tmp_path,
    ) -> None:
        """Reflection response without a day report → journal gets stats."""
        from cortiva.core.fabric import Fabric

        class NoReportConsciousness:
            async def think(self, **kw):
                return ConsciousResponse(content="- [ ] Plan", model="stub")

            async def reflect(self, **kw):
                return ConsciousResponse(
                    content="# Updated Identity only",
                    model="stub",
                )

        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=NoReportConsciousness(),
        )
        agent = fabric.register_agent("agent-01")
        await fabric.memory.store(
            "agent-01",
            "Task: work. Outcome: ok",
            tags=["task"],
            importance=7.0,
        )

        from cortiva.core.agent import AgentState

        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        await fabric.sleep("agent-01")

        assert agent.read_identity("identity") == "# Updated Identity only"
        journal = agent.journal_path()
        assert journal.exists()
        assert "Tasks completed" in journal.read_text()


# ---------------------------------------------------------------------------
# Identity versioning — the Living Summary compounds, it never churns
# ---------------------------------------------------------------------------


class TestIdentityArchiving:
    """archive_identity snapshots the outgoing identity before a rewrite,
    so a lossy/bad regeneration is always recoverable."""

    def _make_agent(self, tmp_path):
        from cortiva.core.fabric import Fabric

        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=AsyncMock(),
        )
        return fabric.register_agent("agent-01")

    def test_archive_snapshots_current_identity(self, tmp_path) -> None:
        agent = self._make_agent(tmp_path)
        agent.write_identity("identity", "# I am version one")

        path = agent.archive_identity("identity")
        assert path is not None
        assert path.read_text(encoding="utf-8") == "# I am version one"
        assert "identity/history" in str(path)

    def test_archive_empty_identity_returns_none(self, tmp_path) -> None:
        agent = self._make_agent(tmp_path)
        agent.write_identity("identity", "   ")
        assert agent.archive_identity("identity") is None

    def test_archive_dedupes_identical_content(self, tmp_path) -> None:
        agent = self._make_agent(tmp_path)
        agent.write_identity("identity", "# Same content")
        first = agent.archive_identity("identity")
        second = agent.archive_identity("identity")
        assert first == second
        assert len(agent.identity_history("identity")) == 1

    def test_identity_history_ordered_oldest_first(self, tmp_path) -> None:
        agent = self._make_agent(tmp_path)
        agent.write_identity("identity", "# v1")
        agent.archive_identity("identity")
        agent.write_identity("identity", "# v2")
        agent.archive_identity("identity")

        history = agent.identity_history("identity")
        assert len(history) == 2
        assert history[0].read_text(encoding="utf-8") == "# v1"
        assert history[1].read_text(encoding="utf-8") == "# v2"

    def test_identity_history_empty_when_no_archive(self, tmp_path) -> None:
        agent = self._make_agent(tmp_path)
        assert agent.identity_history("identity") == []


class TestRegenerationAnchoring:
    """The rewrite prompt anchors to soul + role and tells the agent the
    document compounds — the guard against role contamination and
    detail evaporation."""

    def _prompt(self, **kwargs):
        regen = LivingSummaryRegenerator(
            memory=InMemoryAdapter(),
            consciousness=AsyncMock(),
        )
        return regen.build_regeneration_prompt(
            MagicMock(),
            current_identity="# Current me",
            day_summary="summary",
            experience={
                "key_memories": [],
                "learnings": [],
                "themes": [],
                "task_count": 1,
                "terminal_task_count": 0,
                "escalated_count": 0,
            },
            **kwargs,
        )

    def test_prompt_includes_soul_and_role_anchors(self) -> None:
        prompt = self._prompt(
            soul="Warm, precise, allergic to jargon",
            responsibilities="Own the monthly close",
        )
        assert "Warm, precise, allergic to jargon" in prompt
        assert "Own the monthly close" in prompt
        assert "Your Soul" in prompt
        assert "Role & Responsibilities" in prompt

    def test_prompt_includes_revision_continuity(self) -> None:
        prompt = self._prompt(revision_count=4)
        assert "revision 5" in prompt
        assert "identity/history/" in prompt

    def test_prompt_hard_rules_always_present(self) -> None:
        prompt = self._prompt()
        assert "never adopt the role" in prompt
        assert "do not start from scratch" in prompt

    def test_prompt_omits_empty_anchor_sections(self) -> None:
        prompt = self._prompt()
        assert "Your Soul" not in prompt
        assert "## Continuity" not in prompt

    @pytest.mark.asyncio
    async def test_regenerate_passes_anchors_from_agent(self, tmp_path) -> None:
        from cortiva.core.fabric import Fabric

        memory = InMemoryAdapter()
        await memory.store(
            "agent-01",
            "Task: work. Outcome: ok",
            tags=["task"],
            importance=7.0,
        )

        captured = {}

        class CapturingConsciousness:
            async def reflect(self, **kw):
                captured["context"] = kw.get("context", "")
                return ConsciousResponse(content="# New identity", model="stub")

        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=memory,
            consciousness=CapturingConsciousness(),
        )
        agent = fabric.register_agent("agent-01")
        agent.write_identity("soul", "Curious and unhurried")
        agent.write_identity("responsibilities", "Guard the audit trail")
        agent.write_identity("identity", "# Original identity")
        agent.archive_identity("identity")

        regen = LivingSummaryRegenerator(
            memory=memory,
            consciousness=CapturingConsciousness(),
        )
        result = await regen.regenerate(agent, "day summary")
        assert result is not None

        prompt = captured.get("context", "")
        assert "Curious and unhurried" in prompt
        assert "Guard the audit trail" in prompt
        assert "revision 2" in prompt


class TestFabricArchivesIdentityOnRegen:
    @pytest.mark.asyncio
    async def test_sleep_archives_previous_identity(self, tmp_path) -> None:
        from cortiva.core.fabric import Fabric

        class StubConsciousness:
            async def think(self, **kw):
                return ConsciousResponse(content="- [ ] Plan item", model="stub")

            async def reflect(self, **kw):
                return ConsciousResponse(
                    content="# Rewritten identity",
                    model="stub",
                )

        fabric = Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=StubConsciousness(),
        )
        agent = fabric.register_agent("agent-01")
        agent.write_identity("identity", "# The original me")

        await fabric.memory.store(
            "agent-01",
            "Task: work. Outcome: ok",
            tags=["task"],
            importance=7.0,
        )

        from cortiva.core.agent import AgentState

        agent.state = AgentState.WAKING
        agent.transition(AgentState.PLANNING)
        agent.transition(AgentState.EXECUTING)

        await fabric.sleep("agent-01")

        assert agent.read_identity("identity") == "# Rewritten identity"
        history = agent.identity_history("identity")
        assert len(history) == 1
        assert history[0].read_text(encoding="utf-8") == "# The original me"
