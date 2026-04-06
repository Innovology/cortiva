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
import os
import platform
import re
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
from cortiva.core.agent import Agent, AgentState, Task, TaskQueue, _parse_plan
from cortiva.core.approval import ApprovalQueue
from cortiva.core.capacity import CapacityTracker
from cortiva.core.events import EventBus, FabricEvent
from cortiva.core.balancer import ClusterMetrics, CommunicationTracker
from cortiva.core.budget import ConsciousnessBudgetManager
from cortiva.core.cluster import Cluster, ClusterNode, move_agent
from cortiva.core.context import ContextBuilder
from cortiva.core.discovery import NodeCapabilities
from cortiva.core.familiarity import FamiliarityEngine
from cortiva.core.hooks import HookRouter
from cortiva.core.ipc import FabricServer
from cortiva.core.isolation import NoIsolation
from cortiva.core.delegation import DelegationManager
from cortiva.core.living_summary import LivingSummaryRegenerator
from cortiva.core.planner import (
    DAILY_PROMPT,
    MONTHLY_PROMPT,
    WEEKLY_PROMPT,
    Planner,
    build_daily_context,
    build_monthly_context,
    build_weekly_context,
)
from cortiva.core.org import OrgModel
from cortiva.core.policy import PolicyManager
from cortiva.core.resource_guard import ResourceGuard
from cortiva.core.session import SessionManager
from cortiva.core.timesheet import TimesheetManager
from cortiva.core.models import ClusterModels
from cortiva.core.reflection import ReflectionSuffix, parse_reflection_suffix
from cortiva.core.scheduler import Scheduler

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
        isolation: NoIsolation | None = None,
    ):
        self.agents_dir = Path(agents_dir)
        self.agents_dir.mkdir(parents=True, exist_ok=True)

        # Isolation enforcer
        self.isolation = isolation or NoIsolation(agents_dir=self.agents_dir)

        # Pluggable adapters
        self.memory = memory
        self.consciousness = consciousness
        self.routine = routine
        self.channel = channel
        self.terminal = terminal
        self.budget_manager = budget_manager
        if self.budget_manager and self.channel:
            self.budget_manager.on_alert = self._budget_alert
        self.context_builder = ContextBuilder(memory=memory)
        self.familiarity_engine = FamiliarityEngine(memory=memory)
        self.living_summary = LivingSummaryRegenerator(
            memory=memory, consciousness=consciousness,
        )
        self.scheduler = Scheduler()
        self.session_manager = SessionManager()
        self.timesheet_manager = TimesheetManager(self.agents_dir)
        self.capacity_tracker = CapacityTracker()
        self.policy_manager = PolicyManager()
        self.hook_router = HookRouter()
        self.encryption_vault: Any = None  # EncryptionVault or None
        self.credential_provider: Any = None  # CredentialProvider or None
        self.data_boundary: Any = None  # DataBoundaryEnforcer or None
        self.org: OrgModel | None = None
        self.delegation = DelegationManager(self.agents_dir / ".delegation")
        self.approval_queue = ApprovalQueue(self.agents_dir / ".approvals")
        self.resource_guard = ResourceGuard(self.agents_dir)
        self.communication_tracker = CommunicationTracker()
        self.cluster_metrics = ClusterMetrics(
            communication_tracker=self.communication_tracker,
        )

        # Cluster and model registry (optional, populated during start)
        self.cluster = Cluster()
        self.model_registry = ClusterModels()

        # IPC server (started on demand)
        self.ipc_server: FabricServer | None = None

        # Node capabilities (populated by discover() during start)
        self.capabilities: NodeCapabilities | None = None
        self._custom_endpoints: list[dict[str, Any]] = []
        self._cluster_config: dict[str, Any] = {}

        # Runtime state
        self.agents: dict[str, Agent] = {}
        self.heartbeat_interval = heartbeat_interval
        self.daily_consciousness_limit = daily_consciousness_limit
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        # Per-agent accumulated familiarity signals for today
        self._familiarity_signals: dict[str, list[dict[str, Any]]] = {}
        # Event listeners for portal/WebSocket integration
        self._event_listeners: list[Any] = []
        # Structured event bus (new — used alongside legacy listeners)
        self.event_bus: EventBus = EventBus()

    # ----- Event system -----

    def on_event(self, listener: Any) -> None:
        """Register a listener for fabric events.

        The listener is called with ``(event_type: str, data: dict)``
        for every state change, task completion, or lifecycle transition.
        """
        self._event_listeners.append(listener)

    def _emit(self, event_type: str, **data: Any) -> None:
        """Emit an event to all registered listeners and the EventBus."""
        import time as _time
        event = {"type": event_type, "timestamp": _time.time(), **data}
        for listener in self._event_listeners:
            try:
                listener(event_type, event)
            except Exception:
                pass  # Don't let listener errors break the fabric
        # Also emit to the structured EventBus
        bus_data = {k: v for k, v in data.items() if k != "agent_id"}
        self.event_bus.emit_simple(event_type, agent_id=data.get("agent_id"), **bus_data)

    def _budget_alert(self, agent_id: str, message: str, status: Any) -> None:
        """Post budget alerts to the ops channel."""
        if self.channel:
            import asyncio

            asyncio.ensure_future(
                self.channel.send(
                    sender="cortiva-fabric",
                    recipient="cortiva-ops",
                    content=message,
                    channel="#cortiva-ops",
                )
            )

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

        # Migrate flat layout if needed
        agent.migrate_flat_layout()

        # Reset today/ for a fresh day cycle
        agent.reset_today()
        self._familiarity_signals[agent_id] = []

        # Start a conversation session and clock in for the day
        self.session_manager.start(agent_id)

        # Determine scheduled hours from schedule config
        schedule = self.scheduler.get_schedule(agent_id)
        scheduled_hours = 8.0
        if schedule:
            wake_time = sleep_time = None
            for entry in schedule.entries:
                if entry.action == "wake" and entry.times:
                    wake_time = entry.times[0]
                if entry.action == "sleep" and entry.times:
                    sleep_time = entry.times[0]
            if wake_time and sleep_time:
                wake_mins = wake_time[0] * 60 + wake_time[1]
                sleep_mins = sleep_time[0] * 60 + sleep_time[1]
                scheduled_hours = max(0.0, (sleep_mins - wake_mins) / 60)
        self.timesheet_manager.clock_in(agent_id, scheduled_hours)

        identity = agent.read_all_identity()

        # Check for pending messages
        messages = []
        if self.channel:
            messages = await self.channel.receive(agent_id)

        agent.transition(AgentState.PLANNING)

        if self.budget_manager:
            self.budget_manager.reset_agent(agent_id)

        # --- Multi-horizon planning (monthly → weekly → daily) ---
        planner = Planner(agent.directory)
        delegation_text = self.delegation.pending_for_context(agent_id)

        # Monthly plan (generated on first wake of the month)
        if planner.needs_monthly_plan():
            monthly_ctx = await build_monthly_context(
                agent_id, self.memory,
                goals_context=self._goals_context(agent_id),
            )
            await self._conscious_plan(
                agent, identity, monthly_ctx, MONTHLY_PROMPT,
                call_type="plan_monthly",
                on_success=lambda text: planner.save_monthly(text),
            )
            logger.info(f"Agent {agent_id} created monthly plan")

        # Weekly plan (generated on first wake of the week)
        if planner.needs_weekly_plan():
            weekly_ctx = await build_weekly_context(
                agent_id, self.memory,
                monthly_plan=planner.store.current_monthly(),
                previous_weekly=planner.store.previous_weekly(),
                delegation_context=delegation_text,
            )
            await self._conscious_plan(
                agent, identity, weekly_ctx, WEEKLY_PROMPT,
                call_type="plan_weekly",
                on_success=lambda text: planner.save_weekly(text),
            )
            logger.info(f"Agent {agent_id} created weekly plan")

        # Daily plan (every wake)
        yesterday_journal = ""
        journal_dir = agent.directory / "journal"
        if journal_dir.is_dir():
            journals = sorted(journal_dir.glob("*.md"), reverse=True)
            if journals:
                yesterday_journal = journals[0].read_text(encoding="utf-8")[:300]

        daily_ctx = await build_daily_context(
            agent_id, self.memory,
            weekly_plan=planner.store.current_weekly(),
            yesterday_reflection=yesterday_journal,
            delegation_context=delegation_text,
        )

        # Build full context: identity + messages + org + plan cascade + daily context
        context = await self.context_builder.build_plan_context(agent, identity, messages)

        if self.org:
            org_text = self.org.org_context_for(agent_id)
            if org_text:
                context = context + "\n\n---\n\n" + org_text

        cascade = planner.cascade_context()
        if cascade:
            context = context + "\n\n---\n\n" + cascade

        context = context + "\n\n---\n\n" + daily_ctx

        # Inject any pending inbound hooks
        hook_context = self.hook_router.pending_context(agent_id)
        if hook_context:
            context = context + "\n\n---\n\n" + hook_context
        # Consume the hooks (they've been injected into the plan context)
        self.hook_router.pending_for(agent_id)

        # Validate context belongs to this agent
        self.session_manager.validate_agent(agent_id, context)

        # Generate daily plan
        await self._conscious_plan(
            agent, identity, context, DAILY_PROMPT,
            call_type="plan",
            on_success=lambda text: agent.set_plan(text),
        )
        logger.info(f"Agent {agent_id} has planned their day")

        agent.transition(AgentState.EXECUTING)
        self._emit("agent.wake", agent_id=agent_id, state=agent.state.value)
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
            day_summary = ContextBuilder.build_day_summary(agent)
            reflection_context = await self.context_builder.build_reflection_context(
                agent, identity, day_summary,
            )

            can_reflect = False
            approval = None
            if self.budget_manager:
                approval = self.budget_manager.request_budget(agent_id, "normal")
                can_reflect = approval.approved
            else:
                can_reflect = agent.spend_consciousness()

            if can_reflect:
                # Regenerate Living Summary from accumulated experience
                new_identity = await self.living_summary.regenerate(
                    agent, day_summary,
                )

                if self.budget_manager and approval and approval.backend:
                    self.budget_manager.record_usage(
                        agent_id, approval.backend, 0, 0,
                    )
                    agent.spend_consciousness()

                # Update Living Summary with regenerated content
                if new_identity:
                    agent.write_identity("identity", new_identity)

                # Write journal entry
                journal_path = agent.journal_path()
                journal_path.write_text(
                    new_identity or day_summary, encoding="utf-8",
                )
                logger.info(f"Agent {agent_id} reflected and updated identity")

        # Final runtime state persistence before clearing
        agent.persist_runtime_state()
        agent.clear_plan()
        agent.transition(AgentState.SLEEPING)

        # Clock out and end conversation session
        self.timesheet_manager.clock_out(
            agent_id,
            tasks_completed=agent.tasks_completed_today,
            tasks_escalated=agent.tasks_escalated_today,
            consciousness_calls=agent.consciousness_budget_used,
        )
        self.session_manager.end(agent_id)
        self.isolation.cleanup(agent_id)

        self._emit("agent.sleep", agent_id=agent_id, state=agent.state.value)
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

        # Re-activate any tasks that have been approved since last cycle
        self._check_approved_tasks(agent)

        result: dict[str, Any] = {
            "agent_id": agent_id,
            "action": "idle",
            "conscious_call": False,
            "task": None,
            "all_tasks_complete": False,
        }

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

        # Agent decides what to work on next
        task = agent.next_task()
        if task is None:
            result["action"] = "idle"
            result["all_tasks_complete"] = (
                agent.task_queue.all_done() if agent.task_queue else True
            )
            return result

        # Execute the task (with capacity tracking)
        self.capacity_tracker.task_started(agent_id, task.id)
        await self._execute_task(agent, task, messages)
        self.capacity_tracker.task_finished(agent_id, task.id)
        result["action"] = "executed_task"
        result["task"] = task.description
        result["conscious_call"] = True

        # Write updated plan and runtime state to disk
        self._write_plan(agent)
        agent.persist_runtime_state()
        self._emit(
            "task.complete", agent_id=agent_id,
            task=task.description, status=task.status,
        )

        return result

    async def _execute_task(
        self, agent: Agent, task: Task, messages: list[Any]
    ) -> None:
        """Execute a single task via routine or consciousness."""
        # Check execution policy before starting
        policy_result = self.policy_manager.check_action(agent.id, task.description)
        if policy_result.denied:
            agent.fail_task(task, f"Policy denied: {policy_result.reason}")
            logger.warning(
                "Agent %s task blocked by policy: %s — %s",
                agent.id, task.description, policy_result.reason,
            )
            self._emit(
                "policy.denied", agent_id=agent.id,
                task=task.description, reason=policy_result.reason,
            )
            return

        if policy_result.needs_approval:
            approver = "human"
            if self.org:
                approver = self.org.approver_for(agent.id)
            self.approval_queue.submit(
                agent_id=agent.id,
                task_description=task.description,
                policy_rule=policy_result.matched_rule,
                approver_id=approver,
            )
            # Notify the approver via channel
            if self.channel and approver != "human":
                try:
                    await self.channel.send(
                        sender="cortiva-fabric",
                        recipient=approver,
                        content=(
                            f"Approval needed: {agent.id} wants to: "
                            f"{task.description}"
                        ),
                    )
                except Exception:
                    pass  # don't block on notification failure
            logger.info(
                "Agent %s task requires approval from %s: %s",
                agent.id, approver, task.description,
            )
            self._emit(
                "approval.requested", agent_id=agent.id,
                task=task.description, approver=approver,
            )
            agent.defer_task(task, f"Awaiting approval: {policy_result.reason}")
            return

        task.status = "in_progress"
        routine_assessment: dict[str, Any] | None = None

        if self.budget_manager:
            self.budget_manager.record_task_attempt(agent.id)

        # Compute familiarity signal from memory and accumulate for persistence
        familiarity = await self.familiarity_engine.assess(agent.id, task.description)
        signals = self._familiarity_signals.setdefault(agent.id, [])
        signals.append({
            "task": task.description,
            "strength": familiarity.strength,
            "valence": familiarity.valence,
            "match_count": familiarity.match_count,
        })
        agent.persist_familiarity(signals)

        if self.routine:
            # Ask the routine layer whether this can be handled procedurally
            routine_assessment = await self.routine.assess(
                agent_id=agent.id,
                task_description=task.description,
                procedural_index=agent.read_identity("procedures"),
                familiarity=familiarity,
            )
            action = routine_assessment.get("action", "escalate")

            if action == "defer":
                task.status = "exception"
                task.error = "Routine deferred task"
                assert agent.task_queue is not None
                agent.task_queue.exceptions.append(task)
                agent.tasks_escalated_today += 1
                return
            elif action == "procedural":
                task.status = "done"
                task.outcome = routine_assessment.get("result", "Completed procedurally")
                agent.tasks_completed_today += 1
                return
            # else: escalate — fall through to consciousness

        # Try terminal agent for hands-on tasks (coding, file ops, testing)
        if self.terminal and self._is_terminal_task(task.description):
            terminal_result = await self._execute_via_terminal(agent, task)
            if terminal_result is not None:
                return

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
        context = await self.context_builder.build_execution_context(
            agent, identity, messages, task.description, assessment=routine_assessment,
        )

        # Inject session context (what the agent has done so far today)
        session_context = self.session_manager.render(agent.id)
        if session_context:
            context = context + "\n\n---\n\n" + session_context

        # Validate context belongs to this agent
        self.session_manager.validate_agent(agent.id, context)

        prompt = (
            f"Execute this task: {task.description}\n\n"
            "Describe what you did and the outcome."
        )

        response = await self.consciousness.think(
            agent_id=agent.id,
            context=context,
            prompt=prompt,
            priority=Priority.HIGH if task.priority >= 1 else Priority.NORMAL,
            metadata={"call_type": "execute", "task_execution": True},
        )

        # Record in session for continuity across tasks
        self.session_manager.record(
            agent.id, task.description, response.content, call_type="execute",
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

        # Send inter-agent messages via channel adapter and persist to outbox
        if suffix.messages:
            import json as _json
            agent.write_outbox("messages.json", _json.dumps(suffix.messages, indent=2))
            if self.channel:
                for msg in suffix.messages:
                    recipient = msg.get("to", "")
                    content = msg.get("content", "")
                    if recipient and content:
                        await self.channel.send(
                            sender=agent.id,
                            recipient=recipient,
                            content=content,
                        )
                        self.communication_tracker.record(agent.id, recipient)

        # Log escalation request and persist to outbox
        if suffix.escalation:
            import json as _json
            agent.write_outbox("escalations.json", _json.dumps(
                {"task": task.description, "escalation": suffix.escalation}, indent=2,
            ))
            logger.warning(
                f"Agent {agent.id} escalation on '{task.description}': "
                f"{suffix.escalation}"
            )

        # Process delegation requests (manager → subordinate)
        if suffix.delegate:
            for d in suffix.delegate:
                to_agent = d.get("to", "")
                desc = d.get("description", "")
                prio = d.get("priority", 1)
                if to_agent and desc:
                    try:
                        self.delegation.create_assignment(
                            from_agent=agent.id,
                            to_agent=to_agent,
                            description=desc,
                            priority=prio,
                            org=self.org,
                        )
                        self._emit(
                            "delegation.created", agent_id=agent.id,
                            to_agent=to_agent, description=desc,
                        )
                    except PermissionError as exc:
                        logger.warning("Delegation rejected: %s", exc)

        # Process assignment completion
        if suffix.complete_assignment:
            completed = self.delegation.complete_assignment(
                suffix.complete_assignment, task.outcome or task.description,
            )
            if completed:
                self._emit(
                    "delegation.completed", agent_id=agent.id,
                    assignment_id=completed.id, outcome=completed.outcome,
                )

        # Process shared learning (org-wide knowledge)
        if suffix.shared_learning:
            await self.memory.store(
                agent_id="__org_shared__",
                content=suffix.shared_learning,
                tags=["shared", "learning", f"author:{agent.id}"],
                importance=7.0,
            )
            self._emit(
                "shared_memory.stored", agent_id=agent.id,
                content=suffix.shared_learning[:100],
            )

        # Process self-scheduling requests
        if suffix.schedule:
            result = self.scheduler.apply_schedule_request(agent.id, suffix.schedule)
            if result:
                logger.info("Agent %s self-scheduled: %s", agent.id, result)
                self._emit(
                    "schedule.self_modified", agent_id=agent.id,
                    changes=result,
                )

    def _check_approved_tasks(self, agent: Agent) -> None:
        """Re-activate tasks that have been approved since last cycle."""
        if agent.task_queue is None:
            return
        approved = self.approval_queue.approved_tasks_for(agent.id)
        for req in approved:
            for task in agent.task_queue.tasks:
                if (
                    task.description == req.task_description
                    and task.status == "pending_approval"
                ):
                    task.status = "pending"
                    task.priority = max(task.priority, 1)  # bump priority
                    logger.info(
                        "Agent %s task re-activated after approval: %s",
                        agent.id, task.description[:60],
                    )
                    break

    # Terminal keywords that indicate a task should use the terminal agent
    _TERMINAL_KEYWORDS = frozenset({
        "implement", "code", "write", "fix", "refactor", "test", "debug",
        "commit", "branch", "merge", "deploy", "build", "run", "install",
        "create file", "edit file", "update file", "delete file",
        "pytest", "ruff", "lint", "review code", "open pr", "push",
    })

    def _is_terminal_task(self, description: str) -> bool:
        """Check if a task description suggests terminal agent work."""
        desc_lower = description.lower()
        return any(kw in desc_lower for kw in self._TERMINAL_KEYWORDS)

    async def _execute_via_terminal(self, agent: Agent, task: Task) -> bool | None:
        """Execute a task via the terminal agent adapter.

        Returns ``True`` if the terminal handled the task, ``None`` if it
        should fall through to consciousness execution.
        """
        assert self.terminal is not None

        if not await self.terminal.is_available():
            logger.warning(f"Terminal agent unavailable for {agent.id}, falling back")
            return None

        # Build a prompt that includes agent identity context
        identity = agent.read_all_identity()
        procedures = identity.get("procedures", "")
        responsibilities = identity.get("responsibilities", "")

        prompt = (
            f"You are {agent.id}. Execute this task:\n\n"
            f"{task.description}\n\n"
            f"## Your Procedures\n{procedures}\n\n"
            f"## Your Responsibilities\n{responsibilities}\n\n"
            "When done, summarise what you did and the outcome."
        )

        # Use the agent's workspace directory as cwd
        cwd = agent.directory / "workspace"
        cwd.mkdir(parents=True, exist_ok=True)

        # Apply isolation envelope
        envelope = self.isolation.prepare_terminal_env(
            agent_id=agent.id, cmd=[], cwd=cwd,
        )

        # Enforce tool-level policy
        policy = self.policy_manager.get(agent.id)
        allowed_tools = policy.tools.effective_allowed()

        response = await self.terminal.invoke(
            prompt=prompt,
            cwd=envelope.cwd,
            env=envelope.env,
            allowed_tools=allowed_tools,
        )

        if response.is_error:
            task.status = "exception"
            task.error = f"Terminal error: {response.content[:200]}"
            assert agent.task_queue is not None
            agent.task_queue.exceptions.append(task)
            agent.tasks_escalated_today += 1
            logger.error(f"Terminal execution failed for {agent.id}: {response.content[:100]}")
            return True

        task.status = "done"
        task.outcome = response.content[:500] if response.content else "Completed via terminal"
        agent.tasks_completed_today += 1

        # Store as memory
        await self.memory.store(
            agent_id=agent.id,
            content=f"Task: {task.description}. Outcome (terminal): {task.outcome[:200]}",
            tags=["cycle", "task", "terminal"],
            importance=5.0 + task.priority,
        )

        logger.info(f"Agent {agent.id} completed task via terminal: {task.description[:60]}")
        return True

    async def _conscious_plan(
        self,
        agent: Agent,
        identity: dict[str, str],
        context: str,
        prompt: str,
        *,
        call_type: str = "plan",
        on_success: Any = None,
    ) -> str | None:
        """Make a consciousness call for planning and handle budget.

        Returns the response content, or None if budget was exhausted.
        Calls ``on_success(content)`` if provided.
        """
        can_plan = False
        approval = None
        if self.budget_manager:
            approval = self.budget_manager.request_budget(agent.id, "normal")
            can_plan = approval.approved
        else:
            can_plan = agent.spend_consciousness()

        if not can_plan:
            return None

        response = await self.consciousness.think(
            agent_id=agent.id,
            context=context,
            prompt=prompt,
            priority=Priority.NORMAL,
            metadata={"call_type": call_type},
        )

        self.session_manager.record(
            agent.id, prompt, response.content, call_type=call_type,
        )

        if self.budget_manager and approval and approval.backend:
            self.budget_manager.record_usage(
                agent.id, approval.backend,
                response.tokens_in, response.tokens_out,
            )
            agent.spend_consciousness()

        if on_success:
            on_success(response.content)

        return response.content

    def _goals_context(self, agent_id: str) -> str:
        """Build goals context for planning, if GoalManager is available."""
        try:
            from cortiva.core.goals import GoalManager
            goals_dir = self.agents_dir / ".goals"
            if goals_dir.exists():
                gm = GoalManager(goals_dir)
                return gm.agent_goals_context(agent_id)
        except Exception:
            pass
        return ""

    def _should_replan(self, agent: Agent, messages: list[Any]) -> bool:
        """Delegate replan decision to the agent."""
        return agent.needs_replan(messages)

    async def _replan(self, agent: Agent, messages: list[Any]) -> None:
        """Trigger a replan: EXECUTING -> REPLANNING, build new plan, -> EXECUTING."""
        assert agent.task_queue is not None

        if agent.state == AgentState.EXECUTING:
            agent.transition(AgentState.REPLANNING)

        identity = agent.read_all_identity()
        context = await self.context_builder.build_replan_context(agent, identity, messages)

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
                metadata={"call_type": "replan"},
            )

            if self.budget_manager and approval and approval.backend:
                self.budget_manager.record_usage(
                    agent.id, approval.backend,
                    response.tokens_in, response.tokens_out,
                )
                agent.spend_consciousness()

            agent.update_plan(response.content)

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
        Check all agents. Process scheduled actions and run cycles
        for active agents.
        """
        # Process scheduled actions
        due = self.scheduler.tick()
        for agent_id, actions in due.items():
            if agent_id not in self.agents:
                continue
            agent = self.agents[agent_id]
            for action in actions:
                try:
                    if action == "wake" and agent.state == AgentState.SLEEPING:
                        await self.wake(agent_id)
                    elif action == "sleep" and agent.state in (
                        AgentState.EXECUTING, AgentState.REPLANNING,
                    ):
                        await self.sleep(agent_id)
                    elif action == "replan" and agent.state == AgentState.EXECUTING:
                        await self._replan(agent, [])
                except Exception as e:
                    logger.error(f"Scheduler action {action} for {agent_id}: {e}")

        # Run cycles for active agents concurrently (with resource guards)
        self.resource_guard.reset_heartbeat()
        self.capacity_tracker.heartbeat_start()

        async def _run_cycle(aid: str) -> None:
            # Pre-cycle resource check
            ts = self.timesheet_manager.get(aid)
            hours = ts.today().total_hours
            blocked = self.resource_guard.pre_cycle_check(aid, hours_today=hours)
            if blocked:
                logger.info("Cycle blocked for %s: %s", aid, blocked)
                self._emit("resource.blocked", agent_id=aid, reason=blocked)
                return

            cycle_start = self.capacity_tracker.agent_cycle_start(aid)
            try:
                # Wrap cycle with timeout from resource limits
                result = await self.resource_guard.wrap_cycle(
                    aid, self.cycle(aid),
                )
                if result is None:
                    self._emit(
                        "resource.timeout", agent_id=aid,
                        timeout=self.resource_guard.limits_for(aid).cycle_timeout_s,
                    )
            except Exception as e:
                logger.error(f"Cycle error for {aid}: {e}")
            finally:
                self.capacity_tracker.agent_cycle_end(aid, cycle_start)

            # Post-cycle violation check
            violations = self.resource_guard.post_cycle_check(aid)
            if violations:
                logger.warning("Agent %s resource violations: %s", aid, violations)
                self._emit(
                    "resource.violation", agent_id=aid, violations=violations,
                )

        coros = [
            _run_cycle(agent_id)
            for agent_id, agent in self.agents.items()
            if agent.state == AgentState.EXECUTING
        ]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        self.capacity_tracker.heartbeat_end()

    async def _heartbeat_loop(self) -> None:
        """Continuous heartbeat loop."""
        while self._running:
            try:
                await self.heartbeat()
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(self.heartbeat_interval)

    # ----- IPC command handlers -----

    def _register_ipc_handlers(self, server: FabricServer) -> None:
        """Register all IPC command handlers on *server*."""

        async def _handle_status(**_kw: Any) -> dict[str, Any]:
            return {"ok": True, **self.status()}

        async def _handle_agent_wake(agent_id: str = "", **_kw: Any) -> dict[str, Any]:
            if not agent_id:
                return {"ok": False, "error": "agent_id required"}
            try:
                agent = await self.wake(agent_id)
                return {"ok": True, "agent_id": agent_id, "state": agent.state.value}
            except (KeyError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}

        async def _handle_agent_sleep(agent_id: str = "", **_kw: Any) -> dict[str, Any]:
            if not agent_id:
                return {"ok": False, "error": "agent_id required"}
            try:
                agent = await self.sleep(agent_id)
                return {"ok": True, "agent_id": agent_id, "state": agent.state.value}
            except (KeyError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}

        async def _handle_agent_cycle(agent_id: str = "", **_kw: Any) -> dict[str, Any]:
            if not agent_id:
                return {"ok": False, "error": "agent_id required"}
            try:
                result = await self.cycle(agent_id)
                return {"ok": True, **result}
            except (KeyError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}

        async def _handle_budget(**_kw: Any) -> dict[str, Any]:
            if not self.budget_manager:
                return {"ok": True, "budget": {}}
            return {"ok": True, "budget": {
                aid: {
                    "total_calls": s.total_calls,
                    "total_tokens": s.total_tokens,
                    "escalation_ratio": s.escalation_ratio,
                    "exhausted": s.exhausted,
                }
                for aid, s in self.budget_manager.all_status().items()
            }}

        async def _handle_shutdown(**_kw: Any) -> dict[str, Any]:
            asyncio.get_event_loop().call_soon(self._request_stop)
            return {"ok": True, "message": "Shutdown initiated"}

        async def _handle_discover(**_kw: Any) -> dict[str, Any]:
            if self.capabilities:
                return {"ok": True, **self.capabilities.to_dict()}
            return {"ok": False, "error": "Discovery not yet run"}

        async def _handle_cluster_load(**_kw: Any) -> dict[str, Any]:
            nodes = self.cluster_metrics.snapshot(
                self.capabilities, self.agents, self.budget_manager,
            )
            affinities = self.cluster_metrics.agent_affinity_scores()
            moves = self.cluster_metrics.suggest_moves()
            return {
                "ok": True,
                "nodes": [n.to_dict() for n in nodes],
                "affinities": {
                    f"{a}->{b}": score for (a, b), score in affinities.items()
                },
                "moves": [m.to_dict() for m in moves],
            }

        async def _handle_cluster_status(**_kw: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "local_node_id": self.cluster.local_node_id,
                "node_count": self.cluster.node_count(),
                "single_node": self.cluster.is_single_node(),
                "discovery_mode": self.cluster.discovery_mode,
                "registry": self.cluster.get_registry(),
                "models": self.model_registry.all_model_names(),
            }

        async def _handle_cluster_nodes(**_kw: Any) -> dict[str, Any]:
            nodes_data: list[dict[str, Any]] = []
            for node in self.cluster.nodes.values():
                nodes_data.append(node.to_dict())
            return {"ok": True, "nodes": nodes_data}

        async def _handle_agent_move(
            agent_id: str = "", target_node: str = "", **_kw: Any,
        ) -> dict[str, Any]:
            if not agent_id:
                return {"ok": False, "error": "agent_id required"}
            if not target_node:
                return {"ok": False, "error": "target_node required"}
            try:
                result = await move_agent(self.cluster, agent_id, target_node)
                return {"ok": result.success, **result.to_dict()}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        async def _handle_agent_activity(
            agent_id: str = "", **_kw: Any,
        ) -> dict[str, Any]:
            """Return live activity: current task, session, timesheet."""
            if not agent_id:
                return {"ok": False, "error": "agent_id required"}
            if agent_id not in self.agents:
                return {"ok": False, "error": f"Unknown agent: {agent_id}"}
            agent = self.agents[agent_id]

            # Current task
            current_task = None
            pending_tasks: list[dict[str, str]] = []
            completed_tasks: list[dict[str, str]] = []
            if agent.task_queue:
                for t in agent.task_queue.tasks:
                    td = {"id": t.id, "description": t.description, "status": t.status}
                    if t.status == "in_progress":
                        current_task = td
                    elif t.status == "pending":
                        pending_tasks.append(td)
                    elif t.status == "done":
                        completed_tasks.append(td)

            # Session turns
            session_turns: list[dict[str, str]] = []
            session = self.session_manager.get(agent_id)
            if session:
                for turn in session.turns:
                    session_turns.append({
                        "role": turn.role,
                        "call_type": turn.call_type,
                        "content": turn.content[:200],
                    })

            # Timesheet
            ts = self.timesheet_manager.get(agent_id)
            today = ts.today()

            return {
                "ok": True,
                "agent_id": agent_id,
                "state": agent.state.value,
                "current_task": current_task,
                "completed_tasks": completed_tasks,
                "pending_tasks": pending_tasks,
                "session_turns": session_turns,
                "timesheet": today.to_dict(),
            }

        async def _handle_agent_hours(
            agent_id: str = "", period: str = "today", **_kw: Any,
        ) -> dict[str, Any]:
            """Return working hours summary."""
            if not agent_id:
                return {"ok": False, "error": "agent_id required"}

            ts = self.timesheet_manager.get(agent_id)
            if period == "week":
                week = ts.week()
                total_hours = sum(d.total_hours for d in week)
                total_overtime = sum(d.overtime_hours for d in week)
                return {
                    "ok": True,
                    "agent_id": agent_id,
                    "period": "week",
                    "total_hours": round(total_hours, 2),
                    "total_overtime": round(total_overtime, 2),
                    "days": [d.to_dict() for d in week],
                }
            else:
                today = ts.today()
                return {
                    "ok": True,
                    "agent_id": agent_id,
                    "period": "today",
                    **today.to_dict(),
                }

        async def _handle_watch(**_kw: Any) -> dict[str, Any]:
            """Return live dashboard data for all agents."""
            agents_data: dict[str, Any] = {}
            for aid, agent in self.agents.items():
                current_task = None
                task_progress = ""
                if agent.task_queue:
                    total = len(agent.task_queue.tasks)
                    done = sum(1 for t in agent.task_queue.tasks if t.status == "done")
                    task_progress = f"{done}/{total}"
                    for t in agent.task_queue.tasks:
                        if t.status == "in_progress":
                            current_task = t.description[:60]
                            break

                ts = self.timesheet_manager.get(aid)
                today = ts.today()

                agents_data[aid] = {
                    "state": agent.state.value,
                    "current_task": current_task,
                    "task_progress": task_progress,
                    "consciousness_used": agent.consciousness_budget_used,
                    "consciousness_limit": agent.consciousness_budget_limit,
                    "hours_today": round(today.total_hours, 2),
                    "overtime_hours": round(today.overtime_hours, 2),
                    "scheduled_hours": today.scheduled_hours,
                }

            active = sum(
                1 for a in self.agents.values()
                if a.state in (AgentState.EXECUTING, AgentState.REPLANNING)
            )
            capacity = self.capacity_tracker.snapshot(
                active, len(self.agents), self.heartbeat_interval,
            )
            return {"ok": True, "agents": agents_data, "capacity": capacity}

        async def _handle_capacity(**_kw: Any) -> dict[str, Any]:
            """Return detailed capacity and contention metrics."""
            active = sum(
                1 for a in self.agents.values()
                if a.state in (AgentState.EXECUTING, AgentState.REPLANNING)
            )
            return {
                "ok": True,
                **self.capacity_tracker.snapshot(
                    active, len(self.agents), self.heartbeat_interval,
                ),
            }

        async def _handle_resources(**_kw: Any) -> dict[str, Any]:
            """Return shared resource status — models, adapters, limits."""
            models: list[str] = self.model_registry.all_model_names()
            terminal_agents: list[str] = []
            if self.capabilities:
                terminal_agents = [
                    t.name for t in self.capabilities.terminal_agents if t.available
                ]

            agent_resources: dict[str, Any] = {}
            for aid in self.agents:
                agent_resources[aid] = self.resource_guard.status(aid)

            return {
                "ok": True,
                "shared": {
                    "models": models,
                    "terminal_agents": terminal_agents,
                    "consciousness_provider": type(self.consciousness).__name__,
                    "memory_adapter": type(self.memory).__name__,
                    "channel_adapter": type(self.channel).__name__ if self.channel else None,
                    "routine_adapter": type(self.routine).__name__ if self.routine else None,
                },
                "agents": agent_resources,
            }

        async def _handle_hook_receive(
            source: str = "", event_type: str = "", payload: dict | None = None,
            **_kw: Any,
        ) -> dict[str, Any]:
            """Receive an inbound hook and route to an agent."""
            if not source or not event_type:
                return {"ok": False, "error": "source and event_type required"}

            event = self.hook_router.route(source, event_type, payload or {})
            if event is None:
                return {"ok": False, "error": "No route matched"}

            # Wake the agent if sleeping and route says to
            woke = False
            if self.hook_router.should_wake(event):
                agent_id = event.routed_to
                if agent_id in self.agents:
                    agent = self.agents[agent_id]
                    if agent.state == AgentState.SLEEPING:
                        try:
                            await self.wake(agent_id)
                            event.woke_agent = True
                            woke = True
                            logger.info(
                                "Hook woke agent %s: %s/%s",
                                agent_id, source, event_type,
                            )
                        except Exception as exc:
                            logger.error("Failed to wake %s on hook: %s", agent_id, exc)

            self._emit(
                "hook.received", agent_id=event.routed_to,
                source=source, event_type=event_type,
                priority=event.priority, woke_agent=woke,
            )

            return {"ok": True, **event.to_dict()}

        async def _handle_hook_list(**_kw: Any) -> dict[str, Any]:
            """List recent hooks."""
            return {
                "ok": True,
                "hooks": [h.to_dict() for h in self.hook_router.recent_hooks()],
            }

        async def _handle_agent_chat(
            agent_id: str = "", message: str = "", **_kw: Any,
        ) -> dict[str, Any]:
            """Send a message to an agent and get a response."""
            if not agent_id or not message:
                return {"ok": False, "error": "agent_id and message required"}
            if agent_id not in self.agents:
                return {"ok": False, "error": f"Unknown agent: {agent_id}"}

            from cortiva.core.chat import AgentChat

            agent = self.agents[agent_id]
            chat = AgentChat(
                agent=agent,
                consciousness=self.consciousness,
                memory=self.memory,
                session_manager=self.session_manager,
            )
            try:
                response = await chat.send(message)
                return {"ok": True, "agent_id": agent_id, "response": response}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        async def _handle_agent_logs(
            agent_id: str = "", limit: int = 20, **_kw: Any,
        ) -> dict[str, Any]:
            """Get recent activity logs for an agent."""
            if not agent_id:
                return {"ok": False, "error": "agent_id required"}
            if agent_id not in self.agents:
                return {"ok": False, "error": f"Unknown agent: {agent_id}"}

            from cortiva.core.chat import get_agent_logs

            agent = self.agents[agent_id]
            logs = await get_agent_logs(agent, self.memory, limit=limit)
            return {"ok": True, **logs}

        server.register("status", _handle_status)
        server.register("watch", _handle_watch)
        server.register("resources", _handle_resources)
        server.register("capacity", _handle_capacity)
        server.register("agent.activity", _handle_agent_activity)
        server.register("agent.hours", _handle_agent_hours)
        server.register("hook.receive", _handle_hook_receive)
        server.register("hook.list", _handle_hook_list)
        server.register("agent.chat", _handle_agent_chat)
        server.register("agent.logs", _handle_agent_logs)
        server.register("agent.wake", _handle_agent_wake)
        server.register("agent.sleep", _handle_agent_sleep)
        server.register("agent.cycle", _handle_agent_cycle)
        server.register("agent.move", _handle_agent_move)
        server.register("budget", _handle_budget)
        server.register("discover", _handle_discover)
        server.register("cluster.load", _handle_cluster_load)
        server.register("cluster.status", _handle_cluster_status)
        server.register("cluster.nodes", _handle_cluster_nodes)
        server.register("shutdown", _handle_shutdown)

    def _request_stop(self) -> None:
        """Signal the fabric to stop (used by shutdown IPC command)."""
        self._running = False

    def load_schedules(self, schedules: dict[str, dict[str, str]]) -> None:
        """Load agent schedules from config.

        *schedules* maps agent IDs to schedule dicts, e.g.::

            {"bookkeep-01": {"wake": "09:00 mon-fri", "sleep": "17:00"}}
        """
        for agent_id, sched_config in schedules.items():
            self.scheduler.register(agent_id, sched_config)

    # ----- Start / Stop -----

    async def start(
        self,
        *,
        ipc_socket: Path | None = None,
        custom_endpoints: list[dict[str, Any]] | None = None,
    ) -> None:
        """Start the fabric. Discover agents and begin heartbeat.

        If *ipc_socket* is given, an IPC server is started on that path.
        *custom_endpoints* are passed to node capability discovery.
        """
        logger.info("Starting Cortiva fabric")

        # Auto-discover node capabilities
        node_id = self._cluster_config.get("node_id") or f"{platform.node()}-{os.getpid()}"
        all_endpoints = custom_endpoints or self._custom_endpoints or None
        self.capabilities = await NodeCapabilities.discover(
            node_id, custom_endpoints=all_endpoints,
        )
        logger.info(f"Node capabilities: {self.capabilities.summary}")

        # Initialize cluster
        discovery_mode = self._cluster_config.get("discovery", "static")
        self.cluster = Cluster(local_node_id=node_id, discovery_mode=discovery_mode)
        self.model_registry = ClusterModels(local_node_id=node_id)

        # Register local node
        self.discover_agents()
        local_node = ClusterNode(
            node_id=node_id,
            host=self._cluster_config.get("host", "localhost"),
            port=self._cluster_config.get("port", 9400),
            agents=list(self.agents.keys()),
            capabilities=self.capabilities.to_dict(),
        )
        await self.cluster.join(local_node)

        # Update model registry with local capabilities
        self.model_registry.update_node(
            node_id,
            models=[m.to_dict() for m in self.capabilities.local_models],
            terminal_agents=[t.name for t in self.capabilities.terminal_agents if t.available],
            custom_endpoints=[e.to_dict() for e in self.capabilities.custom_endpoints],
            agent_count=len(self.agents),
        )

        # Discover cluster peers
        static_nodes = self._cluster_config.get("static_nodes")
        config_path = self._cluster_config.get("config_path")
        if static_nodes or config_path or discovery_mode == "mdns":
            peers = await self.cluster.discover(
                static_nodes=static_nodes,
                config_path=config_path,
            )
            for peer in peers:
                caps = peer.capabilities
                if isinstance(caps, dict):
                    self.model_registry.update_node(
                        peer.node_id,
                        host=peer.host,
                        models=caps.get("local_models", []),
                        terminal_agents=[
                            t.get("name", "") for t in caps.get("terminal_agents", [])
                            if t.get("available")
                        ],
                        custom_endpoints=caps.get("custom_endpoints", []),
                        agent_count=len(peer.agents),
                    )
            if peers:
                logger.info(f"Discovered {len(peers)} cluster peer(s)")
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        if ipc_socket is not None:
            self.ipc_server = FabricServer()
            self._register_ipc_handlers(self.ipc_server)
            await self.ipc_server.start(ipc_socket)

        logger.info(f"Fabric running with {len(self.agents)} agents")

    async def stop(self) -> None:
        """Stop the fabric. Sleep all active agents and close IPC."""
        logger.info("Stopping Cortiva fabric")
        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Stop IPC server
        if self.ipc_server:
            await self.ipc_server.stop()
            self.ipc_server = None

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
        if self.capabilities:
            result["capabilities"] = self.capabilities.to_dict()
        return result

