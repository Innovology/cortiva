"""
Basic Agent Lifecycle Example
=============================

Demonstrates the core Cortiva agent lifecycle:
  discover -> wake -> cycle -> sleep

Run with:
    PYTHONPATH=src python3 examples/basic_lifecycle.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse, Priority
from cortiva.core.fabric import Fabric


# ---------------------------------------------------------------------------
# Step 1: Create a mock ConsciousnessAdapter
# ---------------------------------------------------------------------------
# In production you would use an adapter backed by Claude, GPT, etc.
# For testing and demos we return canned responses so no API key is needed.


class MockConsciousness:
    """Minimal consciousness adapter that returns static responses."""

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
        call_type = (metadata or {}).get("call_type", "")

        if call_type == "plan":
            # Return a simple checklist so the fabric can parse tasks.
            return ConsciousResponse(
                content=(
                    "# Today's Plan\n\n"
                    "- [ ] Review project requirements\n"
                    "- [ ] Draft initial design document\n"
                    "- [ ] Send status update to the team\n"
                ),
                tokens_in=50,
                tokens_out=30,
                model="mock",
            )

        # Default: treat the prompt as a task and acknowledge it.
        return ConsciousResponse(
            content=f"Completed: {prompt[:80]}",
            tokens_in=40,
            tokens_out=20,
            model="mock",
        )

    async def reflect(
        self,
        agent_id: str,
        context: str,
        day_summary: str,
    ) -> ConsciousResponse:
        return ConsciousResponse(
            content=(
                f"# {agent_id} — End of Day Reflection\n\n"
                "Today was productive. Completed all planned tasks.\n"
            ),
            tokens_in=60,
            tokens_out=40,
            model="mock",
        )


# ---------------------------------------------------------------------------
# Step 2: Bootstrap an agent directory with minimal identity files
# ---------------------------------------------------------------------------


def bootstrap_agent(agents_dir: Path, agent_id: str) -> Path:
    """Create an agent directory with the minimum required identity files."""
    agent_dir = agents_dir / agent_id
    identity_dir = agent_dir / "identity"
    today_dir = agent_dir / "today"
    identity_dir.mkdir(parents=True, exist_ok=True)
    today_dir.mkdir(parents=True, exist_ok=True)

    (identity_dir / "identity.md").write_text(
        f"# {agent_id}\n\nA demo agent exploring the Cortiva lifecycle.\n"
    )
    (identity_dir / "soul.md").write_text(
        f"# {agent_id} — Soul\n\nCurious, methodical, concise.\n"
    )
    (today_dir / "plan.md").write_text(
        f"# {agent_id} — Plan\n\nNo plan yet. Awaiting first wake cycle.\n"
    )
    return agent_dir


# ---------------------------------------------------------------------------
# Step 3: Run the full lifecycle
# ---------------------------------------------------------------------------


async def main() -> None:
    # Create a temporary directory so the example is self-contained.
    with tempfile.TemporaryDirectory(prefix="cortiva_demo_") as tmp:
        agents_dir = Path(tmp) / "agents"
        agents_dir.mkdir()

        # Prepare the agent on disk.
        agent_id = "demo-agent"
        bootstrap_agent(agents_dir, agent_id)

        # Build adapters.
        memory = InMemoryAdapter()
        consciousness = MockConsciousness()

        # Create the Fabric — the runtime that manages agents.
        fabric = Fabric(
            agents_dir=agents_dir,
            memory=memory,
            consciousness=consciousness,  # type: ignore[arg-type]
        )

        # Discover agents on disk.
        discovered = fabric.discover_agents()
        print(f"Discovered agents: {discovered}")

        agent = fabric.get_agent(agent_id)
        print(f"Agent state after discovery: {agent.state.value}")

        # Wake the agent — loads identity, builds a plan, enters EXECUTING.
        await fabric.wake(agent_id)
        print(f"Agent state after wake: {agent.state.value}")
        print(f"Tasks in queue: {len(agent.task_queue.tasks)}")  # type: ignore[union-attr]

        # Run one cycle — executes the next pending task.
        result = await fabric.cycle(agent_id)
        print(f"Cycle result: action={result['action']}, task={result['task']}")
        print(f"Agent consciousness budget used: {agent.consciousness_budget_used}")

        # Sleep the agent — triggers reflection, writes journal, returns to SLEEPING.
        await fabric.sleep(agent_id)
        print(f"Agent state after sleep: {agent.state.value}")

        print("\nLifecycle complete.")


if __name__ == "__main__":
    asyncio.run(main())
