"""Shared fixtures for integration tests.

Run with: pytest tests/integration/ -m integration
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse, Priority
from cortiva.core.agent import Agent, WORKSPACE_DIRS
from cortiva.core.fabric import Fabric


class MockConsciousness:
    """Deterministic consciousness adapter for integration tests.

    Returns canned responses that the fabric can parse:
    - Planning: returns a numbered task list
    - Executing: returns task output with reflection suffix
    - Reflecting: returns updated identity + journal entry
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append({
            "method": "think",
            "agent_id": agent_id,
            "prompt_preview": prompt[:200],
            "metadata": metadata,
        })

        call_type = (metadata or {}).get("call_type", "")

        if call_type == "plan":
            content = (
                "# Plan\n\n"
                "1. Review incoming data\n"
                "2. Process records\n"
                "3. Generate summary report\n"
            )
        elif call_type == "replan":
            content = (
                "# Revised Plan\n\n"
                "1. Retry failed task with adjusted approach\n"
                "2. Complete remaining work\n"
            )
        else:
            # Task execution
            content = (
                "I have completed the task successfully. "
                "The records were processed and validated.\n\n"
                "---REFLECTION---\n"
                '{"outcome": "Processed records successfully", '
                '"learned": "Batch processing is more efficient"}'
            )

        return ConsciousResponse(
            content=content,
            tokens_in=100,
            tokens_out=50,
            model="mock-model",
            metadata={"agent_id": agent_id},
        )

    async def reflect(
        self,
        agent_id: str,
        context: str,
        day_summary: str,
    ) -> ConsciousResponse:
        self.calls.append({
            "method": "reflect",
            "agent_id": agent_id,
        })
        return ConsciousResponse(
            content=(
                f"I am {agent_id}. Today I processed records and learned "
                "that batch operations are more efficient. I feel more "
                "confident in my abilities."
            ),
            reflection=(
                "Good day overall. Completed all tasks without escalation. "
                "Key learning: batch processing."
            ),
            tokens_in=200,
            tokens_out=100,
            model="mock-model",
        )


class MockChannel:
    """Mock channel adapter that records messages."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, sender: str, recipient: str, content: str, **kwargs: Any) -> Any:
        from cortiva.adapters.protocols import Message
        msg = Message(
            id=f"msg-{len(self.messages)}",
            sender=sender,
            recipient=recipient,
            content=content,
        )
        self.messages.append({"sender": sender, "recipient": recipient, "content": content})
        return msg

    async def receive(self, agent_id: str, **kwargs: Any) -> list:
        return []

    async def listen(self, agent_id: str, channels: list[str]) -> None:
        pass


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    """Create a temporary agents directory."""
    d = tmp_path / "agents"
    d.mkdir()
    return d


@pytest.fixture
def mock_consciousness() -> MockConsciousness:
    return MockConsciousness()


@pytest.fixture
def mock_channel() -> MockChannel:
    return MockChannel()


@pytest.fixture
def memory() -> InMemoryAdapter:
    return InMemoryAdapter()


def create_test_agent(agents_dir: Path, agent_id: str = "test-agent-01") -> Agent:
    """Create a fully populated test agent on disk."""
    agent_dir = agents_dir / agent_id
    agent_dir.mkdir()
    for subdir in WORKSPACE_DIRS:
        (agent_dir / subdir).mkdir()

    (agent_dir / "identity" / "identity.md").write_text(
        f"# {agent_id}\n\n"
        "I am an integration test agent. I process records and generate reports.\n"
    )
    (agent_dir / "identity" / "soul.md").write_text(
        f"# {agent_id} — Persona\n\n"
        "Methodical, detail-oriented, reliable.\n"
    )
    (agent_dir / "identity" / "skills.md").write_text(
        f"# {agent_id} — Skills\n\n"
        "- Data processing\n"
        "- Report generation\n"
    )
    (agent_dir / "identity" / "responsibilities.md").write_text(
        f"# {agent_id} — Responsibilities\n\n"
        "## Primary\n\n"
        "Process incoming records and generate daily summaries.\n"
    )
    (agent_dir / "identity" / "procedures.md").write_text(
        f"# {agent_id} — Procedures\n\n"
        "## Record Processing\n\n"
        "1. Read incoming data from workspace/\n"
        "2. Validate each record\n"
        "3. Write summary to outbox/\n"
    )

    return Agent(id=agent_id, directory=agent_dir)


@pytest.fixture
def fabric(
    agents_dir: Path,
    mock_consciousness: MockConsciousness,
    memory: InMemoryAdapter,
    mock_channel: MockChannel,
) -> Fabric:
    """Create a Fabric with mock adapters for integration testing."""
    f = Fabric(
        agents_dir=agents_dir,
        memory=memory,
        consciousness=mock_consciousness,
        channel=mock_channel,
    )
    return f
