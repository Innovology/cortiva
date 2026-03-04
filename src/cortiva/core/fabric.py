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
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from cortiva.adapters.protocols import (
    ChannelAdapter,
    ConsciousnessAdapter,
    MemoryAdapter,
    Priority,
    RoutineAdapter,
    TerminalAgentAdapter,
)
from cortiva.core.agent import Agent, AgentState, Task, TaskQueue
from cortiva.core.budget import ConsciousnessBudgetManager
from cortiva.core.reflection import ReflectionSuffix, parse_reflection_suffix

logger = logging.getLogger("cortiva.fabric")

# How many exceptions before a replan is triggered
EXCEPTION_THRESHOLD = 3

# Maximum number of replans per wake cycle
MAX_REPLANS = 3


def _parse_plan(plan_text: str) -> TaskQueue:
    """Parse plan markdown into a TaskQueue.

    Recognises checkbox lists (``- [ ]`` / ``- [x]``), numbered lists
    (``1.``), and plain bullet lists (``- ``).  Priority markers like
    ``**[CRITICAL]**`` and ``**[HIGH]**`` are extracted.
    """
    tasks: list[Task] = []
    task_id = 0

    for line in plan_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Match checkbox, numbered, or bullet list items
        # Checkbox: - [ ] or - [x] or - [X] or * [ ] etc.
        checkbox_match = re.match(
            r"^[-*]\s*\[([ xX])\]\s*(.*)", stripped
        )
        # Numbered: 1. or 1)
        numbered_match = re.match(r"^\d+[.)]\s+(.*)", stripped)
        # Plain bullet: - or *
        bullet_match = re.match(r"^[-*]\s+(.*)", stripped)

        description: str | None = None
        done = False

        if checkbox_match:
            done = checkbox_match.group(1).lower() == "x"
            description = checkbox_match.group(2).strip()
        elif numbered_match:
            description = numbered_match.group(1).strip()
        elif bullet_match:
            # Avoid matching header-like lines (e.g. "# Heading")
            candidate = bullet_match.group(1).strip()
            if candidate and not candidate.startswith("#"):
                description = candidate

        if not description:
            continue

        # Extract priority markers
        priority = 0
        priority_pattern = r"\*\*\[(\w+)\]\*\*\s*"
        priority_match = re.search(priority_pattern, description)
        if priority_match:
            marker = priority_match.group(1).upper()
            if marker == "CRITICAL":
                priority = 2
            elif marker == "HIGH":
                priority = 1
            description = re.sub(priority_pattern, "", description).strip()

        if not description:
            continue

        task_id += 1
        tasks.append(Task(
            id=f"task-{task_id}",
            description=description,
            status="done" if done else "pending",
            priority=priority,
        ))

    return TaskQueue(tasks=tasks)


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
        terminal: TerminalAgentAdapter | None = None,
        heartbeat_interval: float = 30.0,
        daily_consciousness_limit: int = 1000,
        budget_manager: ConsciousnessBudgetManager | None = None,
    ):
        self.agents_dir = Path(agents_dir)
        self.agents_dir.mkdir(parents=True, exist_ok=True)

        # Pluggable adapters
        self.memory = memory
        self.consciousness = consciousness
        self.routine = routine
        self.channel = channel
        self.terminal = terminal
        self.budget_manager = budget_manager

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
                    agent.migrate_flat_layout()
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
        agent.ensure_workspace()

        # Write skeleton identity files if they don't exist
        if not agent.identity_path("identity").exists():
            agent.write_identity(
                "identity",
                f"# {agent_id}\n\nNewly created agent. No experiences yet.\n",
            )
        if not agent.identity_path("soul").exists():
            agent.write_identity(
                "soul",
                f"# {agent_id} — Persona\n\nDefault persona. "
                "Configure disposition parameters.\n",
            )
        if not agent.identity_path("skills").exists():
            agent.write_identity(
                "skills",
                f"# {agent_id} — Skills\n\nNo skills defined yet.\n",
            )
        if not agent.identity_path("responsibilities").exists():
            agent.write_identity(
                "responsibilities",
                f"# {agent_id} — Responsibilities\n\n"
                "## Primary\n\n## Secondary\n\n## Escalation\n",
            )
        if not agent.identity_path("procedures").exists():
            agent.write_identity(
                "procedures",
                f"# {agent_id} — Procedures\n\n"
                "No procedures promoted yet.\n",
            )
        if not agent.identity_path("plan").exists():
            agent.write_identity(
                "plan",
                f"# {agent_id} — Plan\n\n"
                "No plan yet. Awaiting first wake cycle.\n",
            )

        agent.transition(AgentState.SLEEPING)
        self.agents[agent_id] = agent
        if self.budget_manager:
            self.budget_manager.register_agent(agent_id)
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

        # Migrate flat layout if needed, then load identity
        agent.migrate_flat_layout()
        identity = agent.read_all_identity()

        # Check for pending messages
        messages = []
        if self.channel:
            messages = await self.channel.receive(agent_id)

        agent.transition(AgentState.PLANNING)

        if self.budget_manager:
            self.budget_manager.reset_agent(agent_id)

        # Ask the conscious layer to build a plan (structured checklist)
        context = self._build_wake_context(agent, identity, messages)
        planning_prompt = (
            "You are waking up. Review your identity, any pending messages, "
            "and your previous plan. Create your plan for today as a structured "
            "checklist. Use this format:\n\n"
            "- [ ] **[CRITICAL]** Task description (for critical tasks)\n"
            "- [ ] **[HIGH]** Task description (for high-priority tasks)\n"
            "- [ ] Task description (for normal tasks)\n"
        )

        if self.budget_manager:
            approval = self.budget_manager.request_budget(agent_id, "normal")
            if approval.approved:
                response = await self.consciousness.think(
                    agent_id=agent_id,
                    context=context,
                    prompt=planning_prompt,
                    priority=Priority.NORMAL,
                )
                self.budget_manager.record_usage(
                    agent_id, approval.backend, response.tokens_in, response.tokens_out,
                )
                agent.spend_consciousness()
                agent.write_identity("plan", response.content)
                agent.task_queue = _parse_plan(response.content)
                logger.info(f"Agent {agent_id} has planned their day")
        elif agent.spend_consciousness():
            response = await self.consciousness.think(
                agent_id=agent_id,
                context=context,
                prompt=planning_prompt,
                priority=Priority.NORMAL,
            )
            agent.write_identity("plan", response.content)
            agent.task_queue = _parse_plan(response.content)
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
            # Persist final plan state
            self._write_plan(agent)

            # End-of-day reflection
            identity = agent.read_all_identity()
            day_summary = self._build_day_summary(agent)

            can_reflect = False
            approval = None
            if self.budget_manager:
                approval = self.budget_manager.request_budget(agent_id, "normal")
                can_reflect = approval.approved
            else:
                can_reflect = agent.spend_consciousness()

            if can_reflect:
                response = await self.consciousness.reflect(
                    agent_id=agent_id,
                    context=self._identity_to_context(identity),
                    day_summary=day_summary,
                )

                if self.budget_manager and approval and approval.backend:
                    self.budget_manager.record_usage(
                        agent_id, approval.backend,
                        response.tokens_in, response.tokens_out,
                    )
                    agent.spend_consciousness()

                # Update Living Summary with reflection output
                if response.content:
                    agent.write_identity("identity", response.content)

                # Write journal entry
                journal_content = response.reflection or response.content
                journal_path = agent.journal_path()
                journal_path.write_text(journal_content, encoding="utf-8")
                logger.info(f"Agent {agent_id} reflected and updated identity")

        agent.task_queue = None
        agent.transition(AgentState.SLEEPING)
        logger.info(f"Agent {agent_id} is now sleeping")
        return agent

    # ----- The Cycle -----

    async def cycle(self, agent_id: str) -> dict[str, Any]:
        """
        Run one plan-execute-replan iteration for an agent.

        Processes one task per call to keep the heartbeat responsive.
        """
        agent = self.get_agent(agent_id)

        if agent.state not in (AgentState.EXECUTING, AgentState.REPLANNING):
            raise ValueError(f"Agent {agent_id} not in executable state: {agent.state.value}")

        result: dict[str, Any] = {
            "agent_id": agent_id,
            "action": "idle",
            "conscious_call": False,
            "task": None,
            "all_tasks_complete": False,
        }

        # Load task_queue from plan.md if not yet parsed
        if agent.task_queue is None:
            plan_text = agent.read_identity("plan")
            agent.task_queue = _parse_plan(plan_text)

        # Check for messages
        messages: list[Any] = []
        if self.channel:
            messages = await self.channel.receive(agent_id)

        # Check replan triggers
        if self._should_replan(agent, messages):
            await self._replan(agent, messages)
            result["action"] = "replanned"
            result["conscious_call"] = True
            return result

        # Get next pending task
        task = agent.task_queue.next_pending()
        if task is None:
            result["action"] = "idle"
            result["all_tasks_complete"] = agent.task_queue.all_done()
            return result

        # Execute the task
        await self._execute_task(agent, task, messages)
        result["action"] = "executed_task"
        result["task"] = task.description
        result["conscious_call"] = True

        # Write updated plan to disk
        self._write_plan(agent)

        return result

    async def _execute_task(
        self, agent: Agent, task: Task, messages: list[Any]
    ) -> None:
        """Execute a single task via routine or consciousness."""
        task.status = "in_progress"

        if self.budget_manager:
            self.budget_manager.record_task_attempt(agent.id)

        if self.routine:
            # Ask the routine layer whether this can be handled procedurally
            from cortiva.adapters.protocols import FamiliaritySignal

            familiarity = FamiliaritySignal(
                strength="novel", valence="neutral",
                match_count=0, text="No prior experience.",
            )
            assessment = await self.routine.assess(
                agent_id=agent.id,
                task_description=task.description,
                procedural_index=agent.read_identity("procedures"),
                familiarity=familiarity,
            )
            action = assessment.get("action", "escalate")

            if action == "defer":
                task.status = "exception"
                task.error = "Routine deferred task"
                assert agent.task_queue is not None
                agent.task_queue.exceptions.append(task)
                agent.tasks_escalated_today += 1
                return
            elif action == "procedural":
                task.status = "done"
                task.outcome = assessment.get("result", "Completed procedurally")
                agent.tasks_completed_today += 1
                return
            # else: escalate — fall through to consciousness

        # Consciousness execution (budget-permitting)
        task_priority = (
            "critical" if task.priority >= 2
            else "high" if task.priority >= 1
            else "normal"
        )

        can_execute = False
        approval = None
        if self.budget_manager:
            approval = self.budget_manager.request_budget(agent.id, task_priority)
            can_execute = approval.approved
        else:
            can_execute = agent.spend_consciousness()

        if not can_execute:
            task.status = "exception"
            task.error = "Budget exhausted"
            assert agent.task_queue is not None
            agent.task_queue.exceptions.append(task)
            agent.tasks_escalated_today += 1
            logger.info(f"Agent {agent.id}: budget exhausted, deferring '{task.description}'")
            return

        identity = agent.read_all_identity()
        context = self._build_execution_context(agent, identity, messages)
        prompt = (
            f"Execute this task: {task.description}\n\n"
            "Describe what you did and the outcome."
        )

        response = await self.consciousness.think(
            agent_id=agent.id,
            context=context,
            prompt=prompt,
            priority=Priority.HIGH if task.priority >= 1 else Priority.NORMAL,
            metadata={"task_execution": True},
        )

        if self.budget_manager and approval and approval.backend:
            self.budget_manager.record_usage(
                agent.id, approval.backend,
                response.tokens_in, response.tokens_out,
            )
            agent.spend_consciousness()

        # Parse reflection suffix from response
        reflection = parse_reflection_suffix(response.content)

        task.status = "done"
        if reflection.suffix and reflection.suffix.outcome:
            task.outcome = reflection.suffix.outcome
        else:
            task.outcome = reflection.clean_content
        agent.tasks_completed_today += 1

        # Process structured reflection metadata if present
        if reflection.suffix:
            await self._process_reflection(agent, task, reflection.suffix)

        # Store as memory
        await self.memory.store(
            agent_id=agent.id,
            content=f"Task: {task.description}. Outcome: {task.outcome[:200]}",
            tags=["cycle", "task"],
            importance=5.0 + task.priority,
        )

    async def _process_reflection(
        self, agent: Agent, task: Task, suffix: ReflectionSuffix
    ) -> None:
        """Process structured reflection metadata from a task execution."""
        # Store learning as high-importance memory
        if suffix.learned:
            await self.memory.store(
                agent_id=agent.id,
                content=suffix.learned,
                tags=["learning", "reflection"],
                importance=8.0,
            )

        # Log prediction error
        if suffix.prediction_error:
            logger.info(
                f"Agent {agent.id} prediction error on '{task.description}': "
                f"{suffix.prediction_error}"
            )

        # Append procedure update to procedures.md
        if suffix.procedure_update:
            current = agent.read_identity("procedures")
            updated = current.rstrip() + "\n\n" + suffix.procedure_update + "\n"
            agent.write_identity("procedures", updated)

        # Send inter-agent messages via channel adapter
        if suffix.messages and self.channel:
            for msg in suffix.messages:
                recipient = msg.get("to", "")
                content = msg.get("content", "")
                if recipient and content:
                    await self.channel.send(
                        sender=agent.id,
                        recipient=recipient,
                        content=content,
                    )

        # Log escalation request
        if suffix.escalation:
            logger.warning(
                f"Agent {agent.id} escalation on '{task.description}': "
                f"{suffix.escalation}"
            )

    def _should_replan(self, agent: Agent, messages: list[Any]) -> bool:
        """Check whether a replan is warranted."""
        if agent.task_queue is None:
            return False
        if agent.task_queue.replan_count >= MAX_REPLANS:
            return False

        # Trigger 1: too many exceptions
        if len(agent.task_queue.exceptions) >= EXCEPTION_THRESHOLD:
            return True

        # Trigger 2: urgent message
        for msg in messages:
            content = getattr(msg, "content", "")
            if "urgent" in content.lower():
                return True

        # Trigger 3: all pending done but exceptions remain
        pending = [t for t in agent.task_queue.tasks if t.status == "pending"]
        if not pending and agent.task_queue.exceptions:
            return True

        return False

    async def _replan(self, agent: Agent, messages: list[Any]) -> None:
        """Trigger a replan: EXECUTING -> REPLANNING, build new plan, -> EXECUTING."""
        assert agent.task_queue is not None

        if agent.state == AgentState.EXECUTING:
            agent.transition(AgentState.REPLANNING)

        identity = agent.read_all_identity()
        summary = agent.task_queue.completion_summary()
        completed = [t for t in agent.task_queue.tasks if t.status == "done"]
        exceptions = agent.task_queue.exceptions

        context = self._build_execution_context(agent, identity, messages)
        completed_str = (
            ", ".join(t.description for t in completed) or "none"
        )
        exception_str = (
            ", ".join(
                f"{t.description} ({t.error})" for t in exceptions
            )
            or "none"
        )
        context += (
            "\n\n---\n\n## Replan Context\n\n"
            f"Completed: {completed_str}\n"
            f"Exceptions: {exception_str}\n"
            f"Summary: {summary}\n"
        )

        can_replan = False
        approval = None
        if self.budget_manager:
            approval = self.budget_manager.request_budget(agent.id, "high")
            can_replan = approval.approved
        else:
            can_replan = agent.spend_consciousness()

        if can_replan:
            response = await self.consciousness.think(
                agent_id=agent.id,
                context=context,
                prompt=(
                    "Your plan needs adjustment. Review completed tasks and exceptions above. "
                    "Create an updated plan as a structured checklist. "
                    "Only include remaining and new tasks."
                ),
                priority=Priority.HIGH,
            )

            if self.budget_manager and approval and approval.backend:
                self.budget_manager.record_usage(
                    agent.id, approval.backend,
                    response.tokens_in, response.tokens_out,
                )
                agent.spend_consciousness()

            new_queue = _parse_plan(response.content)
            new_queue.replan_count = agent.task_queue.replan_count + 1
            agent.task_queue = new_queue
            agent.write_identity("plan", response.content)

        if agent.state == AgentState.REPLANNING:
            agent.transition(AgentState.EXECUTING)

    def _write_plan(self, agent: Agent) -> None:
        """Serialize current task_queue back to plan.md."""
        if agent.task_queue is None:
            return

        lines = [f"# {agent.id} — Plan\n"]
        for task in agent.task_queue.tasks:
            check = "x" if task.status == "done" else " "
            priority_prefix = ""
            if task.priority == 2:
                priority_prefix = "**[CRITICAL]** "
            elif task.priority == 1:
                priority_prefix = "**[HIGH]** "

            status_suffix = ""
            if task.status == "skipped":
                status_suffix = " *(skipped)*"
            elif task.status == "exception":
                status_suffix = f" *(exception: {task.error})*"

            lines.append(f"- [{check}] {priority_prefix}{task.description}{status_suffix}")

        if agent.task_queue.exceptions:
            lines.append("")
            lines.append("## Exceptions")
            lines.append("")
            for task in agent.task_queue.exceptions:
                lines.append(f"- {task.description}: {task.error}")

        agent.write_identity("plan", "\n".join(lines) + "\n")

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
        result: dict[str, Any] = {
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
        if self.budget_manager:
            result["budget"] = {
                agent_id: {
                    "total_calls": s.total_calls,
                    "total_tokens": s.total_tokens,
                    "escalation_ratio": s.escalation_ratio,
                    "exhausted": s.exhausted,
                }
                for agent_id, s in self.budget_manager.all_status().items()
            }
        return result

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
        budget = f"{agent.consciousness_budget_used}/{agent.consciousness_budget_limit}"
        summary = (
            f"Tasks completed: {agent.tasks_completed_today}\n"
            f"Tasks escalated: {agent.tasks_escalated_today}\n"
            f"Consciousness budget used: {budget}\n"
        )

        if agent.task_queue is not None:
            stats = agent.task_queue.completion_summary()
            total = len(agent.task_queue.tasks)
            done_count = stats.get("done", 0)
            exception_count = stats.get("exceptions", 0)
            skipped_count = stats.get("skipped", 0)
            rate = (done_count / total * 100) if total > 0 else 0

            summary += (
                f"\n## Plan vs Reality\n"
                f"Total tasks: {total}\n"
                f"Completed: {done_count}\n"
                f"Exceptions: {exception_count}\n"
                f"Skipped: {skipped_count}\n"
                f"Completion rate: {rate:.0f}%\n"
                f"Replans: {agent.task_queue.replan_count}\n"
            )

            completed_tasks = [t for t in agent.task_queue.tasks if t.status == "done"]
            if completed_tasks:
                summary += "\nCompleted:\n"
                for t in completed_tasks:
                    summary += f"- {t.description}\n"

            if agent.task_queue.exceptions:
                summary += "\nExceptions:\n"
                for t in agent.task_queue.exceptions:
                    summary += f"- {t.description}: {t.error}\n"

        return summary
