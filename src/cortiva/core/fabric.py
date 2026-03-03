"""
Cortiva Fabric — the organisational nervous system.

The Fabric is the runtime that manages all agents. It holds references
to the pluggable adapters (memory, consciousness, routine, channel),
manages agent lifecycles, runs the heartbeat, and orchestrates the
plan-execute-replan cycle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from cortiva.adapters.protocols import (
    ChannelAdapter,
    ConsciousnessAdapter,
    MemoryAdapter,
    Priority,
    RoutineAdapter,
)
from cortiva.core.agent import Agent, AgentState

logger = logging.getLogger("cortiva.fabric")


class Fabric:
    """
    The organisational fabric. Manages agents and connects them
    to the cognitive layers via pluggable adapters.
    """

    def __init__(
        self,
        agents_dir: Path,
        memory: MemoryAdapter,
        consciousness: ConsciousnessAdapter,
        routine: RoutineAdapter | None = None,
        channel: ChannelAdapter | None = None,
        heartbeat_interval: float = 30.0,
        daily_consciousness_limit: int = 1000,
    ):
        self.agents_dir = Path(agents_dir)
        self.agents_dir.mkdir(parents=True, exist_ok=True)

        # Pluggable adapters
        self.memory = memory
        self.consciousness = consciousness
        self.routine = routine
        self.channel = channel

        # Runtime state
        self.agents: dict[str, Agent] = {}
        self.heartbeat_interval = heartbeat_interval
        self.daily_consciousness_limit = daily_consciousness_limit
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None

    # ----- Agent management -----

    def discover_agents(self) -> list[str]:
        """Scan agents directory and register any found."""
        discovered = []
        if not self.agents_dir.exists():
            return discovered

        for path in sorted(self.agents_dir.iterdir()):
            if path.is_dir() and not path.name.startswith("."):
                if path.name not in self.agents:
                    agent = Agent.from_directory(path)
                    agent.consciousness_budget_limit = (
                        self.daily_consciousness_limit // max(len(self.agents) + 1, 1)
                    )
                    self.agents[agent.id] = agent
                    discovered.append(agent.id)
                    logger.info(f"Discovered agent: {agent.id}")

        return discovered

    def register_agent(
        self,
        agent_id: str,
        *,
        consciousness_budget: int | None = None,
    ) -> Agent:
        """Register a new agent. Creates directory and skeleton files."""
        agent_dir = self.agents_dir / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)

        agent = Agent(
            id=agent_id,
            directory=agent_dir,
            state=AgentState.ONBOARDING,
            consciousness_budget_limit=consciousness_budget or 50,
        )

        # Write skeleton identity files if they don't exist
        if not agent.identity_path("identity").exists():
            agent.write_identity("identity", f"# {agent_id}\n\nNewly created agent. No experiences yet.\n")
        if not agent.identity_path("soul").exists():
            agent.write_identity("soul", f"# {agent_id} — Persona\n\nDefault persona. Configure disposition parameters.\n")
        if not agent.identity_path("skills").exists():
            agent.write_identity("skills", f"# {agent_id} — Skills\n\nNo skills defined yet.\n")
        if not agent.identity_path("responsibilities").exists():
            agent.write_identity("responsibilities", f"# {agent_id} — Responsibilities\n\n## Primary\n\n## Secondary\n\n## Escalation\n")
        if not agent.identity_path("procedures").exists():
            agent.write_identity("procedures", f"# {agent_id} — Procedures\n\nNo procedures promoted yet.\n")
        if not agent.identity_path("plan").exists():
            agent.write_identity("plan", f"# {agent_id} — Plan\n\nNo plan yet. Awaiting first wake cycle.\n")

        agent.transition(AgentState.SLEEPING)
        self.agents[agent_id] = agent
        logger.info(f"Registered agent: {agent_id}")
        return agent

    def get_agent(self, agent_id: str) -> Agent:
        """Get an agent by ID."""
        if agent_id not in self.agents:
            raise KeyError(f"Unknown agent: {agent_id}")
        return self.agents[agent_id]

    # ----- Lifecycle operations -----

    async def wake(self, agent_id: str) -> Agent:
        """Wake an agent. Loads identity and begins planning."""
        agent = self.get_agent(agent_id)
        agent.transition(AgentState.WAKING)
        logger.info(f"Waking agent: {agent_id}")

        # Load identity files
        identity = agent.read_all_identity()

        # Check for pending messages
        messages = []
        if self.channel:
            messages = await self.channel.receive(agent_id)

        # Compute familiarity with pending work (if any)
        # For now, transition to planning
        agent.transition(AgentState.PLANNING)

        # Ask the conscious layer to build a plan
        context = self._build_wake_context(agent, identity, messages)
        if agent.spend_consciousness():
            response = await self.consciousness.think(
                agent_id=agent_id,
                context=context,
                prompt="You are waking up. Review your identity, any pending messages, and your previous plan. Create your plan for today.",
                priority=Priority.NORMAL,
            )
            agent.write_identity("plan", response.content)
            logger.info(f"Agent {agent_id} has planned their day")

        agent.transition(AgentState.EXECUTING)
        return agent

    async def sleep(self, agent_id: str) -> Agent:
        """Put an agent to sleep after reflection."""
        agent = self.get_agent(agent_id)

        if agent.state == AgentState.EXECUTING:
            agent.transition(AgentState.REFLECTING)
        elif agent.state == AgentState.REPLANNING:
            agent.transition(AgentState.REFLECTING)

        if agent.state == AgentState.REFLECTING:
            # End-of-day reflection
            identity = agent.read_all_identity()
            day_summary = self._build_day_summary(agent)

            if agent.spend_consciousness():
                response = await self.consciousness.reflect(
                    agent_id=agent_id,
                    context=self._identity_to_context(identity),
                    day_summary=day_summary,
                )

                # Update Living Summary with reflection output
                if response.content:
                    agent.write_identity("identity", response.content)

                # Write journal entry
                journal_content = response.reflection or response.content
                journal_path = agent.journal_path()
                journal_path.write_text(journal_content, encoding="utf-8")
                logger.info(f"Agent {agent_id} reflected and updated identity")

        agent.transition(AgentState.SLEEPING)
        logger.info(f"Agent {agent_id} is now sleeping")
        return agent

    # ----- The Cycle -----

    async def cycle(self, agent_id: str) -> dict[str, Any]:
        """
        Run one plan-execute-replan iteration for an agent.

        This is the core loop. The subconscious checks what's next,
        assesses familiarity, and either handles it procedurally
        or escalates to the conscious layer.
        """
        agent = self.get_agent(agent_id)

        if agent.state not in (AgentState.EXECUTING, AgentState.REPLANNING):
            raise ValueError(f"Agent {agent_id} not in executable state: {agent.state.value}")

        result = {
            "agent_id": agent_id,
            "action": "idle",
            "conscious_call": False,
            "task": None,
        }

        # Check for messages
        messages = []
        if self.channel:
            messages = await self.channel.receive(agent_id)

        # Check for tasks in queue (placeholder — will be expanded)
        # For now, if there are messages, process them
        if messages:
            identity = agent.read_all_identity()
            context = self._build_execution_context(agent, identity, messages)

            if agent.spend_consciousness():
                response = await self.consciousness.think(
                    agent_id=agent_id,
                    context=context,
                    prompt="You have new messages. Read them, decide what to do, and act.",
                    priority=Priority.NORMAL,
                )

                # Store the interaction as a memory
                await self.memory.store(
                    agent_id=agent_id,
                    content=f"Processed {len(messages)} messages. Outcome: {response.content[:200]}",
                    tags=["cycle", "messages"],
                    importance=5.0,
                )

                result["action"] = "processed_messages"
                result["conscious_call"] = True
                agent.tasks_completed_today += 1

        return result

    # ----- Heartbeat -----

    async def heartbeat(self) -> None:
        """
        Check all agents. Wake any that have scheduled work.
        Run cycles for active agents.
        """
        for agent_id, agent in self.agents.items():
            if agent.state == AgentState.EXECUTING:
                try:
                    await self.cycle(agent_id)
                except Exception as e:
                    logger.error(f"Cycle error for {agent_id}: {e}")

    async def _heartbeat_loop(self) -> None:
        """Continuous heartbeat loop."""
        while self._running:
            try:
                await self.heartbeat()
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(self.heartbeat_interval)

    async def start(self) -> None:
        """Start the fabric. Discover agents and begin heartbeat."""
        logger.info("Starting Cortiva fabric")
        self.discover_agents()
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"Fabric running with {len(self.agents)} agents")

    async def stop(self) -> None:
        """Stop the fabric. Sleep all active agents."""
        logger.info("Stopping Cortiva fabric")
        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Sleep all active agents
        for agent_id, agent in self.agents.items():
            if agent.state not in (AgentState.SLEEPING, AgentState.ONBOARDING):
                try:
                    await self.sleep(agent_id)
                except Exception as e:
                    logger.error(f"Error sleeping {agent_id}: {e}")

        logger.info("Fabric stopped")

    # ----- Status -----

    def status(self) -> dict[str, Any]:
        """Get current fabric status."""
        total_consciousness = sum(a.consciousness_budget_used for a in self.agents.values())
        return {
            "agents": {
                aid: {
                    "state": a.state.value,
                    "consciousness_used": a.consciousness_budget_used,
                    "consciousness_remaining": a.consciousness_remaining,
                    "tasks_today": a.tasks_completed_today,
                    "last_wake": a.last_wake.isoformat() if a.last_wake else None,
                }
                for aid, a in self.agents.items()
            },
            "total_consciousness_used": total_consciousness,
            "total_consciousness_limit": self.daily_consciousness_limit,
            "running": self._running,
        }

    # ----- Context builders (private) -----

    def _identity_to_context(self, identity: dict[str, str]) -> str:
        sections = []
        for key, content in identity.items():
            if content.strip():
                sections.append(f"## {key.title()}\n\n{content}")
        return "\n\n---\n\n".join(sections)

    def _build_wake_context(
        self,
        agent: Agent,
        identity: dict[str, str],
        messages: list,
    ) -> str:
        parts = [self._identity_to_context(identity)]
        if messages:
            msg_text = "\n".join(f"- [{m.sender}]: {m.content}" for m in messages)
            parts.append(f"## Pending Messages\n\n{msg_text}")
        parts.append(f"## Date\n\n{datetime.utcnow().strftime('%A, %Y-%m-%d')}")
        return "\n\n---\n\n".join(parts)

    def _build_execution_context(
        self,
        agent: Agent,
        identity: dict[str, str],
        messages: list,
    ) -> str:
        parts = [self._identity_to_context(identity)]
        if messages:
            msg_text = "\n".join(f"- [{m.sender}]: {m.content}" for m in messages)
            parts.append(f"## Messages\n\n{msg_text}")
        return "\n\n---\n\n".join(parts)

    def _build_day_summary(self, agent: Agent) -> str:
        return (
            f"Tasks completed: {agent.tasks_completed_today}\n"
            f"Tasks escalated: {agent.tasks_escalated_today}\n"
            f"Consciousness budget used: {agent.consciousness_budget_used}/{agent.consciousness_budget_limit}\n"
        )
