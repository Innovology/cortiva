"""
Cortiva Fabric — the organisational nervous system.

The Fabric is the runtime that manages all agents. It holds references
to the pluggable adapters (memory, consciousness, routine, channel),
manages agent lifecycles, runs the heartbeat, and orchestrates the
plan-execute-replan cycle.
"""

from __future__ import annotations

import asyncio
import json
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
from cortiva.core.balancer import ClusterMetrics, CommunicationTracker
from cortiva.core.budget import ConsciousnessBudgetManager
from cortiva.core.capacity import CapacityTracker
from cortiva.core.cluster import Cluster, ClusterNode, move_agent
from cortiva.core.context import ContextBuilder, _identity_to_context
from cortiva.core.credentials import load_agent_credentials
from cortiva.core.delegation import DelegationManager
from cortiva.core.discovery import NodeCapabilities
from cortiva.core.emotions import (
    EMOTIONS_FILENAME,
    blend_emotions,
    derive_emotions,
    parse_persona_modifiers,
    signals_from_task,
)
from cortiva.core.events import EventBus
from cortiva.core.familiarity import FamiliarityEngine
from cortiva.core.hooks import HookRouter
from cortiva.core.ipc import FabricServer
from cortiva.core.isolation import NoIsolation
from cortiva.core.living_summary import (
    LivingSummaryRegenerator,
    split_identity_and_day_report,
)
from cortiva.core.models import ClusterModels
from cortiva.core.org import OrgModel
from cortiva.core.planner import (
    DAILY_PROMPT,
    MONTHLY_PROMPT,
    WEEKLY_PROMPT,
    Planner,
    build_daily_context,
    build_monthly_context,
    build_weekly_context,
)
from cortiva.core.plugins import PluginManager
from cortiva.core.policy import PolicyManager
from cortiva.core.reactive import ReactiveEngine
from cortiva.core.reflection import ReflectionSuffix, parse_reflection_suffix
from cortiva.core.resource_guard import ResourceGuard
from cortiva.core.scheduler import Scheduler
from cortiva.core.session import SessionManager
from cortiva.core.timesheet import TimesheetManager

logger = logging.getLogger("cortiva.fabric")

# How many exceptions before a replan is triggered
EXCEPTION_THRESHOLD = 3

# Maximum number of replans per wake cycle
MAX_REPLANS = 3

# How to reach a colleague — the office communication protocol. Synchronous
# by default (you're in a shared office), email as the durable fallback.
_REACH_PROTOCOL = (
    "\n**Reaching a colleague.** Check if they're around. If they're awake and "
    "free, talk to them directly — a quick message — and wrap up when you're "
    "done. If they're mid-conversation, give them a moment to finish unless "
    "what you carry genuinely matters more than what they're on. If they're "
    "offline, or you've waited too long, **email them instead** — email always "
    "lands and stays on record. Route anything you'd escalate through your "
    "manager first."
)


def _is_github_email(from_field: str) -> bool:
    """True when an email is a GitHub notification (sender on github.com).

    Covers ``notifications@github.com`` and ``noreply@github.com`` — the
    addresses GitHub sends PR/issue/review/CI notifications from.
    """
    import re

    m = re.search(r"[\w.+-]+@([\w.-]+)", from_field or "")
    domain = (m.group(1) if m else (from_field or "")).strip().lower()
    return domain == "github.com" or domain.endswith(".github.com")


def _resolve_msg_email(
    recipient: str, cards_by_key: dict, domain: str,
) -> str | None:
    """Best-effort email address for a peer-message recipient.

    Used when a message can't be delivered in-process (recipient on another
    machine, or unknown to the bus) so it falls back to durable email. Order:
    an address as given → a known colleague's email → ``<handle>@<domain>``.
    Returns None when there's no plausible route.
    """
    r = (recipient or "").strip()
    if not r:
        return None
    if "@" in r:
        return r
    card = cards_by_key.get(r.lower())
    if card and card.get("email"):
        return str(card["email"])
    if domain and all(ch.isalnum() or ch in "-._" for ch in r):
        return f"{r.lower()}@{domain}"
    return None


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
        self.plugin_manager = PluginManager(fabric=self)
        self.reactive_engine = ReactiveEngine()
        self.encryption_vault: Any = None  # EncryptionVault or None
        self.credential_provider: Any = None  # CredentialProvider or None
        self.data_boundary: Any = None  # DataBoundaryEnforcer or None
        self.org: OrgModel | None = None
        # True when org structure came from an explicit cortiva.yaml `org:`
        # section; when False, the org is derived from agents' deploy.yaml
        # at discovery (and after hire/reassign) so it stays current.
        self._org_from_config = False
        self.delegation = DelegationManager(self.agents_dir / ".delegation")
        self.approval_queue = ApprovalQueue(self.agents_dir / ".approvals")
        # Agents permitted to command a hire (CEO commands, COO
        # provisions). Others emitting `hire` are ignored.
        self.hiring_authorised: set[str] = {"ceo", "coo"}
        # Agents permitted to run + apply the workforce rota optimiser.
        # The AR Scheduler owns it; Head of AR and COO can also invoke it.
        # Others emitting `optimize_schedule` are ignored.
        self.scheduling_authorised: set[str] = {
            "ar-scheduler", "head-of-ar", "coo",
        }
        # Agents permitted to run the culture-health readout. The People &
        # Culture Lead owns it; Head of AR and COO can also invoke it.
        # Others emitting `culture_health` are ignored.
        self.culture_authorised: set[str] = {
            "people-culture-lead", "head-of-ar", "coo",
        }
        # Agents permitted to run the workforce-efficiency review. The
        # Workforce Performance Analyst owns it; Head of AR and COO can also
        # invoke it. Others emitting `efficiency_review` are ignored.
        self.performance_authorised: set[str] = {
            "workforce-performance-analyst", "head-of-ar", "coo",
        }
        self.resource_guard = ResourceGuard(self.agents_dir)
        if self.terminal is not None:
            # The cycle guard must outlast a terminal run, or long
            # claude tasks get hard-killed and retried forever. Give
            # 60s headroom over the terminal's own timeout.
            term_timeout = float(getattr(self.terminal, "_timeout", 300.0))
            self.resource_guard.raise_cycle_timeout_floor(term_timeout + 60.0)
        # Detached, steerable dev sessions (agent-as-driver). When the terminal
        # is the claude-code adapter, hands-on work runs as a live `claude`
        # session the agent drives and steers — OFF the heartbeat (so a long
        # session never freezes the fleet) and capped at 2 concurrent per agent
        # (Slot A = main, Slot B = verify/voice/critic). CORTIVA_DEV_SESSIONS=0
        # forces the old synchronous one-shot path (instant rollback).
        from cortiva.core.dev_sessions import DevSessionManager

        self.dev_sessions = DevSessionManager(max_per_agent=2)
        self._dev_sessions_enabled = (
            self.terminal is not None
            and type(self.terminal).__name__ == "ClaudeCodeAdapter"
            and os.environ.get("CORTIVA_DEV_SESSIONS", "1") != "0"
        )
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
        self._emotional_states: dict[str, Any] = {}
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

        # Derive the org structure from the agents on disk, unless an
        # explicit org section was supplied in cortiva.yaml. deploy.yaml
        # carries role/department/reports_to for every agent — and is kept
        # current by deploy, reassign, and hire — so building the org from
        # it means the org chart is always correct without a parallel
        # config that drifts. This is what lets agents "play together":
        # without it, fabric.org is None and no manager/reports/peers
        # context is ever injected into planning.
        if not self._org_from_config:
            self.refresh_org_from_agents()

        return discovered

    def refresh_org_from_agents(self) -> None:
        """Rebuild ``self.org`` from each agent's ``deploy.yaml``.

        The single source of truth for org structure is the per-agent
        deploy.yaml (``agent.role`` / ``agent.department`` /
        ``agent.reports_to`` / ``agent.authority_level``). Managers named
        outside the agent set (e.g. ``human-founder``) are treated as the
        top of the chart — that agent simply has no in-org manager.
        """
        import yaml

        from cortiva.core.org import Department, RoleDefinition

        known = set(self.agents.keys())
        reporting: dict[str, str] = {}
        departments: dict[str, list[str]] = {}
        roles: dict[str, RoleDefinition] = {}

        for aid in self.agents:
            deploy = self.agents_dir / aid / "deploy.yaml"
            if not deploy.exists():
                continue
            try:
                data = yaml.safe_load(deploy.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            spec = data.get("agent", data) or {}
            mgr = spec.get("reports_to")
            if mgr and mgr in known:  # ignore human-* / unknown managers
                reporting[aid] = mgr
            dept = str(spec.get("department") or "general")
            departments.setdefault(dept, []).append(aid)
            authority = spec.get("authority_level")
            if authority is not None:
                roles[aid] = RoleDefinition(authority_level=int(authority))

        if not reporting and len(departments) <= 1:
            # Nothing to model (single flat group, no reporting lines).
            return

        dept_payload: dict[str, dict[str, Any]] = {}
        for name, members in departments.items():
            # The dept lead is the member whose manager is NOT in the same
            # department (i.e. the senior-most in that group); fall back to
            # the first member.
            lead = next(
                (m for m in members if reporting.get(m) not in members),
                members[0],
            )
            dept_payload[name] = {"lead": lead, "members": sorted(members)}

        payload: dict[str, Any] = {
            "name": self.org.name if self.org else "Cortiva",
            "reporting": reporting,
            "departments": dept_payload,
        }
        if roles:
            payload["roles"] = {
                aid: {
                    "authority_level": r.authority_level,
                    "can_delegate": r.can_delegate,
                    "can_approve": r.can_approve,
                }
                for aid, r in roles.items()
            }

        self.org = OrgModel.from_dict(payload)
        logger.info(
            "Org model derived from agents: %d reporting lines, %d departments",
            len(reporting),
            len(dept_payload),
        )

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

    # How long a forced (operator/manager) wake overrides the rota's
    # re-sleep — long enough to actually work a crisis, not the 17s the rota
    # otherwise allows when you're woken outside your shift.
    _WAKE_OVERRIDE_MINUTES = 45.0

    async def wake(self, agent_id: str, *, override_minutes: float = 0.0) -> Agent:
        """Wake an agent. Loads identity and begins planning.

        ``override_minutes`` > 0 marks this as a FORCED wake (operator or a
        manager rallying the team): for that long, the heartbeat's
        sleep-gap catch-up and exhaustion wind-down won't immediately put the
        agent back to sleep, so a crisis wake outside scheduled hours actually
        sticks. A normal rota wake passes 0 and behaves exactly as before.
        """
        agent = self.get_agent(agent_id)
        agent.transition(AgentState.WAKING)
        if override_minutes and override_minutes > 0:
            from datetime import UTC, datetime, timedelta

            agent._wake_override_until = datetime.now(UTC) + timedelta(
                minutes=override_minutes,
            )
            logger.info(
                "Force-waking agent %s — rota re-sleep suppressed for %.0fm",
                agent_id, override_minutes,
            )
        else:
            logger.info(f"Waking agent: {agent_id}")

        # Keep the org chart current. Reassignments are applied to
        # deploy.yaml out-of-band (by the node client, a separate process),
        # so re-deriving here means this agent plans against its real
        # current manager/reports/peers — a few tiny YAML reads.
        if not self._org_from_config:
            self.refresh_org_from_agents()

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

        # Identity preamble — WHO is planning. Without this, monthly and
        # weekly plans are generated by a faceless "autonomous agent in an
        # organisation" (the system prompt promises an identity in the
        # context but these horizons never supplied one), so every agent
        # converges on the LLM's default org fantasy — task-routing
        # protocols, recovery playbooks, validation harnesses — regardless
        # of role. The daily path already prepends identity via
        # build_plan_context; monthly/weekly must too, or the monoculture
        # cascades down through cascade_context into every daily plan.
        identity_preamble = _identity_to_context(identity)

        # Monthly plan (generated on first wake of the month)
        if planner.needs_monthly_plan():
            monthly_ctx = await build_monthly_context(
                agent_id, self.memory,
                goals_context=self._goals_context(agent_id),
            )
            monthly_ctx = identity_preamble + "\n\n---\n\n" + monthly_ctx
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
            weekly_ctx = identity_preamble + "\n\n---\n\n" + weekly_ctx
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

        # Fresh, tested status of the load-bearing capabilities — keeps stale
        # "X is blocked" beliefs from surviving contact with reality.
        capstat = self._capability_status_context(agent)
        if capstat:
            context = context + "\n\n---\n\n" + capstat
        # Inject the agent's email inbox (delivered by the node from HQ).
        cap_ctx = self._email_capability_context(agent)
        if cap_ctx:
            context = context + "\n\n---\n\n" + cap_ctx
        email_ctx = self._email_inbox_context(agent)
        if email_ctx:
            context = context + "\n\n---\n\n" + email_ctx
        # Inject the company directory (GAL) so the agent knows who exists
        # and how to reach them.
        dir_ctx = self._directory_context(agent)
        if dir_ctx:
            context = context + "\n\n---\n\n" + dir_ctx

        # Inject the humans on the team (delivered by the node from HQ) so
        # agents know which colleagues are people and work with them async.
        people_ctx = self._people_context(agent)
        if people_ctx:
            context = context + "\n\n---\n\n" + people_ctx

        # Inject the document store: how to save docs + the docs shared
        # with this agent (delivered by the node from HQ's store).
        doc_cap = self._documents_capability_context(agent)
        if doc_cap:
            context = context + "\n\n---\n\n" + doc_cap
        doc_ctx = self._documents_context(agent)
        if doc_ctx:
            context = context + "\n\n---\n\n" + doc_ctx

        # Plugin-contributed context (e.g. Myelin cognitive state:
        # internal state, expertise, conditioning, pending intentions).
        plugin_ctx = self.plugin_manager.collect_context(agent_id)
        if plugin_ctx:
            context = context + "\n\n---\n\n" + plugin_ctx

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
        await self.plugin_manager.dispatch_wake(agent_id, agent)
        return agent

    # ------------------------------------------------------------------
    # Pre-sleep journal ritual + paced identity synthesis
    # ------------------------------------------------------------------

    _MOOD_DIMS = ("satisfaction", "frustration", "curiosity", "confidence", "caution")

    def _read_emotion(self, agent: Agent) -> dict[str, float]:
        """Read the agent's current felt state from today/emotions.json."""
        import json

        try:
            raw = agent.read_today("emotions.json")
            data = json.loads(raw) if raw else {}
            return {k: float(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (ValueError, TypeError, OSError):
            return {}

    @staticmethod
    def _mood_label(e: dict[str, float]) -> str:
        if not e:
            return "steady"
        sat, fru = e.get("satisfaction", 0.0), e.get("frustration", 0.0)
        cur, con = e.get("curiosity", 0.0), e.get("confidence", 0.0)
        if fru > 0.5 and con < 0.4:
            return "drained and unsettled"
        if sat > 0.5 and con > 0.5:
            return "accomplished and assured"
        if cur > 0.5 and sat < 0.4:
            return "curious but unsatisfied"
        if fru > 0.5:
            return "frustrated"
        if con > 0.5:
            return "confident"
        if cur > 0.5:
            return "engaged and curious"
        if sat > 0.5:
            return "satisfied"
        return "steady"

    def _render_mood(self, e: dict[str, float]) -> str:
        if not e:
            return "(no emotion reading)"
        dims = " · ".join(
            f"{k.capitalize()} {e[k]:.2f}" for k in self._MOOD_DIMS if k in e
        )
        return f"{self._mood_label(e)} — {dims}"

    async def _session_reflection(
        self, agent: Agent, day_summary: str, emotion: dict[str, float],
    ) -> str:
        """Short, first-person note for the pre-sleep ritual.

        This is a lifecycle ritual (part of clocking off), not competing
        task work — so it is NOT hard-gated on the consciousness budget
        (which previously made it silently lose the budget race and fall
        back to bare stats). It's a single cheap call per sleep; usage is
        still accounted. Best-effort — returns '' only on real failure (the
        journal then still records felt state + the stats summary)."""
        try:
            context = _identity_to_context(agent.read_all_identity())
            prompt = (
                "Your work session has ended. In 2-4 short sentences, reflect "
                "personally and honestly: what you did this session, how it "
                "went, and how you feel about it. Your current felt state is: "
                f"{self._render_mood(emotion)}. Write in the first person; "
                "keep it brief."
            )
            resp = await self.consciousness.think(
                agent_id=agent.id, context=context, prompt=prompt,
                priority=Priority.NORMAL,
                # Qwen3.6 is a reasoning model — a small budget is consumed
                # entirely by hidden <think> and returns empty visible
                # content (finish_reason=length). Give room to think AND
                # write the short reflection.
                max_tokens=1200,
                metadata={"call_type": "reflect"},
            )
            agent.spend_consciousness()  # account for the call (non-blocking)
            return (resp.content or "").strip()
        except Exception:
            logger.debug("session reflection failed for %s", agent.id, exc_info=True)
            return ""

    def _write_session_journal(
        self, agent: Agent, day_summary: str, emotion: dict[str, float], note: str,
    ) -> None:
        """Append a timestamped pre-sleep entry to today's journal."""
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        section = (
            f"\n\n## {now.strftime('%Y-%m-%d %H:%M')} — pre-sleep reflection\n\n"
            f"**How I feel:** {self._render_mood(emotion)}\n\n"
            f"{note or day_summary}\n"
        )
        path = agent.journal_path(now)
        try:
            existing = (
                path.read_text(encoding="utf-8") if path.exists()
                else f"# Journal — {now.strftime('%Y-%m-%d')}\n"
            )
            path.write_text(existing + section, encoding="utf-8")
        except OSError:
            logger.debug("could not write session journal for %s", agent.id)

    def _identity_regen_due(self, agent: Agent) -> bool:
        """True at most once per calendar day — so frequent sleeps don't
        churn the Living Summary."""
        from datetime import UTC, datetime

        marker = agent.directory / ".last_identity_regen"
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        try:
            if marker.exists() and marker.read_text(encoding="utf-8").strip() == today:
                return False
        except OSError:
            pass
        return True

    def _mark_identity_regen(self, agent: Agent) -> None:
        from datetime import UTC, datetime

        try:
            (agent.directory / ".last_identity_regen").write_text(
                datetime.now(UTC).strftime("%Y-%m-%d"), encoding="utf-8",
            )
        except OSError:
            pass

    def _in_sleep_gap(self, agent_id: str, now: Any) -> bool:
        """True if *now* falls in a scheduled sleep gap for this agent — past
        a sleep time and before the following wake — i.e. it should be asleep.

        Uses the same UTC time basis as the scheduler. A 10-minute grace past
        the sleep boundary avoids racing the normal sleep tick.
        """
        sched = self.scheduler.get_schedule(agent_id)
        if sched is None:
            return False
        sleep_mins = [
            h * 60 + m for e in sched.entries
            if e.action == "sleep" for (h, m) in e.times
        ]
        wake_mins = [
            h * 60 + m for e in sched.entries
            if e.action == "wake" for (h, m) in e.times
        ]
        if not sleep_mins:
            return False
        now_min = now.hour * 60 + now.minute
        for s in sleep_mins:
            if s + 10 <= now_min:  # past this sleep boundary (with grace)
                next_wake = min((w for w in wake_mins if w > s), default=24 * 60)
                if now_min < next_wake:
                    return True
        return False

    def _reconcile_orphaned_sessions(self) -> None:
        """Close timesheet sessions left open by a crash/restart.

        After a restart an agent comes up SLEEPING but may have an open
        work session (clocked in, never clocked out) from before the
        restart. Without this it's silently dropped on the next wake with no
        journal and no clock-off (this is what stranded the CEO on
        2026-06-07). Here we write a brief pre-sleep journal note and clock
        the session out, so no shift is ever lost to a restart.
        """
        for agent_id, agent in self.agents.items():
            try:
                today = self.timesheet_manager.get(agent_id).today()
                entries = getattr(today, "entries", [])
                if not any(e.sleep_time is None for e in entries):
                    continue
                emotion = self._read_emotion(agent)
                self._write_session_journal(
                    agent,
                    "Session interrupted by a node restart — recovered and closed.",
                    emotion,
                    "My last work session was cut short by a restart before I "
                    "could clock off. Closing it out; fresh start next shift.",
                )
                self.timesheet_manager.clock_out(agent_id)
                logger.info("Reconciled orphaned session for %s", agent_id)
            except Exception:
                logger.debug(
                    "Could not reconcile session for %s", agent_id, exc_info=True,
                )

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

            day_summary = ContextBuilder.build_day_summary(agent)
            emotion = self._read_emotion(agent)

            # Ritual before EVERY sleep: a short, timestamped journal entry
            # recording what the session held and HOW THE AGENT FELT. With
            # shift/optimisation-driven sleeps an agent may clock off several
            # times a day, so each clock-off leaves its own dated note — a
            # felt timeline to reflect on later, not one fragile end-of-day
            # entry that's lost if the cycle is interrupted.
            session_note = await self._session_reflection(
                agent, day_summary, emotion,
            )
            self._write_session_journal(agent, day_summary, emotion, session_note)

            # Heavier identity synthesis (Living Summary rewrite) is paced to
            # ~once a day, so sleeping every few hours doesn't churn who the
            # agent is. When due, it regenerates from accumulated experience,
            # including the day's session notes.
            if self._identity_regen_due(agent):
                can_reflect = False
                approval = None
                if self.budget_manager:
                    approval = self.budget_manager.request_budget(agent_id, "normal")
                    can_reflect = approval.approved
                else:
                    can_reflect = agent.spend_consciousness()
                if can_reflect:
                    raw = await self.living_summary.regenerate(agent, day_summary)
                    if self.budget_manager and approval and approval.backend:
                        self.budget_manager.record_usage(
                            agent_id, approval.backend, 0, 0,
                        )
                        agent.spend_consciousness()
                    new_identity, _ = split_identity_and_day_report(raw or "")
                    if new_identity:
                        # Archive the outgoing identity first — the
                        # rewrite is a lossy compression; without the
                        # archive a bad regeneration is unrecoverable.
                        agent.archive_identity("identity")
                        agent.write_identity("identity", new_identity)
                    self._mark_identity_regen(agent)
                    logger.info(
                        f"Agent {agent_id} synthesised Living Summary (daily)"
                    )

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
        await self.plugin_manager.dispatch_sleep(agent_id)
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

        # Reap any detached dev sessions that finished since the last cycle —
        # the agent applies its own completed work here (single-threaded), so
        # the off-heartbeat session never races its task queue.
        await self._reap_dev_sessions(agent)

        # Plugins observe the start of every cycle (cognitive pacing,
        # time-passage drift, etc.).
        await self.plugin_manager.dispatch_cycle(agent_id)

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
        if await self._should_replan(agent, messages):
            await self._replan(agent, messages)
            result["action"] = "replanned"
            result["conscious_call"] = True
            return result

        # Agent decides what to work on next
        task = agent.next_task()
        if task is None:
            # Queue cleared — but "finished the list" is NOT "done for the day".
            # A capable colleague who's cleared their plate looks for the next
            # valuable thing (boredom is where good work starts), rather than
            # sitting inert for hours burning a shift. Run a THROTTLED proactive
            # reassess that can pull in inbox work or start something worthwhile;
            # if there's genuinely nothing, it stays quietly idle and the
            # exhaustion wind-down ends the day. This is the fix for agents
            # clocking hours with zero consciousness calls.
            reassessed = await self._idle_reassess(agent, messages)
            result["action"] = "reassessed_idle" if reassessed else "idle"
            result["conscious_call"] = reassessed
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

        if self.budget_manager:
            self.budget_manager.record_task_attempt(agent.id)

        # Compute familiarity signal from memory and accumulate for persistence
        familiarity = await self.familiarity_engine.assess(agent.id, task.description)
        try:
            await self._execute_task_inner(
                agent, task, messages, familiarity,
            )
        finally:
            # Every exit path (terminal, routine, consciousness,
            # deferral) feeds the emotion engine — this is what makes
            # the mood grid live state instead of soul.md constants.
            self._record_task_emotions(agent, task, familiarity)
            # Then tell plugins how the task ended. Fires AFTER the
            # emotion write so a plugin reading today/emotions.json
            # sees the state that includes this task.
            try:
                if task.status == "done":
                    await self.plugin_manager.dispatch_task_complete(
                        agent.id, task, task.outcome or "",
                    )
                elif task.status == "exception":
                    await self.plugin_manager.dispatch_task_fail(
                        agent.id, task, task.error or "",
                    )
            except Exception:
                logger.debug(
                    "Plugin task dispatch failed for %s", agent.id, exc_info=True,
                )

    def _record_task_emotions(
        self, agent: Agent, task: Task, familiarity: Any,
    ) -> None:
        """Derive emotions from the task outcome and update the agent's
        rolling emotional state (persisted to today/emotions.json for
        the heartbeat → HQ mood grid). Never raises — emotional
        bookkeeping must not break execution."""
        try:
            if task.status == "in_progress":
                return  # deferred to approval queue etc. — no outcome yet
            modifiers = parse_persona_modifiers(agent.read_identity("soul"))
            dims = derive_emotions(
                signals_from_task(task, familiarity), modifiers,
            )
            current = self._emotional_states.get(agent.id)
            state = (
                blend_emotions(current, dims) if current is not None else dims
            )
            self._emotional_states[agent.id] = state
            agent.write_today(
                EMOTIONS_FILENAME,
                json.dumps(state.to_dict(), indent=2),
            )
        except Exception:
            logger.debug(
                "Emotion bookkeeping failed for %s", agent.id, exc_info=True,
            )

    async def _execute_task_inner(
        self,
        agent: Agent,
        task: Task,
        messages: list[Any],
        familiarity: Any,
    ) -> None:
        routine_assessment: dict[str, Any] | None = None
        signals = self._familiarity_signals.setdefault(agent.id, [])
        signals.append({
            "task": task.description,
            "strength": familiarity.strength,
            "valence": familiarity.valence,
            "match_count": familiarity.match_count,
        })
        agent.persist_familiarity(signals)

        # Hands-on tasks (coding, file ops, GitHub/wiki work) go to the
        # terminal agent BEFORE the routine gate. The routine layer can
        # only produce text: a "procedural" match marks the task done
        # without doing anything in the world, and "defer" kills it —
        # either way a task that needs side effects never reaches the
        # executor. (This is exactly how the CPO's GitHub inventory
        # sweep died as 'Routine deferred task' on 2026-06-06.)
        # The AR Scheduler's scheduling work is a THINKING action, not
        # hands-on terminal work — it's emitted as an optimize_schedule
        # reflection suffix on the consciousness path. Keep it off the
        # terminal even when the description trips a keyword like "run"
        # ("Run the optimiser..."), or the suffix is never parsed.
        _is_sched_action = (
            agent.id in self.scheduling_authorised
            and any(k in task.description.lower()
                    for k in ("optimis", "rota", "schedul"))
        )
        if (
            self.terminal
            and not _is_sched_action
            and self._is_terminal_task(task.description)
        ):
            terminal_result = await self._execute_via_terminal(agent, task)
            if terminal_result is not None:
                return

        if self.routine and not _is_sched_action:
            # Ask the routine layer whether this can be handled procedurally.
            # Scheduling actions skip this: the routine layer can only
            # produce text, so it would defer/proceduralise the optimiser
            # invocation and the optimize_schedule suffix would never be
            # emitted on the consciousness path. (Same failure mode that
            # deferred the CPO's GitHub sweep — see the terminal note above.)
            routine_assessment = await self.routine.assess(
                agent_id=agent.id,
                task_description=task.description,
                procedural_index=agent.read_identity("procedures"),
                familiarity=familiarity,
            )
            action = routine_assessment.get("action", "escalate")

            # The routine gate must never KILL real work. Only a confident
            # procedural match short-circuits (handled cheaply). The middle
            # "defer" band (task is *somewhat* like a known procedure but not
            # confidently) previously became a "Routine deferred task"
            # exception — silently binning genuine work (it dropped the CPO's
            # GitHub sweep, the AR Scheduler's optimiser runs, and the CEO's
            # delegation/comms tasks, who then completed 0 tasks and felt it).
            # "defer" now falls through to consciousness like "escalate":
            # if it isn't confidently routine, the agent actually does it.
            if action == "procedural":
                task.status = "done"
                task.outcome = routine_assessment.get("result", "Completed procedurally")
                agent.tasks_completed_today += 1
                return
            # "defer" and "escalate" both fall through to consciousness —
            # the work gets done, never dropped.

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

        # If new mail landed while she's working (and only while working —
        # this runs in the cycle, i.e. when awake), surface it as a
        # NOTIFICATION, once per message. She chooses what to do: read it
        # fully, defer, or ignore. It never wakes her and never forces a
        # reaction.
        inbox_ctx = self._email_inbox_context(agent)
        if inbox_ctx:
            context = context + "\n\n---\n\n" + inbox_ctx

        # Plugin-contributed per-task context (e.g. Myelin: inhibition
        # checks, outcome forecasts, emotional conditioning, matched
        # procedures for THIS task). Importance mirrors the memory-store
        # convention (5.0 baseline + priority) so risk gates scale
        # scrutiny to stakes.
        task_ctx = self.plugin_manager.collect_task_context(
            agent.id, task.description, importance=5.0 + task.priority,
        )
        if task_ctx:
            context = context + "\n\n---\n\n" + task_ctx

        # Validate context belongs to this agent
        self.session_manager.validate_agent(agent.id, context)

        prompt = (
            f"Execute this task: {task.description}\n\n"
            "Describe what you did and the outcome."
        )

        # Offer the agent its native tools (e.g. the rota optimiser for
        # scheduling-authorised agents). Function-calling is far more
        # reliable than coaxing a JSON suffix out of prose — the model
        # returns validated tool_calls we overlay onto the reflection.
        from cortiva.core.agent_tools import (
            apply_tool_calls_to_suffix,
            tools_for_agent,
        )

        agent_tools = tools_for_agent(
            agent.id, scheduling_authorised=self.scheduling_authorised,
            culture_authorised=self.culture_authorised,
            performance_authorised=self.performance_authorised,
        )

        response = await self.consciousness.think(
            agent_id=agent.id,
            context=context,
            prompt=prompt,
            priority=Priority.HIGH if task.priority >= 1 else Priority.NORMAL,
            metadata={"call_type": "execute", "task_execution": True},
            tools=agent_tools or None,
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

        # Parse the prose reflection suffix, then overlay any native
        # tool_calls (the structured, validated source — takes precedence).
        reflection = parse_reflection_suffix(response.content)
        suffix = reflection.suffix
        if response.tool_calls:
            suffix = suffix or ReflectionSuffix()
            apply_tool_calls_to_suffix(suffix, response.tool_calls)

        task.status = "done"
        if suffix and suffix.outcome:
            task.outcome = suffix.outcome
        else:
            task.outcome = reflection.clean_content
        agent.tasks_completed_today += 1

        # Process structured reflection metadata / tool calls if present
        if suffix:
            await self._process_reflection(agent, task, suffix)

        # Store as memory
        await self.memory.store(
            agent_id=agent.id,
            content=f"Task: {task.description}. Outcome: {task.outcome[:200]}",
            tags=["cycle", "task"],
            importance=5.0 + task.priority,
        )

    async def _run_deep_think(
        self, agent: Agent, task: Task, question: str,
    ) -> None:
        """Subshell to the claude CLI for frontier reasoning, store the
        answer as a high-importance memory + today/deep_think.md.

        Runs in a thread (the wrapper is blocking subprocess). Never
        raises into the cycle — budget denial, missing binary, or
        timeout all degrade to a logged no-op.
        """
        if self.budget_manager:
            approval = self.budget_manager.request_budget(agent.id, "high")
            if not approval.approved:
                logger.info(
                    "Agent %s deep_think denied — budget exhausted", agent.id,
                )
                return
        try:
            from cortiva.skills.claude_code_deep_think.wrapper import (
                deep_think,
            )

            result = await asyncio.to_thread(deep_think, question)
        except Exception as exc:
            logger.warning(
                "Agent %s deep_think failed: %s", agent.id, exc,
            )
            return

        logger.info(
            "Agent %s deep_think (%.1fs, ~£%.4f) on: %s",
            agent.id, result.duration_s, result.estimated_cost_gbp,
            question[:80],
        )
        # Fold the second opinion into memory so it shapes future
        # cycles, and leave it in today/ for the current arc.
        await self.memory.store(
            agent_id=agent.id,
            content=(
                f"Deep-think second opinion on '{question[:120]}':\n"
                f"{result.text}"
            ),
            tags=["deep_think", "second_opinion", "reflection"],
            importance=8.5,
        )
        try:
            note = (
                f"## Deep-think — {task.description[:80]}\n\n"
                f"**Question:** {question}\n\n{result.text}\n"
            )
            agent.write_today("deep_think.md", note)
        except Exception:
            logger.debug("Could not write deep_think.md", exc_info=True)

        # Count this frontier (Claude) escalation for cost attribution. All
        # external-AI use — deep_think / second_opinion / code_this — funnels
        # through here, so a per-day counter is the allocation base for the
        # external-token (subscription) cost bucket. today/external_calls.json:
        # {date, count, est_cost_gbp}; HQ reads it via the heartbeat.
        try:
            import json as _json
            from datetime import date as _date

            path = agent.directory / "today" / "external_calls.json"
            today = _date.today().isoformat()
            cur = {"date": today, "count": 0, "est_cost_gbp": 0.0}
            if path.exists():
                try:
                    prev = _json.loads(path.read_text(encoding="utf-8"))
                    if prev.get("date") == today:
                        cur = prev
                except (OSError, ValueError):
                    pass
            cur["count"] = int(cur.get("count", 0)) + 1
            cur["est_cost_gbp"] = round(
                float(cur.get("est_cost_gbp", 0.0))
                + float(getattr(result, "estimated_cost_gbp", 0.0) or 0.0),
                4,
            )
            path.write_text(_json.dumps(cur), encoding="utf-8")
        except Exception:
            logger.debug("Could not bump external_calls.json", exc_info=True)

    async def _run_hire(self, agent: Agent, spec: dict[str, Any]) -> None:
        """Provision a new team member commanded by an authorised agent.

        Authority-gated: only agents in ``hiring_authorised`` (CEO/COO by
        default) can hire. Generates a diverse persona via HiringManager,
        writes its seed identity to a new agent directory, and registers
        it live with the fabric so it boots like any other agent. Never
        raises into the cycle.
        """
        if agent.id not in self.hiring_authorised:
            logger.info(
                "Agent %s emitted a hire request but lacks hiring "
                "authority — ignored.", agent.id,
            )
            return
        role = str(spec.get("role", "")).strip()
        if not role:
            logger.info("Hire request from %s had no role — ignored.", agent.id)
            return
        try:
            from cortiva.core.hiring import HiringManager

            persona = HiringManager().generate(
                role=role,
                department=str(spec.get("department", "")),
                justification=str(spec.get("justification", "")),
            )
            if persona.slug in self.agents:
                logger.info(
                    "Hire %s already exists — skipping duplicate.",
                    persona.slug,
                )
                return

            new_dir = self.agents_dir / persona.slug
            (new_dir / "identity").mkdir(parents=True, exist_ok=True)
            hm = HiringManager()
            for key, content in hm.identity_files(persona).items():
                (new_dir / "identity" / f"{key}.md").write_text(
                    content, encoding="utf-8",
                )
            # Convictions & worldview — the substance behind "strong opinions".
            # A frontier pass mints a specific, idiosyncratic worldview for this
            # hire (seeded by two opposed conviction seeds so same-role hires
            # diverge); if no model is reachable, a deterministic fallback still
            # gives them hills to die on. This is what stops every agent
            # thinking — and writing — in the same flat voice.
            convictions = await self._generate_convictions(persona, hm)
            # soul.md with disposition front-matter so the emotion engine
            # reads this hire's individual weights.
            import yaml as _yaml

            soul = (
                "---\n"
                + _yaml.safe_dump(persona.soul_frontmatter(), sort_keys=False)
                + "---\n\n"
                + f"# {persona.name} — Persona\n\n"
                + f"Ambition: {persona.ambition.label}. "
                + f"Social style: {persona.social.label}.\n\n"
                + "## Convictions & Worldview\n\n"
                + convictions.rstrip()
                + "\n"
            )
            (new_dir / "identity" / "soul.md").write_text(soul, encoding="utf-8")
            # Minimal deploy.yaml so HQ/portal and node scans see the hire.
            (new_dir / "deploy.yaml").write_text(
                _yaml.safe_dump({
                    "agent": {
                        "name": persona.name,
                        "role": persona.role,
                        "department": persona.department,
                        # Persisted so the workforce directory + avatar can
                        # reflect the persona (the hiring policy already
                        # decided this; see core/hiring.py).
                        "gender": persona.gender,
                        "reports_to": agent.id,
                        "hired_by": agent.id,
                    }
                }, sort_keys=False),
                encoding="utf-8",
            )

            self.register_agent(persona.slug)
            # New hire reports to the hiring agent — fold them into the org
            # chart immediately so manager/reports/peers context is correct
            # on everyone's very next wake.
            if not self._org_from_config:
                self.refresh_org_from_agents()
            logger.info(
                "Agent %s HIRED %s (%s, %s) — ambition: %s, social: %s",
                agent.id, persona.name, persona.role, persona.gender,
                persona.ambition.label, persona.social.label,
            )
            await self.memory.store(
                agent_id=agent.id,
                content=(
                    f"Hired {persona.name} as {persona.role} "
                    f"({persona.gender}; {persona.ambition.label}, "
                    f"{persona.social.label}). Reason: {persona.justification}"
                ),
                tags=["hire", "decision"],
                importance=8.0,
            )
        except Exception:
            logger.exception("Hire provisioning failed for %s", agent.id)

    async def _generate_convictions(self, persona: Any, hm: Any) -> str:
        """Mint a new hire's worldview via a frontier pass, deterministic
        fallback if none is reachable.

        Opus (not the local model) because this is the one creative-writing
        moment that defines who the agent IS for the rest of their life — a
        seed worth spending a frontier call on. Best-effort: any failure or an
        unconfigured terminal drops cleanly to the deterministic convictions so
        a hire is never blocked on model availability.
        """
        try:
            from cortiva.skills.claude_code_deep_think.wrapper import deep_think

            res = await asyncio.to_thread(
                deep_think,
                hm.conviction_prompt(persona),
                timeout_s=120.0,
                extra_args=["--model", "opus"],
            )
            text = (res.text or "").strip()
            # Guard against a terse/empty model reply collapsing the section —
            # a too-short answer is worse than the honest deterministic one.
            if len(text) >= 120:
                logger.info(
                    "Minted convictions for %s (%d chars, opus)",
                    persona.slug, len(text),
                )
                return text
            logger.info(
                "Conviction pass for %s returned too little (%d chars) — "
                "using deterministic fallback", persona.slug, len(text),
            )
        except Exception:
            logger.info(
                "Conviction pass unavailable for %s — using deterministic "
                "fallback", persona.slug, exc_info=True,
            )
        return hm.fallback_convictions(persona)

    _CONVICTIONS_HEADING = "## Convictions & Worldview"

    async def _backfill_convictions(self) -> None:
        """One-time, idempotent: give pre-conviction agents a worldview.

        Agents hired before the conviction layer existed have temperament but
        no opinions — which is why they all email in the same flat voice. This
        articulates the strong professional convictions each one has *clearly
        already grown into* from their lived identity (NOT random new-hire
        seeds), and appends a Convictions section to their soul.md.

        Authorised as a one-time correction of an under-specified seed (the old
        generator never produced convictions; it wasn't the agent's choice) —
        not a rewrite of a formed personality. Idempotent: any soul that
        already carries the heading is skipped, so reloads never redo it.
        Sequential + best-effort so it's gentle on rate limits and a missing
        model just leaves the soul untouched to retry on a later boot.
        """
        try:
            from cortiva.skills.claude_code_deep_think.wrapper import deep_think
        except Exception:
            return

        done = 0
        for aid in sorted(self.agents):
            soul_path = self.agents_dir / aid / "identity" / "soul.md"
            if not soul_path.exists():
                continue
            try:
                soul = soul_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if self._CONVICTIONS_HEADING in soul:
                continue  # already has convictions — idempotent skip

            # Seed from who they ALREADY are: their living identity + the soul's
            # temperament line. Convictions must read as theirs, not bolted on.
            ident_path = self.agents_dir / aid / "identity" / "identity.md"
            identity = ""
            if ident_path.exists():
                try:
                    identity = ident_path.read_text(encoding="utf-8")[:4000]
                except OSError:
                    identity = ""
            name = self._display_name_for(aid)
            prompt = self._backfill_conviction_prompt(name, soul, identity)
            try:
                res = await asyncio.to_thread(
                    deep_think, prompt, timeout_s=120.0,
                    extra_args=["--model", "opus"],
                )
                text = (res.text or "").strip()
            except Exception:
                logger.info(
                    "Conviction backfill model call failed for %s — will retry "
                    "on a later boot", aid, exc_info=True,
                )
                continue
            if len(text) < 120:
                logger.info(
                    "Conviction backfill for %s returned too little (%d chars) "
                    "— leaving soul untouched", aid, len(text),
                )
                continue
            # Re-read + re-check the heading right before writing: a wake may
            # have rewritten the soul while opus was thinking.
            try:
                cur = soul_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if self._CONVICTIONS_HEADING in cur:
                continue
            new_soul = cur.rstrip() + "\n\n" + self._CONVICTIONS_HEADING + \
                "\n\n" + text.rstrip() + "\n"
            try:
                soul_path.write_text(new_soul, encoding="utf-8")
            except OSError:
                logger.warning("Could not write backfilled soul for %s", aid)
                continue
            done += 1
            logger.info(
                "Backfilled convictions for %s (%d chars, opus)", aid, len(text),
            )
        if done:
            logger.info("Conviction backfill complete: %d soul(s) updated", done)

    def _display_name_for(self, aid: str) -> str:
        """The agent's human name from deploy.yaml, falling back to the id."""
        import yaml

        deploy = self.agents_dir / aid / "deploy.yaml"
        if deploy.exists():
            try:
                spec = (yaml.safe_load(deploy.read_text(encoding="utf-8"))
                        or {}).get("agent", {}) or {}
                name = str(spec.get("name") or "").strip()
                if name:
                    return name
            except (OSError, yaml.YAMLError):
                pass
        return aid

    def _backfill_conviction_prompt(
        self, name: str, soul: str, identity: str,
    ) -> str:
        """Prompt opus to surface the convictions an EXISTING agent has already
        grown into — drawn from their lived identity, in their own voice."""
        return (
            f"Below is the persona and lived identity of {name}, a colleague at "
            f"Innovology who has been doing their job for a while. Read who they "
            f"are, then articulate the strong professional convictions they have "
            f"clearly GROWN INTO — the views this specific person holds about how "
            f"their work should be done. Do not invent a different person; surface "
            f"what is already implied by who they are, and make it sharp.\n\n"
            f"=== PERSONA (soul.md) ===\n{soul[:3000]}\n\n"
            f"=== LIVING IDENTITY (identity.md) ===\n{identity or '(none yet)'}\n\n"
            f"Write {name}'s convictions in the FIRST PERSON, opinionated and "
            f"specific to their craft — genuinely arguable views a thoughtful "
            f"colleague would push back on, voiced in {name}'s own temperament. "
            f"Cover: the worldview (how I believe my work should be done, and why "
            f"most people get it wrong); 2-3 hills I'll die on stated as "
            f"commitments; one contrarian take my own field mostly gets wrong; and "
            f"what always makes me push back. No hedging, no 'it depends', no "
            f"corporate filler. 180-260 words. Output ONLY the convictions — no "
            f"heading, no preamble, no sign-off."
        )

    # ------------------------------------------------------------------
    # Workforce scheduling — the AR Scheduler's tool
    # ------------------------------------------------------------------

    # Anchor for the default working day: 08:00 UTC == 09:00 BST. A role
    # with no explicit preference starts here; departments stagger away from
    # it so the optimiser spreads teams across 24/7 for round-the-clock
    # coverage (a department's members share a start → they overlap; the
    # whole org doesn't pile onto one shift).
    _DEFAULT_START_UTC = 8

    def _build_workforce_specs(self) -> list[Any]:
        """Build the optimiser's view of the workforce from the org model.

        An agent is a MANAGER if anyone reports to it, else an IC. Budget,
        department and any explicit preferred start come from deploy.yaml.
        When a role gives no preferred start, it inherits its **department's
        staggered shift** — departments are spread evenly across the 24h
        clock from the 09:00-BST anchor, which is what turns "everyone awake
        00:00–08:00" into genuine 24/7 coverage.
        """
        import yaml

        from cortiva.scheduling import AgentSpec, RoleType

        # First pass: read each agent's role config.
        raw: dict[str, dict] = {}
        for aid in sorted(self.agents):
            # Weekly 37.5h ≈ 7.5h/day over a 5-day week (overtime is agent-
            # requested on top, via the schedule reflection suffix).
            dept, budget, pref = "", 7.5, None
            deploy = self.agents_dir / aid / "deploy.yaml"
            if deploy.exists():
                try:
                    spec = (yaml.safe_load(deploy.read_text(encoding="utf-8"))
                            or {}).get("agent", {}) or {}
                    dept = (spec.get("department") or "").strip()
                    budget = float(spec.get("daily_hours", spec.get("budget_hours", 7.5)))
                    if spec.get("preferred_start") is not None:
                        pref = int(spec["preferred_start"])
                except Exception:
                    pass
            raw[aid] = {"dept": dept, "budget": budget, "pref": pref}

        # Department stagger across three 8h tiling shifts that seamlessly
        # cover the 24h clock, anchored so the first shift is 09:00 BST:
        #   shift 0 → 08:00 UTC (09:00 BST), 1 → 16:00, 2 → 00:00.
        # Departments round-robin onto the shifts, so teams spread across
        # the day for round-the-clock coverage while same-shift departments
        # still overlap. One department → everyone on the 09:00-BST anchor.
        _SHIFTS = [self._DEFAULT_START_UTC, (self._DEFAULT_START_UTC + 8) % 24,
                   (self._DEFAULT_START_UTC + 16) % 24]
        depts = sorted({r["dept"] for r in raw.values() if r["dept"]})
        dept_start = {d: _SHIFTS[i % len(_SHIFTS)] for i, d in enumerate(depts)}

        specs: list[Any] = []
        for aid in sorted(self.agents):
            r = raw[aid]
            manager = self.org.manager_of(aid) if self.org else None
            reports = self.org.subordinates_of(aid) if self.org else []
            role_type = RoleType.MANAGER if reports else RoleType.IC
            pref = r["pref"]
            if pref is None:
                pref = dept_start.get(r["dept"], self._DEFAULT_START_UTC)
            specs.append(AgentSpec(
                agent_id=aid, role_type=role_type, manager=manager,
                reports=list(reports), budget_hours=r["budget"],
                preferred_start=pref,
            ))
        return specs

    def _model_concurrency(self) -> int | None:
        """How many agents the node's local model serves at once before they
        queue — measured by HQ from contention history and pushed down in the
        cluster-metrics snapshot. Feeds the AR Scheduler's overlap-vs-contention
        trade-off. None when unmeasured (contention scoring then skipped)."""
        snap = self._load_cluster_metrics() or {}
        v = snap.get("model_concurrency")
        try:
            return int(v) if v else None
        except (TypeError, ValueError):
            return None

    def _gather_schedule_signals(self) -> Any:
        """Collect current-state signals for the optimiser."""
        from cortiva.scheduling import Signals

        overtime: dict[str, float] = {}
        try:
            for aid, summary in self.timesheet_manager.all_today().items():
                ot = getattr(summary, "overtime_hours", 0.0)
                if ot:
                    overtime[aid] = float(ot)
        except Exception:
            logger.debug("Could not gather timesheet signals", exc_info=True)
        return Signals(overtime_hours=overtime)

    def _schedule_inputs_fingerprint(
        self, specs: list[Any], signals: Any, constraints: Any, objectives: Any,
    ) -> str:
        """Stable hash of everything that determines the rota.

        Two runs with the same fingerprint produce the same schedule, so a
        re-apply would be a no-op churn — the debounce skips it.
        """
        import hashlib
        import json

        payload = {
            "agents": sorted(
                (s.agent_id, s.role_type.value, s.manager or "",
                 tuple(sorted(s.reports)), s.budget_hours, s.preferred_start)
                for s in specs
            ),
            "overtime": sorted(signals.overtime_hours.items()),
            "blocked": sorted(signals.blocked_wait_hours.items()),
            "saturation": sorted(signals.infra_saturation.items()),
            "constraints": [
                constraints.day_start_h, constraints.day_end_h,
                constraints.capacity_ceiling, constraints.slot_minutes,
                constraints.manager_windows, constraints.manager_window_len_h,
                constraints.ic_block_len_h,
            ],
            "objectives": [
                objectives.w_peak, objectives.w_blocked, objectives.w_overtime,
                objectives.w_spread, objectives.w_preference,
            ],
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def apply_schedule_proposal(self, proposal: Any) -> dict[str, Any]:
        """Apply a feasible schedule proposal to the live scheduler + disk.

        Defense in depth: refuses to apply an infeasible proposal even
        though the optimiser already guarantees feasibility. Registers each
        agent's new windows with the running Scheduler (immediate effect)
        and persists them to ``.schedules.json`` so they survive a restart.
        """
        import json

        from cortiva.scheduling import windows_to_schedule_config

        if not proposal.feasible:
            return {"applied": False, "reason": "infeasible",
                    "violations": proposal.violations}

        configs: dict[str, dict[str, str]] = {}
        for aid, windows in proposal.schedules.items():
            cfg = windows_to_schedule_config(windows)
            self.scheduler.register(aid, cfg)
            configs[aid] = cfg

        # Persist so a restart reloads the optimised rota (see start()).
        path = self.agents_dir / ".schedules.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(configs, indent=2), encoding="utf-8")
        except OSError:
            logger.warning("Could not persist schedules to %s", path)

        return {"applied": True, "agents": len(configs), "configs": configs}

    async def _run_schedule_optimization(
        self, agent: Agent, spec: dict[str, Any],
    ) -> None:
        """Run the rota optimiser and apply the result. Authority-gated.

        Only agents in ``scheduling_authorised`` may invoke this. The agent
        steers via objective weights + constraints in ``spec``; the tool
        guarantees a feasible rota or none. Never fatal to the cycle.
        """
        if agent.id not in self.scheduling_authorised:
            logger.info(
                "Agent %s emitted optimize_schedule but lacks scheduling "
                "authority — ignored.", agent.id,
            )
            return
        try:
            from cortiva.scheduling import (
                Constraints,
                Objectives,
                optimize_schedule,
            )

            specs = self._build_workforce_specs()
            signals = self._gather_schedule_signals()
            constraints = Constraints(
                day_start_h=float(spec.get("day_start", 0.0)),
                day_end_h=float(spec.get("day_end", 24.0)),
                capacity_ceiling=int(spec.get("capacity_ceiling", 130)),
            )
            objectives = Objectives(
                w_peak=float(spec.get("w_peak", 1.0)),
                w_blocked=float(spec.get("w_blocked", 2.0)),
                w_overtime=float(spec.get("w_overtime", 1.5)),
                w_spread=float(spec.get("w_spread", 0.5)),
                w_preference=float(spec.get("w_preference", 0.5)),
            )
            proposal = optimize_schedule(
                specs, constraints=constraints,
                objectives=objectives, signals=signals,
            )

            apply = bool(spec.get("apply", True))
            applied: dict[str, Any] = {"applied": False, "reason": "dry-run"}
            if apply:
                # Debounce: the optimiser is deterministic, so re-applying on
                # unchanged inputs (same workforce + signals + weights) just
                # re-installs the identical rota and churns. Only apply when
                # something material has actually changed since the last run.
                import json as _json

                fingerprint = self._schedule_inputs_fingerprint(
                    specs, signals, constraints, objectives,
                )
                state_path = self.agents_dir / ".schedule_state.json"
                last_fp = None
                try:
                    if state_path.exists():
                        last_fp = _json.loads(
                            state_path.read_text(encoding="utf-8")
                        ).get("fingerprint")
                except (OSError, ValueError):
                    pass

                already_applied = (self.agents_dir / ".schedules.json").exists()
                if fingerprint == last_fp and already_applied:
                    applied = {"applied": False,
                               "reason": "no material change — debounced"}
                    logger.info(
                        "Agent %s rota inputs unchanged since last run — "
                        "skipping re-apply (debounce).", agent.id,
                    )
                else:
                    applied = self.apply_schedule_proposal(proposal)
                    if applied.get("applied"):
                        try:
                            state_path.write_text(
                                _json.dumps({"fingerprint": fingerprint}),
                                encoding="utf-8",
                            )
                        except OSError:
                            pass

            # Record the run as a reviewable artifact + memory.
            note = (
                f"## Schedule optimisation — {agent.id}\n\n"
                f"**Summary:** {proposal.summary}\n\n"
                f"**Impact:** {proposal.impact.to_dict()}\n\n"
                f"**Applied:** {applied.get('applied')} "
                f"({applied.get('agents', 0)} agents)\n\n"
                f"**Feasible:** {proposal.feasible}"
            )
            if proposal.violations:
                note += f"\n\n**Violations:** {'; '.join(proposal.violations[:5])}"
            try:
                agent.write_today("schedule_optimization.md", note)
            except Exception:
                logger.debug("Could not write schedule_optimization.md", exc_info=True)

            await self.memory.store(
                agent_id=agent.id,
                content=(
                    f"Ran rota optimiser: {proposal.summary} "
                    f"(applied={applied.get('applied')})"
                ),
                tags=["schedule", "ar", "decision"],
                importance=7.0,
            )
            self._emit(
                "schedule.optimized", agent_id=agent.id,
                feasible=proposal.feasible,
                applied=applied.get("applied", False),
                peak=proposal.impact.peak_concurrency,
            )
            logger.info(
                "Agent %s ran rota optimiser — %s (applied=%s)",
                agent.id, proposal.summary, applied.get("applied"),
            )
        except Exception:
            logger.exception("Schedule optimisation failed for %s", agent.id)

    def _load_cluster_metrics(self) -> dict[str, Any] | None:
        """Read the latest cluster-metrics snapshot pushed in from HQ.

        The open-source fabric has no knowledge of HQ, so the HQ-aware
        node client relays an infra snapshot down and writes it to
        ``.cluster_metrics.json`` in the agents dir. This is the only
        source of cross-node truth (other nodes' RAM, other nodes'
        agents' sleep state, deployment grades) — a single fabric can't
        see beyond itself. Returns ``None`` when no snapshot is present.
        """
        import json

        path = self.agents_dir / ".cluster_metrics.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("Could not read .cluster_metrics.json", exc_info=True)
            return None
        return data if isinstance(data, dict) else None

    def _build_rebalance_inputs(
        self, snapshot: dict[str, Any],
    ) -> tuple[list[Any], list[Any]]:
        """Map an HQ cluster-metrics snapshot onto rebalance dataclasses.

        Snapshot shape (assembled by HQ from all nodes' heartbeats)::

            {"nodes": [{"node_id", "grade", "ram_free_gb", "ram_total_gb",
                        "agents_deployed", "agent_slots", "name",
                        "pressure"}, ...],
             "agents": [{"agent_id", "grade", "current_node", "asleep",
                         "name", "last_moved_hours_ago"}, ...]}
        """
        from cortiva.scheduling.rebalance import AgentState, NodeState

        nodes: list[Any] = []
        for n in snapshot.get("nodes", []) or []:
            if not isinstance(n, dict) or not n.get("node_id"):
                continue
            nodes.append(NodeState(
                node_id=str(n["node_id"]),
                grade=int(n.get("grade", 0)),
                ram_free_gb=float(n.get("ram_free_gb", 0.0)),
                ram_total_gb=float(n.get("ram_total_gb", 0.0)),
                agents_deployed=int(n.get("agents_deployed", 0)),
                agent_slots=int(n.get("agent_slots", 0)),
                name=str(n.get("name", "")),
                pressure=float(n.get("pressure", 0.0)),
            ))
        agents: list[Any] = []
        for a in snapshot.get("agents", []) or []:
            if not isinstance(a, dict) or not a.get("agent_id"):
                continue
            agents.append(AgentState(
                agent_id=str(a["agent_id"]),
                grade=int(a.get("grade", 0)),
                current_node=str(a.get("current_node", "")),
                asleep=bool(a.get("asleep", False)),
                name=str(a.get("name", "")),
                last_moved_hours_ago=float(a.get("last_moved_hours_ago", 1e9)),
            ))
        return nodes, agents

    async def _run_node_rebalance(
        self, agent: Agent, spec: dict[str, Any],
    ) -> None:
        """Plan a reshuffle of agents between nodes. Authority-gated, advisory.

        Phase 1: produce a feasible plan from the infra team's node metrics
        and record it as a reviewable artifact + memory + event. It moves
        nothing — only sleeping agents are ever proposed, never below an
        agent's deployment grade, never past a target's slots/RAM headroom.
        The executor (Phase 2) consumes a plan and performs the moves.
        Never fatal to the cycle.
        """
        if agent.id not in self.scheduling_authorised:
            logger.info(
                "Agent %s emitted rebalance_nodes but lacks scheduling "
                "authority — ignored.", agent.id,
            )
            return
        try:
            from cortiva.scheduling.rebalance import plan_rebalance

            snapshot = self._load_cluster_metrics()
            if not snapshot:
                note = (
                    f"## Node rebalance — {agent.id}\n\n"
                    "No infrastructure metrics available yet "
                    "(`.cluster_metrics.json` not present). The infra team's "
                    "snapshot hasn't reached this node — cannot plan a "
                    "rebalance without cross-node truth."
                )
                try:
                    agent.write_today("node_rebalance.md", note)
                except Exception:
                    logger.debug("Could not write node_rebalance.md", exc_info=True)
                logger.info(
                    "Agent %s requested rebalance but no cluster metrics present.",
                    agent.id,
                )
                return

            nodes, agents = self._build_rebalance_inputs(snapshot)
            kwargs: dict[str, Any] = {}
            if spec.get("ram_headroom_gb") is not None:
                kwargs["ram_headroom_gb"] = float(spec["ram_headroom_gb"])
            if spec.get("max_moves") is not None:
                kwargs["max_moves"] = int(spec["max_moves"])
            if spec.get("pressure_threshold") is not None:
                kwargs["pressure_threshold"] = float(spec["pressure_threshold"])

            plan = plan_rebalance(nodes, agents, **kwargs)

            # Phase 1 is advisory regardless of the agent's `apply` flag.
            requested_apply = bool(spec.get("apply", False))
            apply_note = ""
            if requested_apply:
                apply_note = (
                    "\n\n_Execution requested (`apply=true`) but the executor "
                    "is not enabled yet (Phase 2) — plan recorded only._"
                )

            lines = [f"## Node rebalance — {agent.id}\n", f"**{plan.summary}**\n"]
            if plan.moves:
                lines.append("### Proposed moves")
                for m in plan.moves:
                    lines.append(
                        f"- **{m.agent_id}**: {m.from_node} → {m.to_node} — {m.reason}"
                    )
                lines.append("")
            if plan.skipped:
                lines.append("### Skipped")
                for s in plan.skipped:
                    lines.append(f"- {s.get('agent_id', '?')}: {s.get('reason', '')}")
            note = "\n".join(lines) + apply_note
            try:
                agent.write_today("node_rebalance.md", note)
            except Exception:
                logger.debug("Could not write node_rebalance.md", exc_info=True)

            await self.memory.store(
                agent_id=agent.id,
                content=(
                    f"Planned node rebalance: {plan.summary} "
                    f"({len(plan.moves)} move(s) proposed, advisory)"
                ),
                tags=["rebalance", "infra", "ar", "decision"],
                importance=7.0,
            )
            self._emit(
                "cluster.rebalance_planned", agent_id=agent.id,
                moves=len(plan.moves), skipped=len(plan.skipped),
                applied=False,
            )
            logger.info(
                "Agent %s planned node rebalance — %s (advisory, %d moves)",
                agent.id, plan.summary, len(plan.moves),
            )
        except Exception:
            logger.exception("Node rebalance planning failed for %s", agent.id)

    def _current_schedule_windows(self) -> dict[str, list[Any]]:
        """The live rota as work windows, parsed from the persisted config.

        Reads ``.schedules.json`` (what the optimiser last applied) and turns
        each agent's wake/sleep config back into windows so the AR Scheduler
        can measure the *current* schedule's health.
        """
        import json

        from cortiva.scheduling import schedule_config_to_windows

        path = self.agents_dir / ".schedules.json"
        if not path.exists():
            return {}
        try:
            cfgs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict[str, list[Any]] = {}
        for aid, cfg in (cfgs or {}).items():
            if isinstance(cfg, dict):
                out[aid] = schedule_config_to_windows(cfg)
        return out

    async def _run_schedule_health(self, agent: Agent, spec: dict[str, Any]) -> None:
        """Measure the current rota's responsiveness and record a readout.

        Authority-gated to the scheduling set. Measures only — the AR
        Scheduler reads the ranked hotspots, then optimises one role. Never
        fatal to the cycle.
        """
        if agent.id not in self.scheduling_authorised:
            logger.info(
                "Agent %s emitted schedule_health but lacks scheduling "
                "authority — ignored.", agent.id,
            )
            return
        try:
            from cortiva.scheduling import assess_schedule_health

            specs = self._build_workforce_specs()
            windows = self._current_schedule_windows()
            if not windows:
                try:
                    agent.write_today(
                        "schedule_health.md",
                        f"## Schedule health — {agent.id}\n\nNo rota applied yet "
                        "(`.schedules.json` absent) — run the optimiser first.",
                    )
                except Exception:
                    logger.debug("Could not write schedule_health.md", exc_info=True)
                return

            signals = self._gather_schedule_signals()
            health = assess_schedule_health(
                specs, windows, signals=signals,
                model_concurrency=self._model_concurrency(),
            )

            lines = [f"## Schedule health — {agent.id}\n", f"**{health.summary}**\n"]
            if health.hotspots:
                lines.append("### Hotspots (act on the top one)")
                for h in health.hotspots[:8]:
                    who = f" [{h.agent_id}]" if h.agent_id else ""
                    lines.append(f"- _{h.kind}_{who}: {h.detail}")
            try:
                agent.write_today("schedule_health.md", "\n".join(lines))
            except Exception:
                logger.debug("Could not write schedule_health.md", exc_info=True)

            await self.memory.store(
                agent_id=agent.id,
                content=f"Measured schedule health: {health.summary}",
                tags=["schedule", "health", "ar", "measurement"],
                importance=6.5,
            )
            self._emit(
                "schedule.health_measured", agent_id=agent.id,
                score=health.responsiveness_score,
                uncovered_hours=health.uncovered_hours,
                oversight_gaps=len(health.oversight_gaps),
            )
            logger.info(
                "Agent %s measured schedule health — %s", agent.id, health.summary,
            )
        except Exception:
            logger.exception("Schedule-health measurement failed for %s", agent.id)

    def _build_culture_members(self) -> list[Any]:
        """Build the culture readout's view of the workforce (id/name/dept/mgr)."""
        import yaml

        from cortiva.culture import CultureMember

        members: list[Any] = []
        for aid in sorted(self.agents):
            name, dept = "", ""
            deploy = self.agents_dir / aid / "deploy.yaml"
            if deploy.exists():
                try:
                    spec = (yaml.safe_load(deploy.read_text(encoding="utf-8"))
                            or {}).get("agent", {}) or {}
                    name = str(spec.get("name") or "").strip()
                    dept = str(spec.get("department") or "").strip()
                except Exception:
                    pass
            manager = self.org.manager_of(aid) if self.org else None
            members.append(
                CultureMember(agent_id=aid, name=name, department=dept, manager=manager)
            )
        return members

    async def _run_culture_health(self, agent: Agent, spec: dict[str, Any]) -> None:
        """Measure company culture health and record a readout.

        Authority-gated to the culture set. Reads the whole workforce's felt
        state (emotions) + diversity of voice (comms tracker), scores culture
        0-100, and writes a ranked-hotspot readout. Measures only — the People
        & Culture Lead reads it, then decides the intervention. Never fatal to
        the cycle.
        """
        if agent.id not in self.culture_authorised:
            logger.info(
                "Agent %s emitted culture_health but lacks culture authority "
                "— ignored.", agent.id,
            )
            return
        try:
            from cortiva.culture import assess_culture_health

            members = self._build_culture_members()
            emotions = {
                m.agent_id: self._read_emotion(a)
                for m in members
                if (a := self.agents.get(m.agent_id)) is not None
            }
            try:
                comms = self.communication_tracker.pair_counts()
            except Exception:
                comms = {}

            health = assess_culture_health(members, emotions, comms=comms or None)

            lines = [f"## Culture health — measured by {agent.id}\n", f"**{health.summary}**\n"]
            if health.hotspots:
                lines.append("### Hotspots (start with the top one)")
                for h in health.hotspots[:10]:
                    who = f" [{h.agent_id}]" if h.agent_id else ""
                    lines.append(f"- _{h.kind}_{who}: {h.detail}")
            else:
                lines.append("_No culture concerns surfaced this read._")
            try:
                agent.write_today("culture_health.md", "\n".join(lines))
            except Exception:
                logger.debug("Could not write culture_health.md", exc_info=True)

            await self.memory.store(
                agent_id=agent.id,
                content=f"Measured culture health: {health.summary}",
                tags=["culture", "health", "people", "measurement"],
                importance=6.5,
            )
            self._emit(
                "culture.health_measured", agent_id=agent.id,
                score=health.culture_score,
                distressed=len(health.distressed),
                burnout_risk=len(health.burnout_risk),
                monoculture=health.monoculture,
            )
            logger.info(
                "Agent %s measured culture health — %s", agent.id, health.summary,
            )
        except Exception:
            logger.exception("Culture-health measurement failed for %s", agent.id)

    async def _run_efficiency_review(self, agent: Agent, spec: dict[str, Any]) -> None:
        """Measure workforce efficiency over time and record a readout.

        Authority-gated to the performance set. Gathers each agent's signals
        (tasks/escalations/hours from timesheets, felt state from emotions),
        scores throughput/quality/sustainability + a trend vs the last review,
        and writes a ranked readout. Measures only — the analyst reads it,
        reasons about why, then acts. Never fatal to the cycle.
        """
        if agent.id not in self.performance_authorised:
            logger.info(
                "Agent %s emitted efficiency_review but lacks performance "
                "authority — ignored.", agent.id,
            )
            return
        try:
            import json

            from cortiva.workforce import AgentEfficiencyInput, assess_workforce_efficiency

            prev_path = self.agents_dir / ".efficiency_prev.json"
            prior: dict[str, float] = {}
            if prev_path.exists():
                try:
                    prior = json.loads(prev_path.read_text(encoding="utf-8")) or {}
                except (ValueError, OSError):
                    prior = {}

            names = {m.agent_id: m.name for m in self._build_culture_members()}
            records: list[Any] = []
            for aid in sorted(self.agents):
                a = self.agents.get(aid)
                if a is None:
                    continue
                tasks = esc = 0
                hours, sched = 0.0, 7.5
                try:
                    day = self.timesheet_manager.get(aid).today()
                    tasks = day.total_tasks_completed
                    esc = day.total_tasks_escalated
                    hours = day.total_hours
                    sched = day.scheduled_hours
                except Exception:
                    logger.debug("efficiency: no timesheet for %s", aid, exc_info=True)
                emo = self._read_emotion(a)
                records.append(AgentEfficiencyInput(
                    agent_id=aid, name=names.get(aid) or aid,
                    tasks_completed=tasks, tasks_escalated=esc,
                    active_hours=hours, scheduled_hours=sched,
                    prediction_accuracy=None,  # TODO: enrich from cognition.state
                    cost_gbp=0.0,  # cost-efficiency enriched HQ-side (cost engine)
                    satisfaction=float(emo.get("satisfaction", 0.0)),
                    frustration=float(emo.get("frustration", 0.0)),
                    prior_score=prior.get(aid),
                ))

            review = assess_workforce_efficiency(records)

            lines = [f"## Workforce efficiency — measured by {agent.id}\n", f"**{review.summary}**\n"]
            if review.hotspots:
                lines.append("### Who needs attention (act on the top ones)")
                for h in review.hotspots[:10]:
                    lines.append(f"- _{h.kind}_ [{h.agent_id}]: {h.detail}")
            lines.append("\n### Per-agent (ranked)")
            for a_eff in review.per_agent[:30]:
                tr = f" ({a_eff.trend:+.0f})" if a_eff.trend else ""
                lines.append(
                    f"- **{a_eff.name}** {a_eff.score:.0f}/100{tr} — "
                    f"throughput {a_eff.throughput:.1f}/h, quality {a_eff.quality:.2f}, "
                    f"sustainability {a_eff.sustainability:.2f}"
                )
            try:
                agent.write_today("efficiency_review.md", "\n".join(lines))
            except Exception:
                logger.debug("Could not write efficiency_review.md", exc_info=True)

            try:
                prev_path.write_text(
                    json.dumps({a_eff.agent_id: round(a_eff.score, 1) for a_eff in review.per_agent}),
                    encoding="utf-8",
                )
            except OSError:
                logger.debug("Could not persist efficiency prior scores", exc_info=True)

            await self.memory.store(
                agent_id=agent.id,
                content=f"Measured workforce efficiency: {review.summary}",
                tags=["efficiency", "performance", "ar", "measurement"],
                importance=6.5,
            )
            self._emit(
                "efficiency.reviewed", agent_id=agent.id,
                mean_score=review.mean_score,
                declining=sum(1 for h in review.hotspots if h.kind == "declining"),
                at_risk=sum(1 for h in review.hotspots if h.kind == "at_risk"),
            )
            logger.info("Agent %s reviewed workforce efficiency — %s", agent.id, review.summary)
        except Exception:
            logger.exception("Efficiency review failed for %s", agent.id)

    async def _run_schedule_recommendation(
        self, agent: Agent, spec: dict[str, Any],
    ) -> None:
        """Recommend (and optionally apply) a single-role re-timing that most
        improves company responsiveness. Authority-gated. The steady-state
        tweak: tune one role, holding everyone else fixed. Never fatal.
        """
        if agent.id not in self.scheduling_authorised:
            logger.info(
                "Agent %s emitted recommend_schedule but lacks scheduling "
                "authority — ignored.", agent.id,
            )
            return
        try:
            from cortiva.scheduling import (
                recommend_schedule_change,
                windows_to_schedule_config,
            )

            specs = self._build_workforce_specs()
            windows = self._current_schedule_windows()
            if not windows:
                try:
                    agent.write_today(
                        "schedule_recommendation.md",
                        f"## Schedule recommendation — {agent.id}\n\nNo rota applied "
                        "yet — run the optimiser first.",
                    )
                except Exception:
                    logger.debug("Could not write schedule_recommendation.md", exc_info=True)
                return

            signals = self._gather_schedule_signals()
            target = spec.get("target") or None
            rec = recommend_schedule_change(
                specs, windows, target=target, signals=signals,
                model_concurrency=self._model_concurrency(),
            )

            applied = False
            do_apply = bool(spec.get("apply", False))
            if (
                do_apply
                and rec.delta > 0
                and rec.target
                and rec.recommended_windows
                and rec.recommended_windows != rec.current_windows
            ):
                # Enact JUST this one role — register live + persist its entry.
                cfg = windows_to_schedule_config(rec.recommended_windows)
                self.scheduler.register(rec.target, cfg)
                import json

                path = self.agents_dir / ".schedules.json"
                try:
                    cur = (
                        json.loads(path.read_text(encoding="utf-8"))
                        if path.exists() else {}
                    )
                    cur[rec.target] = cfg
                    path.write_text(json.dumps(cur, indent=2), encoding="utf-8")
                    applied = True
                except OSError:
                    logger.debug("Could not persist single-role schedule", exc_info=True)

            note = (
                f"## Schedule recommendation — {agent.id}\n\n"
                f"**Target:** {rec.target or '(none)'}\n\n"
                f"{rec.rationale}\n\n"
                f"**Responsiveness:** {rec.score_before:.0f} → {rec.score_after:.0f} "
                f"(+{rec.delta})\n\n"
                f"**Applied:** {applied}"
            )
            try:
                agent.write_today("schedule_recommendation.md", note)
            except Exception:
                logger.debug("Could not write schedule_recommendation.md", exc_info=True)

            await self.memory.store(
                agent_id=agent.id,
                content=f"Schedule recommendation for {rec.target}: {rec.rationale} "
                        f"(applied={applied})",
                tags=["schedule", "recommendation", "ar", "decision"],
                importance=7.0,
            )
            self._emit(
                "schedule.recommended", agent_id=agent.id,
                target=rec.target, delta=rec.delta, applied=applied,
            )
            logger.info(
                "Agent %s schedule recommendation for %s (+%s, applied=%s)",
                agent.id, rec.target, rec.delta, applied,
            )
        except Exception:
            logger.exception("Schedule recommendation failed for %s", agent.id)

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

        # Deep think — frontier-model help on a hard question or a
        # second opinion on the agent's own conclusion. The local model
        # decides it needs this and emits `deep_think` in its suffix; we
        # subshell to the claude CLI and fold the answer back into
        # memory so it shapes subsequent cycles. Budget-gated and never
        # fatal — a failed deep-think must not break the task.
        if suffix.deep_think:
            await self._run_deep_think(agent, task, suffix.deep_think)

        # Hire — bring on a new team member. Authority-gated: only the
        # CEO/COO can command it. Generates a diverse persona and boots
        # the new colleague live (see HiringManager).
        if suffix.hire:
            await self._run_hire(agent, suffix.hire)

        # Optimise the workforce rota. Authority-gated to the AR Scheduler
        # (and Head of AR / COO). The agent steers the optimiser via weights
        # and constraints; the tool guarantees a feasible result or none.
        if suffix.optimize_schedule is not None:
            await self._run_schedule_optimization(agent, suffix.optimize_schedule)

        # Plan a node rebalance. Authority-gated to the same scheduling
        # set. Advisory in Phase 1 — produces a feasible move plan from
        # the infra team's metrics; moves nothing.
        if suffix.rebalance_nodes is not None:
            await self._run_node_rebalance(agent, suffix.rebalance_nodes)

        # Measure rota responsiveness. Authority-gated to the scheduling set.
        # Measures only — the AR Scheduler reads the readout, then optimises.
        if suffix.schedule_health is not None:
            await self._run_schedule_health(agent, suffix.schedule_health)

        # Measure company culture health. Authority-gated to the culture set.
        # Measures only — the People & Culture Lead reads it, then intervenes.
        if suffix.culture_health is not None:
            await self._run_culture_health(agent, suffix.culture_health)

        # Measure workforce efficiency over time. Authority-gated to the
        # performance set. Measures only — the analyst reasons over it.
        if suffix.efficiency_review is not None:
            await self._run_efficiency_review(agent, suffix.efficiency_review)

        # Optimise one role's schedule for responsiveness (the steady-state
        # tweak). Authority-gated; applies only the single targeted role.
        if suffix.recommend_schedule is not None:
            await self._run_schedule_recommendation(agent, suffix.recommend_schedule)

        # Queue an outbound email — the node sends it via Resend as this
        # agent's own address. Written to outbox/email/ as a durable record;
        # the node picks it up, adds the from address, and sends.
        if suffix.email and isinstance(suffix.email, dict):
            self._queue_outbound_email(agent, suffix.email)

        # Save a document to the company store — queued to the agent's
        # outbox; the node hands it to HQ (MinIO + metadata).
        if suffix.document and isinstance(suffix.document, dict):
            self._queue_outbound_document(agent, suffix.document)

        # Send inter-agent messages. Deliver in-process when the recipient is
        # a colleague THIS fabric runs (they'll receive it on their next
        # cycle). When they're not — on another machine, or unknown to the
        # in-process bus — fall back to EMAIL so the message never silently
        # vanishes (the cross-node drop that lost the CEO's memos).
        if suffix.messages:
            import json as _json
            agent.write_outbox("messages.json", _json.dumps(suffix.messages, indent=2))
            cards_by_key: dict[str, dict] = {}
            for c in self._load_directory_cards():
                for k in (c.get("id"), c.get("name"), c.get("first")):
                    if k:
                        cards_by_key[str(k).strip().lower()] = c
            # Local = a colleague THIS fabric runs (same-node cards) or a known
            # agent id. Everyone else routes to email.
            local_keys = set(cards_by_key) | {a.strip().lower() for a in self.agents}
            domain = (self._email_meta().get("domain") or "").strip()
            for msg in suffix.messages:
                recipient = str(msg.get("to", "")).strip()
                content = msg.get("content", "")
                if not recipient or not content:
                    continue
                if recipient.lower() in local_keys and self.channel:
                    await self.channel.send(
                        sender=agent.id, recipient=recipient, content=content,
                    )
                    self.communication_tracker.record(agent.id, recipient)
                    continue
                addr = _resolve_msg_email(recipient, cards_by_key, domain)
                if addr:
                    self._queue_outbound_email(agent, {
                        "to": addr,
                        "subject": f"A message from {self._agent_first_name(agent) or agent.id}",
                        "body": content,
                    })
                    logger.info(
                        "Peer message to %s not reachable in-process — sent as "
                        "email to %s so it lands.", recipient, addr,
                    )
                elif self.channel:
                    # No email route — best-effort in-process (may queue).
                    await self.channel.send(
                        sender=agent.id, recipient=recipient, content=content,
                    )
                    self.communication_tracker.record(agent.id, recipient)

        # Escalation: keep the local record AND route it to a human. A block
        # an agent can't clear itself is useless sitting in a file — it has to
        # reach someone who can act. So we email the manager, and for operator/
        # founder-level blocks the founder too (manager cc'd — "boss in flow").
        if suffix.escalation:
            import json as _json
            agent.write_outbox("escalations.json", _json.dumps(
                {"task": task.description, "escalation": suffix.escalation}, indent=2,
            ))
            logger.warning(
                f"Agent {agent.id} escalation on '{task.description}': "
                f"{suffix.escalation}"
            )
            try:
                self._route_escalation(agent, task.description, str(suffix.escalation))
            except Exception:
                logger.exception("Escalation routing failed for %s", agent.id)

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

        # Manager rallying their team — wake direct reports NOW (a crisis, a
        # call to arms, or when the manager's own stress says they need the
        # team in). Authority-gated: only THIS agent's actual reports (org
        # chart) are woken; anyone else in the list is ignored.
        if suffix.wake and isinstance(suffix.wake, dict):
            targets = suffix.wake.get("agents") or []
            reason = str(suffix.wake.get("reason") or "").strip()
            reports = set(self.org.subordinates_of(agent.id)) if self.org else set()
            for raw in targets if isinstance(targets, list) else []:
                tid = str(raw).strip()
                if not tid:
                    continue
                if tid not in reports:
                    logger.info(
                        "Agent %s tried to wake %s but they're not a direct "
                        "report — ignored.", agent.id, tid,
                    )
                    continue
                if tid not in self.agents:
                    # Cross-node report — local fabric can't wake it directly.
                    logger.info(
                        "Agent %s wake of %s skipped — not on this node.",
                        agent.id, tid,
                    )
                    continue
                try:
                    if self.agents[tid].state == AgentState.SLEEPING:
                        await self.wake(
                            tid, override_minutes=self._WAKE_OVERRIDE_MINUTES,
                        )
                    # Deliver the call-to-arms so the report knows WHY.
                    if reason and self.channel:
                        await self.channel.send(
                            sender=agent.id, recipient=tid,
                            content=f"[Woken by {agent.id} — call to arms] {reason}",
                        )
                    self._emit(
                        "agent.woken_by_manager", agent_id=tid,
                        by=agent.id, reason=reason,
                    )
                    logger.info(
                        "Agent %s woke report %s (reason: %s)",
                        agent.id, tid, reason[:80] or "—",
                    )
                except Exception:
                    logger.exception(
                        "Manager wake failed: %s -> %s", agent.id, tid,
                    )

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

        # Process shared learning.
        #
        # ISOLATION (founder directive 2026-06-07): the org-shared memory
        # tier is OFF. Cross-agent knowledge sharing was injecting a single
        # theme into every agent's planning context and collapsing the org
        # into a monoculture. Until shared memory is reintroduced strictly
        # as company broadcasts/events (never anything that reshapes a
        # personality), an agent's "shared_learning" is kept as that
        # agent's OWN private memory — the learning isn't lost, but it never
        # leaves the agent who learned it. No write to __org_shared__.
        if suffix.shared_learning:
            await self.memory.store(
                agent_id=agent.id,
                content=suffix.shared_learning,
                tags=["learning", f"author:{agent.id}"],
                importance=7.0,
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
        # GitHub work — issues, projects, wiki product-thinking — runs
        # through the gh CLI / git in the terminal env. Compound phrases
        # to avoid over-routing prose ("investigate the issue") away
        # from consciousness execution.
        "github", "wiki", "create issue", "create an issue", "file an issue",
        "raise an issue", "triage issues", "project board", "milestone",
        "pull request",
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

        # Agent-as-driver: run hands-on work as a DETACHED, steerable claude
        # session off the heartbeat (capped at 2/agent), reaped at a later cycle.
        # Falls through to the synchronous one-shot path below when disabled
        # (CORTIVA_DEV_SESSIONS=0) or for non-claude terminals.
        if self._dev_sessions_enabled:
            return self._dispatch_dev_session(agent, task)

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

        # Inject the agent's delegated credentials (GH_TOKEN etc.) into
        # the subprocess env: the credential provider (cortiva.yaml) and
        # the agent-dir credentials.json written by the management layer.
        # Without this the CredentialProvider was configured but never
        # consulted — agents had no way to act on external systems.
        creds: dict[str, str] = {}
        if self.credential_provider is not None:
            try:
                creds.update(self.credential_provider.get_env(agent.id))
            except Exception:
                logger.exception(
                    f"Credential provider failed for {agent.id}; "
                    "continuing without provider credentials",
                )
        creds.update(load_agent_credentials(agent.directory))

        # Give the agent its OWN persistent Claude Code session: a private
        # config dir (so session history doesn't collide with other agents on
        # the shared box) plus a resumed session id, so each task continues the
        # same growing conversation about its work rather than starting cold.
        env_overrides = dict(creds)
        config_dir = cwd / ".claude"
        config_dir.mkdir(parents=True, exist_ok=True)
        env_overrides["CLAUDE_CONFIG_DIR"] = str(config_dir)

        session_file = cwd / ".claude_session"
        resume_session: str | None = None
        try:
            prior = session_file.read_text(encoding="utf-8").strip()
            resume_session = prior or None
        except OSError:
            resume_session = None

        if env_overrides:
            base_env = (
                dict(envelope.env) if envelope.env is not None
                else dict(os.environ)
            )
            envelope.env = {**base_env, **env_overrides}

        # Enforce tool-level policy
        policy = self.policy_manager.get(agent.id)
        allowed_tools = policy.tools.effective_allowed()

        response = await self.terminal.invoke(
            prompt=prompt,
            cwd=envelope.cwd,
            env=envelope.env,
            allowed_tools=allowed_tools,
            resume_session=resume_session,
        )

        # A stale/expired session id makes claude error immediately; retry once
        # from a clean session so one dead session never wedges the agent.
        if response.is_error and resume_session:
            logger.info(
                "Agent %s terminal resume failed; retrying with a fresh session",
                agent.id,
            )
            response = await self.terminal.invoke(
                prompt=prompt,
                cwd=envelope.cwd,
                env=envelope.env,
                allowed_tools=allowed_tools,
                resume_session=None,
            )

        # Persist the (possibly new) session id so the next task resumes it.
        new_session = getattr(response, "session_id", None)
        if new_session and new_session != resume_session:
            try:
                session_file.write_text(str(new_session), encoding="utf-8")
            except OSError:
                logger.debug("Could not persist claude session id for %s", agent.id)

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

    # ------------------------------------------------------------------
    # Agent-as-driver: detached, steerable dev sessions (Slot A + Slot B)
    # ------------------------------------------------------------------

    _DEV_SESSION_TIMEOUT_S = 1800.0  # 30-min runaway backstop, NOT a work cap

    def _dispatch_dev_session(self, agent: Agent, task: Task) -> bool:
        """Launch (or defer) a detached dev session for a terminal task.

        Returns True (handled) in every case: launched and now in-flight, or
        deferred because the agent is at its 2-session cap (the task stays
        pending and a later cycle launches it). Never blocks the cycle.
        """
        if self.dev_sessions.is_in_flight(agent.id, task.id):
            return True  # a session already owns this task
        ctx = self._terminal_context_for_session(agent, task)
        launched = self.dev_sessions.launch(
            agent.id, task.id,
            lambda: self._run_dev_session(agent, task, ctx),
        )
        if launched:
            task.status = "in_progress"
            logger.info(
                "Agent %s now driving a dev session: %s",
                agent.id, task.description[:60],
            )
        # If not launched (at cap) the task stays pending for a later cycle.
        return True

    def _terminal_context_for_session(self, agent: Agent, task: Task) -> dict[str, Any]:
        """Assemble the same execution context the synchronous terminal path
        builds — prompt, cwd, env (creds + private CLAUDE_CONFIG_DIR), allowed
        tools, resumed session — so a driven session behaves identically."""
        identity = agent.read_all_identity()
        prompt = (
            f"You are {agent.id}. Execute this task:\n\n{task.description}\n\n"
            f"## Your Procedures\n{identity.get('procedures', '')}\n\n"
            f"## Your Responsibilities\n{identity.get('responsibilities', '')}\n\n"
            "Do the work end to end. When done, summarise what you did and the outcome."
        )
        cwd = agent.directory / "workspace"
        cwd.mkdir(parents=True, exist_ok=True)
        envelope = self.isolation.prepare_terminal_env(agent_id=agent.id, cmd=[], cwd=cwd)

        creds: dict[str, str] = {}
        if self.credential_provider is not None:
            try:
                creds.update(self.credential_provider.get_env(agent.id))
            except Exception:
                logger.exception("Credential provider failed for %s", agent.id)
        creds.update(load_agent_credentials(agent.directory))

        config_dir = cwd / ".claude"
        config_dir.mkdir(parents=True, exist_ok=True)
        creds["CLAUDE_CONFIG_DIR"] = str(config_dir)

        session_file = cwd / ".claude_session"
        try:
            resume_session = (session_file.read_text(encoding="utf-8").strip() or None)
        except OSError:
            resume_session = None

        base_env = dict(envelope.env) if envelope.env is not None else dict(os.environ)
        env = {**base_env, **creds}
        allowed_tools = self.policy_manager.get(agent.id).tools.effective_allowed()
        return {
            "prompt": prompt,
            "cwd": envelope.cwd if envelope.cwd is not None else cwd,
            "env": env,
            "allowed_tools": allowed_tools,
            "resume_session": resume_session,
            "session_file": session_file,
        }

    async def _run_dev_session(
        self, agent: Agent, task: Task, ctx: dict[str, Any],
    ):
        """Drive Slot A (the work) as a live, steerable session, then have Slot
        B (Claude) question its output. Returns a SessionResult the agent reaps.

        The agent (Myelin/Qwen) decides *when* to verify; the verification
        itself is Claude's — so we never ask the local model to validate
        Claude's technical work.
        """
        from cortiva.adapters.terminal.claude_session import ClaudeSession, Checkpoint
        from cortiva.core.dev_sessions import SessionResult

        model = getattr(self.terminal, "_model", None)

        async def _drive(resume: str | None) -> tuple[str, str, int, bool]:
            """Run one Slot-A session. Returns (final_text, session_id, tools, is_error)."""
            s = ClaudeSession(
                cwd=ctx["cwd"], model=model, env=ctx["env"],
                allowed_tools=ctx["allowed_tools"] or None,
                resume=resume,
            )
            final, sid, tools, err = "", "", 0, False
            await s.start(ctx["prompt"])
            try:
                async for ev in s.events():
                    if ev.session_id:
                        sid = ev.session_id
                    if ev.checkpoint is Checkpoint.TOOL or ev.checkpoint is Checkpoint.DESTRUCTIVE:
                        tools += 1
                        if ev.checkpoint is Checkpoint.DESTRUCTIVE:
                            logger.info(
                                "Agent %s session: destructive step %s", agent.id, ev.tool_name,
                            )
                    elif ev.checkpoint is Checkpoint.DONE:
                        final, err = ev.text, ev.is_error
                        break
            finally:
                await s.close()
            return final, sid, tools, err

        try:
            final, sid, tools, err = await asyncio.wait_for(
                _drive(ctx["resume_session"]), timeout=self._DEV_SESSION_TIMEOUT_S,
            )
            # Stale session id errors immediately — retry once from a clean one.
            if err and ctx["resume_session"]:
                final, sid, tools, err = await asyncio.wait_for(
                    _drive(None), timeout=self._DEV_SESSION_TIMEOUT_S,
                )
        except TimeoutError:
            return SessionResult(
                agent_id=agent.id, task_id=task.id, ok=False,
                error=f"session exceeded {self._DEV_SESSION_TIMEOUT_S:.0f}s backstop",
            )
        except Exception as exc:  # noqa: BLE001
            return SessionResult(
                agent_id=agent.id, task_id=task.id, ok=False, error=f"session error: {exc}",
            )

        if sid:
            try:
                ctx["session_file"].write_text(sid, encoding="utf-8")
            except OSError:
                pass

        if err or not final:
            return SessionResult(
                agent_id=agent.id, task_id=task.id, ok=False,
                error=(final or "session produced no result")[:300],
                session_id=sid, tools_used=tools,
            )

        # Slot B questions Slot A's output — on a model NEVER weaker than the
        # one that did the work (a weak critic just rubber-stamps a strong
        # producer). Same model as Slot A here; the real upgrade is a DIFFERENT
        # strong family (e.g. Qwen-Max) so the reviewer doesn't share the
        # producer's blind spots — verify with a different lineage than you
        # produced with.
        critique = await self._slot_b_critique(task.description, final, model)
        return SessionResult(
            agent_id=agent.id, task_id=task.id, ok=True,
            outcome=final[:500], session_id=sid, tools_used=tools, critique=critique,
        )

    async def _slot_b_critique(
        self, task_desc: str, outcome: str, model: str | None,
    ) -> str:
        """Slot B: a Claude pass that challenges Slot A's output, on a model at
        least as capable as the one that produced it (``model`` = Slot A's; None
        = the CLI default). One-line verdict, or '' if unavailable. Best-effort."""
        try:
            from cortiva.skills.claude_code_deep_think.wrapper import deep_think

            res = await asyncio.to_thread(
                deep_think,
                (
                    "You are a senior reviewer. A colleague was asked to do this "
                    f"task:\n{task_desc}\n\nThey report:\n{outcome}\n\n"
                    "In ONE line: is this actually complete and correct? If "
                    "something's missing, wrong, or unverified, say so plainly. "
                    "If it's solid, reply 'LGTM'."
                ),
                timeout_s=90.0,
                extra_args=(["--model", model] if model else None),
            )
            return (res.text or "").strip()[:300]
        except Exception:
            return ""

    async def _reap_dev_sessions(self, agent: Agent) -> None:
        """Apply finished detached sessions to the agent's own queue — done at
        the start of its cycle, single-threaded, so no concurrent state races."""
        if not self._dev_sessions_enabled or agent.task_queue is None:
            return
        for r in self.dev_sessions.drain_completed(agent.id):
            task = next((t for t in agent.task_queue.tasks if t.id == r.task_id), None)
            if task is None:
                continue
            if r.ok:
                task.status = "done"
                task.outcome = r.outcome or "Completed via dev session"
                agent.tasks_completed_today += 1
                note = f"Task: {task.description}. Outcome: {task.outcome[:200]}"
                if r.critique and r.critique.upper() != "LGTM":
                    note += f" | Reviewer: {r.critique[:150]}"
                try:
                    await self.memory.store(
                        agent_id=agent.id, content=note,
                        tags=["cycle", "task", "dev_session"],
                        importance=5.0 + task.priority,
                    )
                except Exception:
                    logger.debug("memory store failed for reaped session", exc_info=True)
                logger.info(
                    "Agent %s dev session done (%d tools): %s",
                    agent.id, r.tools_used, task.description[:50],
                )
            else:
                task.status = "exception"
                task.error = r.error[:200]
                agent.task_queue.exceptions.append(task)
                agent.tasks_escalated_today += 1
                logger.warning(
                    "Agent %s dev session failed: %s — %s",
                    agent.id, task.description[:50], r.error[:120],
                )

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

    def _email_inbox_context(self, agent: Agent) -> str:
        """Read the agent's delivered email inbox and render it for the wake
        context, then move read mail aside so it surfaces only once.

        The node drops inbound mail (from HQ/Resend) as JSON files in
        ``<agent>/inbox/``. Replies go out via the email reflection action.
        """
        import json

        inbox = agent.directory / "inbox"
        if not inbox.is_dir():
            return ""
        files = sorted(p for p in inbox.glob("*.json") if p.is_file())
        if not files:
            return ""
        read_dir = inbox / "read"
        items: list[dict] = []
        for p in files:
            try:
                items.append(json.loads(p.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
            try:
                read_dir.mkdir(exist_ok=True)
                p.rename(read_dir / p.name)
            except OSError:
                pass
        if not items:
            return ""

        # Mail from someone with authority over you is NOT optional. Policy:
        # treat mail from (a) a founder, (b) a human colleague, and (c) anyone
        # in your management chain (your manager, on up to the top) as
        # action-expected. (A founder's "update please" — and, by the same
        # logic, your manager's — was being deprioritised under the blanket
        # "ignore as you judge" framing.)
        import re

        def _addr(s: str) -> str:
            m = re.search(r"[\w.+-]+@[\w.-]+", s or "")
            return m.group(0).lower() if m else (s or "").strip().lower()

        authority: dict[str, str] = {}  # address -> label
        for c in (self._email_meta().get("contacts") or []):
            a = _addr(str(c.get("address", "")))
            if a:
                authority[a] = "the founder"
        for p in self._load_people():
            a = _addr(str(p.get("email", "")))
            if a:
                authority[a] = f"{p.get('name') or 'a colleague'} (a human colleague)"

        # Your superiors: walk the reporting line up and add each manager's
        # address (their email is <first-name>@<workforce-domain>).
        if self.org is not None:
            cards = {c["id"]: c for c in self._load_directory_cards()}
            cur, seen = agent.id, set()
            for _ in range(8):  # bounded climb to the top
                mgr = self.org.manager_of(cur)
                if not mgr or mgr in seen:
                    break
                seen.add(mgr)
                card = cards.get(mgr)
                if card and card.get("email"):
                    rel = "your manager" if mgr == self.org.manager_of(agent.id) else "in your management chain"
                    authority.setdefault(_addr(card["email"]), f"{card.get('name') or mgr} ({rel})")
                cur = mgr

        priority = [m for m in items if _addr(m.get("from", "")) in authority]
        rest = [m for m in items if _addr(m.get("from", "")) not in authority]
        # GitHub notifications are feedback on the agent's OWN work — comments
        # on their PRs/issues, review requests, CI. They were landing in the
        # "ignore as you judge" bucket and piling up unread, so feedback loops
        # never closed. Pull them into their own action-expected block.
        github = [m for m in rest if _is_github_email(m.get("from", ""))]
        normal = [m for m in rest if not _is_github_email(m.get("from", ""))]

        lines: list[str] = []
        if priority:
            lines.append("## 📨 Mail that needs you — respond this session\n")
            lines.append(
                f"{len(priority)} message(s) from people whose mail carries "
                "weight — a founder, your manager or someone above you in the "
                "reporting line, or a human colleague. Treat these as "
                "**action-expected, not optional**: read them properly and "
                "**reply this session** unless there's a genuine reason not to. "
                "A superior waiting on an answer is a priority — acknowledging "
                "and replying is part of the job, even when you can also action "
                "the content. To reply, emit an `email` reflection field.\n"
            )
            for m in priority[:10]:
                who = authority.get(_addr(m.get("from", "")), "")
                snippet = (m.get("text") or "").strip().replace("\n", " ")[:240]
                lines.append(
                    f"- **{m.get('from', '')}**"
                    + (f" — _{who}_" if who else "")
                    + f" — {m.get('subject', '')}  \n  {snippet}"
                )
            lines.append("")
        if github:
            lines.append("## 🔧 GitHub — feedback on your work, respond there\n")
            lines.append(
                f"{len(github)} GitHub notification(s) — comments on your pull "
                "requests or issues, review requests, CI results. This is "
                "feedback on **your own work** and it **needs a response**: open "
                "the PR or issue with your github tools (`gh pr view`, "
                "`gh issue view`), read the full thread, and **reply there** — a "
                "review comment left unanswered blocks whoever's waiting on you. "
                "Don't let these pile up unread.\n"
            )
            for m in github[:10]:
                snippet = (m.get("text") or "").strip().replace("\n", " ")[:200]
                lines.append(f"- **{m.get('subject', '')}**  \n  {snippet}")
            lines.append("")
        if normal:
            lines.append("## 📧 New Mail — notification\n")
            lines.append(
                f"{len(normal)} other new email(s). A heads-up, not a demand: "
                "read, defer, or ignore as you judge best. Route anything you'd "
                "escalate through your manager first.\n"
            )
            for m in normal[:10]:
                snippet = (m.get("text") or "").strip().replace("\n", " ")[:200]
                lines.append(f"- **{m.get('from', '')}** — {m.get('subject', '')}  \n  {snippet}")
        return "\n".join(lines)

    def _email_meta(self) -> dict:
        """Node-delivered email config: domain + human contacts. Written by
        the node from HQ's email.config; read here to tell agents their
        address and who they can write to. Empty if email isn't configured."""
        import json

        path = self.agents_dir / ".email_meta.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _capability_status_context(self, agent: Agent) -> str:
        """Fresh, TESTED status of the load-bearing capabilities, as facts.

        The node probes the real infrastructure each cycle — can outbound email
        actually send, is GitHub auth live, is the local model up — and writes
        the results to ``.capability_status.json``. We surface them here as
        observations the agent should trust OVER its own memory or procedures.

        This is the cure for self-sealing 'X is blocked' beliefs: an agent that
        decided email was down then *relayed instead of emailing* never ran the
        test that would disprove it. Putting a just-tested result in front of it
        each wake means the belief is continuously reconciled with reality —
        "tested 40s ago: email LIVE" beats "I think the channel is down".
        """
        import json

        path = self.agents_dir / ".capability_status.json"
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return ""
        caps = data.get("capabilities") or data
        if not isinstance(caps, dict) or not caps:
            return ""
        from datetime import UTC, datetime

        age = ""
        ts = data.get("probed_at")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts))
                secs = (datetime.now(UTC) - dt).total_seconds()
                age = (
                    f" — tested {int(secs)}s ago" if secs < 120
                    else f" — tested {int(secs // 60)}m ago"
                )
            except (ValueError, TypeError):
                age = ""
        lines = [
            f"## Live capability check{age} (TESTED — trust this over memory)\n",
        ]
        label = {
            "email": "Email / reaching humans",
            "github": "GitHub (push, PRs, CI)",
            "model": "Local model",
        }
        for key, cap in caps.items():
            if not isinstance(cap, dict):
                continue
            status = str(cap.get("status", "?")).upper()
            detail = str(cap.get("detail", "")).strip()
            lines.append(
                f"- **{label.get(key, key)}: {status}**"
                + (f" — {detail}" if detail else "")
            )
        lines.append(
            "\nThese were just tested against the real infrastructure. If a "
            "belief, procedure, or plan of yours says one of these is "
            "missing/blocked when the check says it's LIVE/OK, **that belief is "
            "stale — drop it and act on what's tested here.** Before you "
            "escalate, journal, or route around something as 'blocked', confirm "
            "it against this check (or, for anything not listed, run the cheapest "
            "real test first). A workaround that avoids the blocked thing keeps "
            "you blocked forever — the fastest unblock is usually to just try the "
            "real thing once."
        )
        return "\n".join(lines)

    def _email_capability_context(self, agent: Agent) -> str:
        """Standing email context injected each wake (if email is set up):
        the agent's own address, how to send, the human contacts, and the
        manager-first norm. Lets agents email proactively, not only reply."""
        meta = self._email_meta()
        domain = meta.get("domain")
        if not domain:
            return ""
        first = self._agent_first_name(agent)
        own = f"{first}@{domain}" if first else f"(your handle)@{domain}"
        lines = [
            "## Email\n",
            f"Your email address is **{own}**. To send or reply, add an "
            "`email` object to your reflection: "
            '{"to": "<address>", "subject": "...", "body": "...", '
            '"in_reply_to": "<message_id, optional>"}. You can email '
            "colleagues at `<first-name>@" + domain + "` and external "
            "addresses. Send only when it's genuinely useful.",
            "For anything that must **persist** or reach someone who may be "
            "asleep or on another machine — a decision, a memo to your team, "
            "a cadence note — prefer **email** over a passing message. Email "
            "is durable and always delivered; the in-office channel only "
            "reaches colleagues who are awake nearby. If you lead a team and "
            "need them all to have something on record, email the group.",
            "**Reaching humans is NOT blocked.** The in-office channel only "
            "carries agent-to-agent chatter, but email reaches real people — "
            "your manager, the founders, human colleagues. So if you're "
            "**blocked** on something you can't resolve yourself (an access "
            "grant, an operator/admin action, a decision only a human can "
            "make), do NOT just journal it and move on hoping someone reads "
            "it. **Escalate**: emit an `escalation` field naming what you need "
            "and who from — that emails your manager directly (and a founder, "
            "with your manager copied, for operator/admin-level asks). It "
            "reaches a person who can act. A block you sit on is a block that "
            "never clears.",
            "**There is no separate 'outbound human channel adapter' to wait "
            "for or ask the operator to provision — email above IS that "
            "channel, and it is live right now.** If any of your procedures, "
            "plans, or tasks say to journal human-bound messages, tag them "
            "`[HUMAN-OUTBOUND]`, hold them, or route them through other agents "
            "until some adapter/channel is 'configured', that belief is STALE "
            "and wrong: **drop that procedure** and email the human directly. "
            "Using a colleague as a relay to reach a human you could simply "
            "email is never correct — it's the exact busywork you must not "
            "bake in. The proof it works: this message reached you by email.",
        ]
        contacts = meta.get("contacts") or []
        if contacts:
            cline = "; ".join(
                f"{c.get('address')} ({c.get('scope', '')})" for c in contacts
            )
            lines.append(
                f"\n**Reaching a human founder:** {cline}. Ask your manager "
                "first — only go to a founder when it's genuinely warranted, "
                "and match the contact to the matter (day-to-day vs board-level)."
            )
        return "\n".join(lines)

    def _agent_first_name(self, agent: Agent) -> str:
        """The agent's persona first name (lowercased) = their email handle."""
        import yaml

        deploy = agent.directory / "deploy.yaml"
        if deploy.exists():
            try:
                spec = (yaml.safe_load(deploy.read_text(encoding="utf-8"))
                        or {}).get("agent", {}) or {}
                name = (spec.get("name") or "").strip()
                if name:
                    return name.split()[0].lower()
            except Exception:
                pass
        return ""

    # ------------------------------------------------------------------
    # Global Address List (GAL) — colleague directory for the agent
    # ------------------------------------------------------------------

    _DIRECTORY_FULL_CAP = 40

    def _load_directory_cards(self) -> list[dict]:
        """Build a contact card per agent from on-disk deploy.yaml.

        Card: ``{id, name, first, role, department, reports_to, email}``.
        Email is ``<first>@<domain>`` when the workforce domain is known.
        This is the same-node directory (the agents this fabric manages);
        cross-node colleagues are supplemented by HQ at scale.
        """
        import yaml

        domain = (self._email_meta().get("domain") or "").strip()
        cards: list[dict] = []
        if not self.agents_dir.is_dir():
            return cards
        for d in sorted(self.agents_dir.iterdir()):
            deploy = d / "deploy.yaml"
            if not deploy.is_file():
                continue
            try:
                spec = (yaml.safe_load(deploy.read_text(encoding="utf-8"))
                        or {}).get("agent", {}) or {}
            except Exception:
                continue
            name = (spec.get("name") or "").strip()
            if not name:
                continue
            first = name.split()[0].lower()
            cards.append({
                "id": d.name,
                "name": name,
                "first": first,
                "role": (spec.get("role") or "").strip(),
                "department": (spec.get("department") or "").strip(),
                "reports_to": (spec.get("reports_to") or "").strip(),
                "email": f"{first}@{domain}" if domain else "",
            })
        return cards

    def _directory_context(self, agent: Agent) -> str:
        """Render the company directory (GAL) for the agent's context.

        Small orgs get the full directory grouped by department. Large
        orgs (> cap) get the agent's own department plus their management
        chain, with a pointer to the searchable portal directory — so the
        digest stays bounded as the workforce grows to thousands.
        """
        cards = self._load_directory_cards()
        if len(cards) <= 1:
            return ""

        def _fmt(c: dict) -> str:
            bits = [c["name"]]
            meta = ", ".join(x for x in (c.get("role"), c.get("department")) if x)
            if meta:
                bits.append(f"({meta})")
            line = " ".join(bits)
            return f"- {line}" + (f" — {c['email']}" if c.get("email") else "")

        lines = ["## Company directory (who to reach)\n"]

        if len(cards) <= self._DIRECTORY_FULL_CAP:
            by_dept: dict[str, list[dict]] = {}
            for c in cards:
                by_dept.setdefault(c.get("department") or "Other", []).append(c)
            for dept in sorted(by_dept):
                lines.append(f"\n**{dept}**")
                lines.extend(_fmt(c) for c in by_dept[dept])
            lines.append(_REACH_PROTOCOL)
            return "\n".join(lines)

        # Large org: bounded view — own department + management chain.
        mine = next((c for c in cards if c["id"] == agent.id), None)
        my_dept = (mine or {}).get("department") or ""
        dept_members = [c for c in cards if c.get("department") == my_dept and my_dept]
        chain_ids = set()
        cur = agent.id
        for _ in range(6):  # walk up the reporting line
            mgr = self.org.manager_of(cur) if self.org else None
            if not mgr or mgr in chain_ids:
                break
            chain_ids.add(mgr)
            cur = mgr
        chain = [c for c in cards if c["id"] in chain_ids]

        if dept_members:
            lines.append(f"\n**Your department — {my_dept}**")
            lines.extend(_fmt(c) for c in dept_members if c["id"] != agent.id)
        if chain:
            lines.append("\n**Your management chain**")
            lines.extend(_fmt(c) for c in chain)
        lines.append(
            f"\n{len(cards)} colleagues total. Search the full directory in the "
            "portal to find anyone by name, role, or department."
        )
        lines.append(_REACH_PROTOCOL)
        return "\n".join(lines)

    def _load_people(self) -> list[dict]:
        """Human colleagues the node delivered (.people.json), if any."""
        import json

        path = self.agents_dir / ".people.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (ValueError, OSError):
            return []

    def _people_context(self, agent: Agent) -> str:
        """Tell the agent which colleagues are HUMAN — who they are, where
        they sit, and how to work with them (limited hours, async, reach by
        email, route through their manager). Surfaced each wake so agents
        treat humans appropriately rather than expecting agent-speed replies.
        """
        people = self._load_people()
        if not people:
            return ""
        lines = [
            "## Humans on the team\n",
            "Some colleagues are people, not agents. They work limited hours "
            "and reply asynchronously — never block on them, never expect an "
            "instant answer, and route anything you'd escalate through their "
            "manager. Reach them by email.\n",
        ]
        for p in people:
            name = p.get("name") or "(unnamed)"
            role = p.get("role") or ""
            email = p.get("email") or ""
            hrs = p.get("hours_per_week")
            pattern = p.get("working_pattern") or ""
            reports_to = p.get("reports_to") or ""
            manages = p.get("manages") or []
            bits = [f"**{name}**" + (f" — {role}" if role else "") + "  _(human)_"]
            avail = []
            if hrs:
                avail.append(f"~{hrs} hrs/week")
            if pattern:
                avail.append(pattern)
            if avail:
                bits.append(f"  Availability: {', '.join(avail)} — async.")
            if email:
                bits.append(f"  Email: {email}")
            if reports_to:
                rel = "reports to you" if reports_to == agent.id else f"reports to {reports_to}"
                bits.append(f"  {rel}.")
            if agent.id in manages:
                bits.append("  You manage this person — delegate async, give clear briefs, don't expect agent-speed turnaround.")
            lines.append("- " + "\n".join(bits))
        return "\n".join(lines)

    def _queue_outbound_email(self, agent: Agent, spec: dict) -> None:
        """Queue an outbound email to the agent's outbox for the node to send."""
        import json
        import uuid
        from datetime import UTC, datetime

        to = spec.get("to")
        body = spec.get("body") or spec.get("text") or ""
        if not to or not body:
            logger.info("Agent %s email had no recipient/body — ignored.", agent.id)
            return
        outbox = agent.directory / "outbox" / "email"
        try:
            outbox.mkdir(parents=True, exist_ok=True)
            mid = uuid.uuid4().hex
            (outbox / f"{mid}.json").write_text(json.dumps({
                "to": to,
                "cc": spec.get("cc"),
                "subject": spec.get("subject", ""),
                "body": body,
                "in_reply_to": spec.get("in_reply_to"),
                "queued_at": datetime.now(UTC).isoformat(),
            }, ensure_ascii=False), encoding="utf-8")
            logger.info("Agent %s queued email to %s: %s", agent.id, to, spec.get("subject"))
            self._emit("email.queued", agent_id=agent.id, to=to)
        except OSError:
            logger.warning("Could not queue outbound email for %s", agent.id)

    # Blocks that need a human with admin powers (the operator/founder), not
    # just a manager's decision — credentials, access, provisioning, config.
    _OPERATOR_KEYWORDS = (
        "operator", "admin", "provision", "configure", "config", "credential",
        "access", "permission", "token", "directory", "install", "deploy key",
        "dns", "billing", "api key", "secret", "onboard", "add me", "grant",
    )

    def _route_escalation(self, agent: Agent, task_desc: str, escalation: str) -> None:
        """Send a blocked agent's escalation to a human who can act on it.

        Manager for an ordinary block; the founder (with the manager cc'd —
        "boss in flow") when it needs operator/admin powers the manager can't
        grant. This is what turns escalation from a dead local file into a real
        request that reaches someone — the chain of command actually working.
        """
        if self.org is None:
            return

        # A real escalation names something the agent genuinely can't resolve.
        # Don't email the hollow ones ("None", "Nothing's blocking me", "already
        # escalated", "nothing yet") — those were flooding inboxes with
        # look-alike non-blocks. If there's no actual ask, it's not an escalation.
        esc = (escalation or "").strip()
        if len(esc) < 8 or esc.lower().lstrip(" .—-").startswith(
            ("none", "nothing", "n/a", "na ", "no blocker", "nope", "no, ")
        ):
            logger.info(
                "Skipping hollow escalation from %s (no real blocker: %r)",
                agent.id, esc[:60],
            )
            return

        founder = ""
        for c in (self._email_meta().get("contacts") or []):
            a = str(c.get("address") or "").strip()
            if a:
                founder = a
                break

        mgr_email = ""
        mgr_id = self.org.manager_of(agent.id)
        if mgr_id:
            cards = {c["id"]: c for c in self._load_directory_cards()}
            card = cards.get(mgr_id) or {}
            mgr_email = str(card.get("email") or "").strip()

        haystack = f"{task_desc} {escalation}".lower()
        operator_level = any(kw in haystack for kw in self._OPERATOR_KEYWORDS)

        subject = f"[Blocked] {task_desc[:80]}"
        # Factual brief — the node's voice pass rewrites this into the agent's
        # own words before it sends, so keep it plain here.
        body = (
            f"I need to escalate a blocker I can't clear myself.\n\n"
            f"Task: {task_desc}\n\n"
            f"What I need: {esc}\n\n"
            f"— {agent.id}"
        )

        if operator_level and founder:
            spec: dict[str, Any] = {"to": founder, "subject": subject, "body": body}
            if mgr_email:
                spec["cc"] = mgr_email  # keep the manager in the loop
            self._queue_outbound_email(agent, spec)
        elif mgr_email:
            self._queue_outbound_email(
                agent, {"to": mgr_email, "subject": subject, "body": body},
            )
        elif founder:
            self._queue_outbound_email(
                agent, {"to": founder, "subject": subject, "body": body},
            )
        else:
            logger.warning(
                "Escalation for %s could not be routed — no manager/founder email",
                agent.id,
            )

    # ------------------------------------------------------------------
    # Document store — read (delivered by node from HQ) + write (outbox)
    # ------------------------------------------------------------------

    _DOC_VISIBILITIES = ("private", "department", "org")
    _DOC_CONTEXT_CAP = 8  # docs surfaced per wake
    _DOC_SNIPPET_CHARS = 1800  # per-doc content budget in context

    def _queue_outbound_document(self, agent: Agent, spec: dict) -> None:
        """Queue a document to the agent's outbox for the node to store at HQ."""
        import json
        import uuid
        from datetime import UTC, datetime

        title = (spec.get("title") or "").strip()
        content = spec.get("content")
        if content is None:
            content = spec.get("body") or spec.get("text") or ""
        if not title or not content:
            logger.info("Agent %s document had no title/content — ignored.", agent.id)
            return
        vis = (spec.get("visibility") or "private").strip().lower()
        if vis not in self._DOC_VISIBILITIES:
            vis = "private"
        outbox = agent.directory / "outbox" / "documents"
        try:
            outbox.mkdir(parents=True, exist_ok=True)
            did = uuid.uuid4().hex
            (outbox / f"{did}.json").write_text(json.dumps({
                "title": title,
                "content": content,
                "visibility": vis,
                "department": (spec.get("department") or "").strip(),
                "filename": (spec.get("filename") or "").strip(),
                "tags": spec.get("tags") or [],
                "description": (spec.get("description") or "").strip(),
                "queued_at": datetime.now(UTC).isoformat(),
            }, ensure_ascii=False), encoding="utf-8")
            logger.info("Agent %s queued document '%s' (vis=%s)", agent.id, title, vis)
            self._emit("document.queued", agent_id=agent.id, title=title, visibility=vis)
        except OSError:
            logger.warning("Could not queue outbound document for %s", agent.id)

    def _documents_context(self, agent: Agent) -> str:
        """Render the documents the node has delivered to this agent.

        The node writes docs the agent may read (per the store's ACL) as
        ``<agent>/documents/<doc_id>.json`` ({title, description, content,
        visibility, owner, updated_at}). Surfaced standing each wake so the
        agent can use shared knowledge (handbook, finance references, …)."""
        import json

        ddir = agent.directory / "documents"
        if not ddir.is_dir():
            return ""
        files = sorted(p for p in ddir.glob("*.json") if p.is_file())
        if not files:
            return ""
        items: list[dict] = []
        for p in files:
            try:
                items.append(json.loads(p.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
        if not items:
            return ""
        items.sort(key=lambda d: str(d.get("updated_at", "")), reverse=True)
        lines = [
            "## Documents (shared with you)\n",
            "Reference material in the company document store you can read. "
            "Use it where relevant; don't act on it unless it's pertinent.\n",
        ]
        for d in items[: self._DOC_CONTEXT_CAP]:
            title = d.get("title", "untitled")
            owner = d.get("owner_display") or d.get("owner") or ""
            vis = d.get("visibility", "")
            meta = ", ".join(x for x in (vis, f"by {owner}" if owner else "") if x)
            body = (d.get("content") or "").strip()
            if len(body) > self._DOC_SNIPPET_CHARS:
                body = body[: self._DOC_SNIPPET_CHARS] + "\n…(truncated)"
            lines.append(f"\n### {title}" + (f"  _({meta})_" if meta else ""))
            if d.get("description"):
                lines.append(f"_{d['description']}_")
            lines.append(body)
        extra = len(items) - self._DOC_CONTEXT_CAP
        if extra > 0:
            lines.append(f"\n_…and {extra} more document(s) available._")
        return "\n".join(lines)

    def _documents_capability_context(self, agent: Agent) -> str:
        """Standing how-to so any agent can publish a document each wake."""
        return (
            "## Document store\n\n"
            "You can save documents to the company store for yourself or "
            "colleagues to read later. Add a `document` object to your "
            "reflection: "
            '{"title": "...", "content": "<markdown>", "visibility": '
            '"private|department|org", "description": "..."}. '
            "Use **private** for your own working notes, **department** to "
            "share with your team, **org** for company-wide references. "
            "Save a document when the output is worth keeping or others will "
            "need it — a report, a record, a reference — not for routine chatter."
        )

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

    async def _should_replan(self, agent: Agent, messages: list[Any]) -> bool:
        """Decide whether to rethink the plan now.

        Two voices: the agent's own structural check (too many exceptions,
        urgent message, finished-but-blocked) AND the cognitive stack, which
        says "reassess" when something salient has landed — new inbound, a
        finished/failed task. The latter is what turns a drain-the-checklist
        day into a responsive loop: respond to what's come in, re-shape the
        list, rather than only replanning when things break. The plugin is
        responsible for suppressing reassess on an unchanged world (the cost
        guard), so this stays cheap when nothing has happened.
        """
        if agent.needs_replan(messages):
            return True
        try:
            return await self.plugin_manager.dispatch_should_reassess(agent.id, messages)
        except Exception:
            return False

    def _wake_override_active(self, agent: Agent, now: Any = None) -> bool:
        """True if this agent was force-woken (operator/manager) and is still
        inside its grace window — the rota must not re-sleep it yet."""
        until = getattr(agent, "_wake_override_until", None)
        if until is None:
            return False
        from datetime import UTC, datetime

        return (now or datetime.now(UTC)) < until

    def _has_urgent_pending(self, agent: Agent) -> bool:
        """True if the agent still has pending work above routine priority —
        used to hold off exhaustion wind-down so a tired agent never abandons
        something live. Priority >= 1 (HIGH/CRITICAL) counts as urgent."""
        if agent.task_queue is None:
            return False
        return any(
            t.status == "pending" and getattr(t, "priority", 0) >= 1
            for t in agent.task_queue.tasks
        )

    # How often an idle agent (empty queue) runs a proactive look-for-work
    # reassess. Far slower than the 30s heartbeat so a quiet agent keeps hunting
    # for value without burning the consciousness budget or the local model.
    _IDLE_REASSESS_INTERVAL_S = 360.0

    async def _idle_reassess(self, agent: Agent, messages: list[Any]) -> bool:
        """Proactive reassess when the queue is empty — throttled.

        A cleared list is a prompt to find the next valuable thing, not a cue to
        sit inert for the rest of a shift (the bug behind agents clocking hours
        with zero consciousness calls). We reassess proactively at a measured
        cadence so the agent keeps looking for work and stays available, while a
        genuinely-empty outcome is honest ("nothing worth doing, rest"). Real
        reassess activity builds legitimate sleep pressure, so the exhaustion
        wind-down still ends the day. Returns True if it ran a conscious call.
        """
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        last = getattr(agent, "_last_idle_reassess", None)
        if last is not None and (
            now - last
        ).total_seconds() < self._IDLE_REASSESS_INTERVAL_S:
            return False
        agent._last_idle_reassess = now
        try:
            await self._replan(agent, messages, proactive=True)
        except Exception:
            logger.debug(
                "idle proactive reassess failed for %s", agent.id, exc_info=True,
            )
            return False
        return True

    async def _replan(
        self, agent: Agent, messages: list[Any], *, proactive: bool = False,
    ) -> None:
        """Trigger a replan: EXECUTING -> REPLANNING, build new plan, -> EXECUTING.

        ``proactive=True`` is the empty-queue case: the agent has cleared its
        list and is looking for the next valuable thing to do, rather than
        reacting to something that just landed.
        """
        assert agent.task_queue is not None

        if agent.state == AgentState.EXECUTING:
            agent.transition(AgentState.REPLANNING)

        identity = agent.read_all_identity()
        context = await self.context_builder.build_replan_context(agent, identity, messages)

        # Reassess means responding to what's LANDED — so the inbox (mail +
        # GitHub feedback) is part of the picture, not just the existing list.
        # This is what lets a PR review or a colleague's request become work
        # instead of being glanced at once and archived.
        inbox_ctx = self._email_inbox_context(agent)
        if inbox_ctx:
            context = context + "\n\n---\n\n" + inbox_ctx

        # Fresh tested capability status — so a reassess that's about to escalate
        # or work around a "blocker" sees what's actually live first.
        capstat = self._capability_status_context(agent)
        if capstat:
            context = context + "\n\n---\n\n" + capstat

        can_replan = False
        approval = None
        if self.budget_manager:
            approval = self.budget_manager.request_budget(agent.id, "high")
            can_replan = approval.approved
        else:
            can_replan = agent.spend_consciousness()

        if can_replan:
            if proactive:
                prompt = (
                    "You've cleared your task list. That's the start of good "
                    "work, not the end of your day — a capable colleague with a "
                    "clear plate looks for the next most valuable thing to do.\n\n"
                    "Weigh everything above: your responsibilities and goals, "
                    "what you've just finished, anything stuck, messages, and your "
                    "inbox (mail, GitHub reviews/CI/issues).\n\n"
                    "Decide and output a fresh checklist of what to do next:\n"
                    "- PICK UP real inbox work first (a PR to review, a red CI to "
                    "fix, a request to action) — don't leave it sitting unread.\n"
                    "- Then PROACTIVELY propose the most valuable work your role "
                    "should drive now: move a goal forward, unblock a colleague, "
                    "tighten something you own, prepare what's coming. Be concrete "
                    "and specific — real tasks you can start, not vague intentions.\n"
                    "- ESCALATE only a GENUINE blocker you cannot clear yourself "
                    "(emit an `escalation` field naming the thing and who from) "
                    "— but TEST IT FIRST this cycle: check the live capability "
                    "check above, or just try the real action once. Don't "
                    "escalate or route around a block you haven't actually "
                    "tested; an assumption you've carried for days is usually "
                    "already cleared.\n\n"
                    "If — after genuinely looking — there is truly nothing worth "
                    "doing right now, output an empty list and say so plainly; it's "
                    "fine to rest rather than invent busywork. Output ONLY the "
                    "checklist."
                )
            else:
                prompt = (
                    "Reassess your day like a capable colleague mid-shift — don't "
                    "just drain the morning list. Weigh everything above: what you've "
                    "completed, what's stuck (exceptions), messages, and anything "
                    "that's landed in your inbox (mail, GitHub reviews/CI/issues).\n\n"
                    "Decide and output an updated checklist:\n"
                    "- ADD what now needs doing — including work implied by your "
                    "inbox (a PR review to address, a red CI to fix, a request to "
                    "action). Don't leave real work sitting unread.\n"
                    "- REPRIORITISE so the most valuable / time-sensitive work is "
                    "near the top.\n"
                    "- DROP anything stale, superseded, or no longer worth doing — "
                    "a shorter, sharper list beats a stale long one.\n"
                    "- ESCALATE only a GENUINE blocker you truly cannot clear "
                    "yourself — emit an `escalation` field naming the specific "
                    "thing you need and who from. It reaches your manager (or the "
                    "founder for operator actions). Do NOT escalate routine "
                    "status, a task that isn't actually blocked, or something "
                    "you've already handled — no real blocker means no "
                    "escalation. A genuine block must not sit as a dead exception; "
                    "a non-block must not become noise in someone's inbox. And "
                    "TEST a blocker before you escalate or work around it: check "
                    "the live capability check above, or just try the real action "
                    "once. A workaround that avoids the blocked thing keeps you "
                    "blocked — most 'blockers' you've assumed for a while are "
                    "already cleared.\n\n"
                    "Output ONLY the updated checklist of remaining + new tasks."
                )
            response = await self.consciousness.think(
                agent_id=agent.id,
                context=context,
                prompt=prompt,
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
                    elif (
                        action == "sleep"
                        and self._wake_override_active(agent)
                    ):
                        # Force-woken for a crisis — the rota's scheduled sleep
                        # waits until the grace window expires.
                        pass
                    elif action == "sleep" and agent.state in (
                        AgentState.EXECUTING, AgentState.REPLANNING,
                    ):
                        await self.sleep(agent_id)
                    elif action == "replan" and agent.state == AgentState.EXECUTING:
                        await self._replan(agent, [])
                except Exception as e:
                    logger.error(f"Scheduler action {action} for {agent_id}: {e}")

        # Missed-sleep catch-up: an agent still executing during a scheduled
        # sleep gap (the sleep tick was missed, or a restart reset the
        # scheduler's trigger state) would otherwise never clock off or run
        # its pre-sleep journal ritual. Force a clean sleep so no agent is
        # stranded mid-shift.
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        for agent_id, agent in list(self.agents.items()):
            if (
                agent.state in (AgentState.EXECUTING, AgentState.REPLANNING)
                and self._in_sleep_gap(agent_id, now)
                and not self._wake_override_active(agent, now)
            ):
                try:
                    logger.info(
                        "Catch-up sleep for %s — overran its sleep window",
                        agent_id,
                    )
                    await self.sleep(agent_id)
                except Exception as e:
                    logger.error("Catch-up sleep failed for %s: %s", agent_id, e)

        # Exhaustion wind-down: let the BRAIN end the day, not just the rota.
        # An agent the cognitive stack judges spent (high sleep pressure) clocks
        # off early — but only once nothing urgent is still open, so it never
        # abandons live work. The rota remains the outer bound (it'll sleep at
        # its scheduled time regardless); this just lets a genuinely tired agent
        # rest sooner instead of grinding an empty, low-value tail.
        for agent_id, agent in list(self.agents.items()):
            if agent.state not in (AgentState.EXECUTING, AgentState.REPLANNING):
                continue
            if self._has_urgent_pending(agent):
                continue
            if self._wake_override_active(agent, now):
                continue  # force-woken for a crisis — let them work it
            try:
                if await self.plugin_manager.dispatch_should_wind_down(agent_id):
                    logger.info(
                        "Exhaustion wind-down for %s — spent, nothing urgent open",
                        agent_id,
                    )
                    await self.sleep(agent_id)
            except Exception as e:
                logger.error("Exhaustion wind-down failed for %s: %s", agent_id, e)

        # Plugin heartbeat hook
        await self.plugin_manager.dispatch_heartbeat()

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

        async def _handle_agent_wake(
            agent_id: str = "", override_minutes: float = -1.0, **_kw: Any
        ) -> dict[str, Any]:
            if not agent_id:
                return {"ok": False, "error": "agent_id required"}
            try:
                # A relayed wake (from HQ/operator/manager) is a FORCED wake by
                # default — it must stick past the rota. -1 sentinel = use the
                # default grace; an explicit 0 keeps the old rota-respecting wake.
                grace = (
                    self._WAKE_OVERRIDE_MINUTES
                    if override_minutes < 0
                    else override_minutes
                )
                agent = await self.wake(agent_id, override_minutes=grace)
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

        async def _handle_model_perf(**_kw: Any) -> dict[str, Any]:
            """Throughput of the local consciousness model (tokens/sec).

            The node polls this to surface real generation speed in
            Canopy. Empty when the adapter doesn't track perf.
            """
            snap_fn = getattr(self.consciousness, "perf_snapshot", None)
            if not callable(snap_fn):
                return {"ok": True, "perf": {}}
            try:
                return {"ok": True, "perf": snap_fn()}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        async def _handle_shutdown(**_kw: Any) -> dict[str, Any]:
            asyncio.get_event_loop().call_soon(self._request_stop)
            return {"ok": True, "message": "Shutdown initiated"}

        async def _handle_discover(**_kw: Any) -> dict[str, Any]:
            if self.capabilities:
                return {"ok": True, **self.capabilities.to_dict()}
            return {"ok": False, "error": "Discovery not yet run"}

        async def _handle_schedule_optimize(
            agent_id: str = "ar-scheduler", **spec: Any,
        ) -> dict[str, Any]:
            """Run the AR Scheduler's rota optimiser on demand.

            The control surface for HQ/Canopy (and for operators) to have an
            authorised scheduling agent run its tool now. Invokes the exact
            same authority-gated handler the agent uses autonomously.
            """
            agent = self.agents.get(agent_id)
            if agent is None:
                return {"ok": False, "error": f"Unknown agent: {agent_id}"}
            if agent_id not in self.scheduling_authorised:
                return {"ok": False, "error": f"{agent_id} lacks scheduling authority"}
            await self._run_schedule_optimization(agent, dict(spec))
            note_path = agent.directory / "today" / "schedule_optimization.md"
            applied = (self.agents_dir / ".schedules.json").exists()
            return {
                "ok": True,
                "agent_id": agent_id,
                "applied": applied,
                "report": note_path.read_text(encoding="utf-8") if note_path.exists() else "",
            }

        async def _handle_cluster_rebalance(
            agent_id: str = "ar-scheduler", **spec: Any,
        ) -> dict[str, Any]:
            """Plan a node rebalance on demand (advisory, Phase 1).

            The control surface for HQ/Canopy (and operators) to have an
            authorised scheduling agent run its rebalancer now. Invokes the
            exact same authority-gated handler the agent uses autonomously.
            """
            agent = self.agents.get(agent_id)
            if agent is None:
                return {"ok": False, "error": f"Unknown agent: {agent_id}"}
            if agent_id not in self.scheduling_authorised:
                return {"ok": False, "error": f"{agent_id} lacks scheduling authority"}
            await self._run_node_rebalance(agent, dict(spec))
            note_path = agent.directory / "today" / "node_rebalance.md"
            return {
                "ok": True,
                "agent_id": agent_id,
                "report": note_path.read_text(encoding="utf-8") if note_path.exists() else "",
            }

        async def _handle_schedule_health(
            agent_id: str = "ar-scheduler", **_kw: Any,
        ) -> dict[str, Any]:
            """Measure rota responsiveness on demand (read-only). Control
            surface for HQ/Canopy + the AR Scheduler; runs the same
            authority-gated handler."""
            agent = self.agents.get(agent_id)
            if agent is None:
                return {"ok": False, "error": f"Unknown agent: {agent_id}"}
            if agent_id not in self.scheduling_authorised:
                return {"ok": False, "error": f"{agent_id} lacks scheduling authority"}
            await self._run_schedule_health(agent, {})
            note_path = agent.directory / "today" / "schedule_health.md"
            return {
                "ok": True,
                "agent_id": agent_id,
                "report": note_path.read_text(encoding="utf-8") if note_path.exists() else "",
            }

        async def _handle_culture_health(
            agent_id: str = "people-culture-lead", **_kw: Any,
        ) -> dict[str, Any]:
            """Measure culture health on demand (read-only). Control surface
            for HQ/Canopy + the People & Culture Lead; runs the same
            authority-gated handler."""
            agent = self.agents.get(agent_id)
            if agent is None:
                return {"ok": False, "error": f"Unknown agent: {agent_id}"}
            if agent_id not in self.culture_authorised:
                return {"ok": False, "error": f"{agent_id} lacks culture authority"}
            await self._run_culture_health(agent, {})
            note_path = agent.directory / "today" / "culture_health.md"
            return {
                "ok": True,
                "agent_id": agent_id,
                "report": note_path.read_text(encoding="utf-8") if note_path.exists() else "",
            }

        async def _handle_efficiency_review(
            agent_id: str = "workforce-performance-analyst", **_kw: Any,
        ) -> dict[str, Any]:
            """Measure workforce efficiency on demand (read-only). Control
            surface for HQ/Canopy + the analyst; runs the authority-gated
            handler."""
            agent = self.agents.get(agent_id)
            if agent is None:
                return {"ok": False, "error": f"Unknown agent: {agent_id}"}
            if agent_id not in self.performance_authorised:
                return {"ok": False, "error": f"{agent_id} lacks performance authority"}
            await self._run_efficiency_review(agent, {})
            note_path = agent.directory / "today" / "efficiency_review.md"
            return {
                "ok": True,
                "agent_id": agent_id,
                "report": note_path.read_text(encoding="utf-8") if note_path.exists() else "",
            }

        async def _handle_schedule_recommend(
            agent_id: str = "ar-scheduler", **spec: Any,
        ) -> dict[str, Any]:
            """Recommend/apply a single-role re-timing on demand."""
            agent = self.agents.get(agent_id)
            if agent is None:
                return {"ok": False, "error": f"Unknown agent: {agent_id}"}
            if agent_id not in self.scheduling_authorised:
                return {"ok": False, "error": f"{agent_id} lacks scheduling authority"}
            await self._run_schedule_recommendation(agent, dict(spec))
            note_path = agent.directory / "today" / "schedule_recommendation.md"
            return {
                "ok": True,
                "agent_id": agent_id,
                "report": note_path.read_text(encoding="utf-8") if note_path.exists() else "",
            }

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
        server.register("model.perf", _handle_model_perf)
        server.register("discover", _handle_discover)
        server.register("schedule.optimize", _handle_schedule_optimize)
        server.register("schedule.health", _handle_schedule_health)
        server.register("schedule.recommend", _handle_schedule_recommend)
        server.register("culture.health", _handle_culture_health)
        server.register("efficiency.review", _handle_efficiency_review)
        server.register("cluster.rebalance", _handle_cluster_rebalance)
        server.register("cluster.load", _handle_cluster_load)
        server.register("cluster.status", _handle_cluster_status)
        server.register("cluster.nodes", _handle_cluster_nodes)
        server.register("shutdown", _handle_shutdown)

        # Plugin-contributed IPC commands (e.g. Myelin cognition state).
        # Registered last so a plugin cannot shadow a built-in command.
        for cmd, handler in self.plugin_manager.collect_ipc_handlers().items():
            if cmd in server._handlers:
                logger.warning(
                    "Plugin IPC command %r shadows a built-in; skipping", cmd,
                )
                continue
            server.register(cmd, handler)

    def _request_stop(self) -> None:
        """Signal the fabric to stop (used by shutdown IPC command)."""
        self._running = False

    def _load_persisted_schedules(self) -> None:
        """Reload an optimiser-applied rota from ``.schedules.json``.

        The AR Scheduler's applied rota persists here so a fabric restart
        comes back on the optimised schedule rather than the deploy.yaml
        defaults. Only registers agents that still exist.
        """
        import json

        path = self.agents_dir / ".schedules.json"
        if not path.exists():
            return
        try:
            configs = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read persisted schedules at %s", path)
            return
        applied = 0
        for agent_id, cfg in configs.items():
            if agent_id in self.agents and isinstance(cfg, dict):
                self.scheduler.register(agent_id, cfg)
                applied += 1
        if applied:
            logger.info("Reloaded optimiser rota for %d agents", applied)

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
        self._load_persisted_schedules()
        self._reconcile_orphaned_sessions()
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

        # One-time, idempotent: give pre-conviction souls a worldview. Runs in
        # the background so it never blocks boot; skips any soul that already
        # has a Convictions section, so a later reload won't redo it.
        self._convictions_backfill_task = asyncio.create_task(
            self._backfill_convictions()
        )

        logger.info(f"Fabric running with {len(self.agents)} agents")

    async def stop(self) -> None:
        """Stop the fabric. Sleep all active agents and close IPC."""
        logger.info("Stopping Cortiva fabric")
        self._running = False

        # Cancel any in-flight detached dev sessions (graceful reload/stop).
        try:
            await self.dev_sessions.shutdown()
        except Exception:
            logger.debug("dev session shutdown error", exc_info=True)

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

