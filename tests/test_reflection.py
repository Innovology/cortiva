"""Tests for reflection suffix parsing and fabric integration."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core.fabric import Fabric
from cortiva.core.reflection import REFLECTION_DELIMITER, parse_reflection_suffix

# ---------------------------------------------------------------------------
# TestParseReflectionSuffix — parser unit tests
# ---------------------------------------------------------------------------


class TestParseReflectionSuffix:
    def test_no_delimiter(self) -> None:
        text = "Just a normal response with no reflection."
        result = parse_reflection_suffix(text)
        assert result.clean_content == text
        assert result.suffix is None

    def test_valid_suffix(self) -> None:
        suffix_data = {
            "outcome": "Filed the report",
            "learned": "CSV exports need UTF-8 BOM for Excel",
            "prediction_error": "Expected 10 rows, got 50",
            "procedure_update": "- Always add BOM header to CSV exports",
            "messages": [{"to": "manager-01", "content": "Report ready"}],
            "escalation": "Need access to prod database",
        }
        text = f"Main response content{REFLECTION_DELIMITER}{json.dumps(suffix_data)}"
        result = parse_reflection_suffix(text)

        assert result.clean_content == "Main response content"
        assert result.suffix is not None
        assert result.suffix.outcome == "Filed the report"
        assert result.suffix.learned == "CSV exports need UTF-8 BOM for Excel"
        assert result.suffix.prediction_error == "Expected 10 rows, got 50"
        assert result.suffix.procedure_update == "- Always add BOM header to CSV exports"
        assert len(result.suffix.messages) == 1
        assert result.suffix.messages[0]["to"] == "manager-01"
        assert result.suffix.escalation == "Need access to prod database"

    def test_malformed_json(self) -> None:
        text = f"Response{REFLECTION_DELIMITER}{{not valid json}}"
        result = parse_reflection_suffix(text)
        assert result.clean_content == text
        assert result.suffix is None

    def test_non_object_json(self) -> None:
        text = f"Response{REFLECTION_DELIMITER}[1, 2, 3]"
        result = parse_reflection_suffix(text)
        assert result.clean_content == text
        assert result.suffix is None

    def test_code_fences_stripped(self) -> None:
        suffix_data = {"outcome": "Done", "learned": "Fences are fine"}
        fenced = f"```json\n{json.dumps(suffix_data)}\n```"
        text = f"Response{REFLECTION_DELIMITER}{fenced}"
        result = parse_reflection_suffix(text)

        assert result.clean_content == "Response"
        assert result.suffix is not None
        assert result.suffix.outcome == "Done"
        assert result.suffix.learned == "Fences are fine"

    def test_partial_fields(self) -> None:
        suffix_data = {"learned": "Only a learning, nothing else"}
        text = f"Content{REFLECTION_DELIMITER}{json.dumps(suffix_data)}"
        result = parse_reflection_suffix(text)

        assert result.suffix is not None
        assert result.suffix.learned == "Only a learning, nothing else"
        assert result.suffix.outcome is None
        assert result.suffix.prediction_error is None
        assert result.suffix.procedure_update is None
        assert result.suffix.messages == []
        assert result.suffix.escalation is None

    def test_messages_field(self) -> None:
        suffix_data = {
            "messages": [
                {"to": "agent-a", "content": "Hello"},
                {"to": "agent-b", "content": "World"},
            ]
        }
        text = f"Content{REFLECTION_DELIMITER}{json.dumps(suffix_data)}"
        result = parse_reflection_suffix(text)

        assert result.suffix is not None
        assert len(result.suffix.messages) == 2
        assert result.suffix.messages[1]["to"] == "agent-b"

    def test_empty_string(self) -> None:
        result = parse_reflection_suffix("")
        assert result.clean_content == ""
        assert result.suffix is None

    def test_delimiter_with_no_json(self) -> None:
        text = f"Content{REFLECTION_DELIMITER}"
        result = parse_reflection_suffix(text)
        assert result.clean_content == text
        assert result.suffix is None


# ---------------------------------------------------------------------------
# TestFabricReflectionIntegration — end-to-end fabric tests
# ---------------------------------------------------------------------------


class MockConsciousnessWithReflection:
    """Mock consciousness that returns a reflection suffix for task execution."""

    def __init__(self, suffix_data: dict | None = None):
        self._suffix_data = suffix_data

    async def think(self, agent_id, context, prompt, **kwargs):
        if "plan" in prompt.lower() or "checklist" in prompt.lower():
            return ConsciousResponse(
                content=(
                    "# Plan\n\n"
                    "- [ ] Do the thing\n"
                ),
                tokens_in=100,
                tokens_out=50,
                model="mock",
            )

        if "adjustment" in prompt.lower() or "updated plan" in prompt.lower():
            return ConsciousResponse(
                content="# Updated Plan\n\n- [ ] Retry\n",
                tokens_in=100,
                tokens_out=50,
                model="mock",
            )

        # Task execution — include reflection suffix if configured
        content = f"[{agent_id}] Completed task successfully."
        if self._suffix_data is not None:
            content += REFLECTION_DELIMITER + json.dumps(self._suffix_data)

        return ConsciousResponse(
            content=content,
            tokens_in=100,
            tokens_out=50,
            model="mock",
        )

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(
            content=f"# {agent_id}\n\nCompleted a productive day.",
            reflection="Today went well.",
            tokens_in=200,
            tokens_out=100,
            model="mock",
        )


class TestFabricReflectionIntegration:
    def _make_fabric(
        self, tmp_path: Path, suffix_data: dict | None = None
    ) -> Fabric:
        return Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=MockConsciousnessWithReflection(suffix_data),
        )

    @pytest.mark.asyncio
    async def test_outcome_extracted_from_suffix(self, tmp_path: Path) -> None:
        suffix = {"outcome": "Filed quarterly report"}
        fabric = self._make_fabric(tmp_path, suffix_data=suffix)
        fabric.register_agent("worker-01")
        agent = await fabric.wake("worker-01")

        await fabric.cycle("worker-01")

        assert agent.task_queue is not None
        done_tasks = [t for t in agent.task_queue.tasks if t.status == "done"]
        assert len(done_tasks) == 1
        assert done_tasks[0].outcome == "Filed quarterly report"

    @pytest.mark.asyncio
    async def test_learning_stored_as_memory(self, tmp_path: Path) -> None:
        suffix = {"learned": "Always validate CSV encoding"}
        fabric = self._make_fabric(tmp_path, suffix_data=suffix)
        fabric.register_agent("worker-01")
        await fabric.wake("worker-01")

        await fabric.cycle("worker-01")

        memories = await fabric.memory.recall("worker-01", limit=50)
        learning_memories = [
            m for m in memories
            if "learning" in m.tags and "reflection" in m.tags
        ]
        assert len(learning_memories) == 1
        assert learning_memories[0].content == "Always validate CSV encoding"
        assert learning_memories[0].importance == 8.0

    @pytest.mark.asyncio
    async def test_procedure_appended(self, tmp_path: Path) -> None:
        suffix = {"procedure_update": "- Step 9: Verify BOM header"}
        fabric = self._make_fabric(tmp_path, suffix_data=suffix)
        fabric.register_agent("worker-01")
        agent = await fabric.wake("worker-01")

        await fabric.cycle("worker-01")

        procedures = agent.read_identity("procedures")
        assert "- Step 9: Verify BOM header" in procedures

    @pytest.mark.asyncio
    async def test_messages_sent_via_channel(self, tmp_path: Path) -> None:
        suffix = {"messages": [{"to": "manager-01", "content": "Report ready"}]}
        fabric = self._make_fabric(tmp_path, suffix_data=suffix)

        # Attach a mock channel
        mock_channel = AsyncMock()
        mock_channel.receive = AsyncMock(return_value=[])
        fabric.channel = mock_channel

        fabric.register_agent("worker-01")
        await fabric.wake("worker-01")
        await fabric.cycle("worker-01")

        mock_channel.send.assert_called_once_with(
            sender="worker-01",
            recipient="manager-01",
            content="Report ready",
        )

    @pytest.mark.asyncio
    async def test_backward_compat_without_suffix(self, tmp_path: Path) -> None:
        """When no reflection suffix is present, everything works as before."""
        fabric = self._make_fabric(tmp_path, suffix_data=None)
        fabric.register_agent("worker-01")
        agent = await fabric.wake("worker-01")

        result = await fabric.cycle("worker-01")
        assert result["action"] == "executed_task"

        assert agent.task_queue is not None
        done_tasks = [t for t in agent.task_queue.tasks if t.status == "done"]
        assert len(done_tasks) == 1
        assert "[worker-01] Completed task successfully." in done_tasks[0].outcome
