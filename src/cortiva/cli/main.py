"""
Cortiva CLI — manage your agent organisation from the terminal.

Usage:
    cortiva init <name>                  Initialise a new workspace
    cortiva start                        Start the fabric
    cortiva stop                         Stop the fabric
    cortiva status                       Show agent status
    cortiva agent create <id>            Register a new agent
    cortiva agent create <id> -t <tpl>   Create agent from template
    cortiva agent wake <id>              Wake an agent
    cortiva agent sleep <id>             Put an agent to sleep
    cortiva agent list                   List all agents
    cortiva template list                List available templates
    cortiva config set <key> <val>       Set configuration
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml


def cmd_init(args: argparse.Namespace) -> None:
    """Initialise a new Cortiva workspace."""
    workspace = Path(args.name)
    if workspace.exists():
        print(f"Directory '{args.name}' already exists.")
        sys.exit(1)

    workspace.mkdir(parents=True)
    (workspace / "agents").mkdir()

    config = {
        "fabric": {
            "name": args.name,
            "heartbeat_interval": 30,
        },
        "memory": {
            "adapter": "inmemory",
            "config": {},
        },
        "consciousness": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "budget": {
                "daily_limit": 1000,
                "per_agent_default": 50,
            },
        },
        "routine": {
            "adapter": "ollama",
            "model": "qwen3.5:35b-a3b",
        },
        "channel": {
            "adapter": "slack",
            "config": {},
        },
        "agents": {
            "directory": "./agents",
        },
    }

    config_path = workspace / "cortiva.yaml"
    config_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))

    print(f"Initialised Cortiva workspace: {args.name}/")
    print(f"  Config: {config_path}")
    print(f"  Agents: {workspace / 'agents'}/")
    print()
    print("Next steps:")
    print(f"  cd {args.name}")
    print("  cortiva agent create bookkeep-01")
    print("  cortiva start")


def _try_ipc_status() -> dict | None:
    """Try to get live status from the running daemon.  Returns None on failure."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if not client.is_daemon_running():
        return None
    try:
        return client.send_sync("status")
    except Exception:
        return None


def cmd_status(args: argparse.Namespace) -> None:
    """Show fabric status."""
    config_path = Path("cortiva.yaml")
    if not config_path.exists():
        print("Not a Cortiva workspace. Run 'cortiva init <name>' first.")
        sys.exit(1)

    # Try live status from the daemon first
    live = _try_ipc_status()
    if live and live.get("ok"):
        agents_data = live.get("agents", {})
        running = live.get("running", False)
        status_label = "running" if running else "stopped"
        print(f"Cortiva fabric ({status_label}) — {len(agents_data)} agent(s)\n")
        print(f"  {'Agent':<20} {'State':<14} {'Consciousness':>15} {'Tasks':>8}")
        print(f"  {'-'*20} {'-'*14} {'-'*15} {'-'*8}")
        for aid, info in agents_data.items():
            used = info.get("consciousness_used", 0)
            remaining = info.get("consciousness_remaining", 0)
            budget = f"{used}/{used + remaining}"
            tasks = info.get("tasks_today", 0)
            print(f"  {aid:<20} {info['state']:<14} {budget:>15} {tasks:>8}")
        return

    # Fallback: filesystem scan
    agents_dir = Path("agents")
    if not agents_dir.exists():
        print("No agents directory found.")
        return

    agents = sorted(
        p.name for p in agents_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )

    if not agents:
        print("No agents registered.")
        return

    print(f"Cortiva workspace (daemon not running) — {len(agents)} agent(s)\n")
    for agent_id in agents:
        agent_dir = agents_dir / agent_id
        # Check new subdirectory layout, fall back to flat layout
        identity = agent_dir / "identity" / "identity.md"
        if not identity.exists():
            identity = agent_dir / "identity.md"
        has_identity = "+" if identity.exists() else "-"
        plan = agent_dir / "today" / "plan.md"
        if not plan.exists():
            plan = agent_dir / "plan.md"
        has_plan = "+" if plan.exists() else "-"
        print(f"  {agent_id:<20} identity:{has_identity}  plan:{has_plan}")


def cmd_agent_create(args: argparse.Namespace) -> None:
    """Register a new agent, optionally from a template."""
    config_path = Path("cortiva.yaml")
    if not config_path.exists():
        print("Not a Cortiva workspace. Run 'cortiva init <name>' first.")
        sys.exit(1)

    agent_dir = Path("agents") / args.id
    if agent_dir.exists():
        print(f"Agent '{args.id}' already exists.")
        sys.exit(1)

    template_name = getattr(args, "template", None)

    if template_name:
        from cortiva.templates import apply_template

        try:
            written = apply_template(template_name, agent_dir)
        except KeyError as exc:
            print(str(exc))
            sys.exit(1)

        print(f"Created agent: {args.id} (from template '{template_name}')")
        print(f"  Directory: {agent_dir}/")
        print(f"  Files: {', '.join(written)}")
    else:
        from cortiva.core.agent import WORKSPACE_DIRS

        agent_dir.mkdir(parents=True)
        for subdir in WORKSPACE_DIRS:
            (agent_dir / subdir).mkdir()

        aid = args.id
        files = {
            "identity/identity.md": f"# {aid}\n\nNewly created agent. No experiences yet.\n",
            "identity/soul.md": (
                f"# {aid} — Persona\n\n"
                "Default persona. Configure disposition parameters.\n"
            ),
            "identity/skills.md": f"# {aid} — Skills\n\nNo skills defined yet.\n",
            "identity/responsibilities.md": (
                f"# {aid} — Responsibilities\n\n"
                "## Primary\n\n## Secondary\n\n## Escalation\n"
            ),
            "identity/procedures.md": f"# {aid} — Procedures\n\nNo procedures promoted yet.\n",
            "today/plan.md": (
                f"# {aid} — Plan\n\n"
                "No plan yet. Awaiting first wake cycle.\n"
            ),
        }

        for filename, content in files.items():
            (agent_dir / filename).write_text(content)

        print(f"Created agent: {args.id}")
        print(f"  Directory: {agent_dir}/")
        print()
        print("Edit the identity files to configure this agent:")
        print(f"  {agent_dir}/identity/soul.md              — personality and disposition")
        print(f"  {agent_dir}/identity/skills.md            — domain knowledge")
        print(f"  {agent_dir}/identity/responsibilities.md  — authority boundaries")


def cmd_agent_list(args: argparse.Namespace) -> None:
    """List all agents."""
    cmd_status(args)


def cmd_template_list(args: argparse.Namespace) -> None:
    """List available agent templates."""
    from cortiva.templates import list_templates

    templates = list_templates()
    if not templates:
        print("No templates available.")
        return

    print(f"Available templates ({len(templates)}):\n")
    for name in templates:
        print(f"  {name}")
    print()
    print("Use: cortiva agent create <id> --template <name>")


def cmd_budget(args: argparse.Namespace) -> None:
    """Show consciousness budget status."""
    config_path = Path("cortiva.yaml")
    if not config_path.exists():
        print("Not a Cortiva workspace. Run 'cortiva init <name>' first.")
        sys.exit(1)

    from cortiva.core.config import _build_budget_manager, load_config

    config = load_config(config_path)
    manager = _build_budget_manager(config)
    if manager is None:
        print("No budget manager configured. Add a 'consciousness.budget' section to cortiva.yaml.")
        return

    # Discover agents and register them so we have entries
    agents_dir = Path(config.get("agents", {}).get("directory", "./agents"))
    if agents_dir.exists():
        for p in sorted(agents_dir.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                manager.register_agent(p.name)

    agent_filter = getattr(args, "agent", None)

    if agent_filter:
        status = manager.agent_status(agent_filter)
        if not status.backends:
            print(f"Agent '{agent_filter}' not found.")
            sys.exit(1)
        print(f"Budget detail for {agent_filter}\n")
        for backend_name, info in status.backends.items():
            exhausted = " (EXHAUSTED)" if info["is_exhausted"] else ""
            print(f"  {backend_name}:{exhausted}")
            print(f"    Calls:  {info['calls_used']}/{info['calls_limit']}")
            print(f"    Tokens: {info['tokens_used']}/{info['tokens_limit']}")
        print(f"\n  Task attempts:      {status.task_attempts}")
        print(f"  Consciousness calls: {status.consciousness_calls}")
        print(f"  Escalation ratio:   {status.escalation_ratio:.2f}")
        if status.priority_counts:
            print(f"  Priority counts:    {status.priority_counts}")
    else:
        all_status = manager.all_status()
        if not all_status:
            print("No agents registered.")
            return
        print(f"{'Agent':<20} {'Calls':>8} {'Tokens':>10} {'Esc. Ratio':>12} {'Status':>10}")
        print("-" * 64)
        for agent_id, status in all_status.items():
            state = "EXHAUSTED" if status.exhausted else "OK"
            print(
                f"{agent_id:<20} {status.total_calls:>8} "
                f"{status.total_tokens:>10} {status.escalation_ratio:>11.2f} "
                f"{state:>10}"
            )


def cmd_watch(args: argparse.Namespace) -> None:
    """Show live dashboard of all agents."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if not client.is_daemon_running():
        print("No running Cortiva daemon. Use 'cortiva start' first.")
        sys.exit(1)

    try:
        resp = client.send_sync("watch")
        if not resp or not resp.get("ok"):
            print(f"Failed: {resp.get('error', 'unknown') if resp else 'no response'}")
            sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    agents = resp.get("agents", {})
    if not agents:
        print("No agents registered.")
        return

    print(f"Cortiva Watch — {len(agents)} agent(s)\n")
    header = (
        f"  {'Agent':<20} {'State':<12} {'Task':>6} "
        f"{'Current Work':<35} {'Hours':>7} {'OT':>6} {'Budget':>10}"
    )
    print(header)
    print(f"  {'-'*20} {'-'*12} {'-'*6} {'-'*35} {'-'*7} {'-'*6} {'-'*10}")

    for aid, info in agents.items():
        state = info.get("state", "?")
        progress = info.get("task_progress", "-")
        current = info.get("current_task", "-") or "-"
        if len(current) > 33:
            current = current[:30] + "..."
        hours = info.get("hours_today", 0)
        overtime = info.get("overtime_hours", 0)
        c_used = info.get("consciousness_used", 0)
        c_limit = info.get("consciousness_limit", 0)
        budget = f"{c_used}/{c_limit}"

        ot_str = f"+{overtime:.1f}h" if overtime > 0 else "-"
        print(
            f"  {aid:<20} {state:<12} {progress:>6} "
            f"{current:<35} {hours:>6.1f}h {ot_str:>6} {budget:>10}"
        )

    # Show capacity summary if available
    cap = resp.get("capacity")
    if cap:
        node = cap.get("node", {})
        cont = cap.get("contention", {})
        ag = cap.get("agents", {})
        print()
        print(f"  Node: {node.get('cpu_cores', '?')} cores, "
              f"{node.get('ram_available_gb', '?')}GB RAM free "
              f"({node.get('ram_percent', '?')}% used)")
        print(f"  Agents: {ag.get('active', 0)} active / {ag.get('total', 0)} total "
              f"(max ~{ag.get('max_concurrent', '?')})")
        if cont.get("avg_queue_wait_s", 0) > 0:
            print(f"  Contention: avg queue wait {cont['avg_queue_wait_s']:.1f}s, "
                  f"avg LLM wait {cont.get('avg_consciousness_wait_s', 0):.1f}s, "
                  f"heartbeat util {cont.get('heartbeat_utilisation_pct', 0):.0f}%")
        share = cap.get("agent_share_pct", {})
        if share:
            parts = [f"{aid}: {pct}%" for aid, pct in share.items()]
            print(f"  Heartbeat share: {', '.join(parts)}")


def cmd_capacity(args: argparse.Namespace) -> None:
    """Show detailed node capacity and contention metrics."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if not client.is_daemon_running():
        print("No running Cortiva daemon. Use 'cortiva start' first.")
        sys.exit(1)

    try:
        resp = client.send_sync("capacity")
        if not resp or not resp.get("ok"):
            print(f"Failed: {resp.get('error', 'unknown') if resp else 'no response'}")
            sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    node = resp.get("node", {})
    ag = resp.get("agents", {})
    cont = resp.get("contention", {})
    share = resp.get("agent_share_pct", {})
    tasks = resp.get("recent_tasks", [])

    print("Node Resources\n")
    print(f"  CPU cores:       {node.get('cpu_cores', '?')}")
    print(f"  RAM total:       {node.get('ram_total_gb', '?')} GB")
    print(f"  RAM available:   {node.get('ram_available_gb', '?')} GB ({node.get('ram_percent', '?')}% used)")
    print(f"  Disk free:       {node.get('disk_free_gb', '?')} GB")

    print(f"\nAgent Capacity\n")
    print(f"  Active agents:   {ag.get('active', 0)}")
    print(f"  Total agents:    {ag.get('total', 0)}")
    basis = ag.get("max_concurrent_basis", "")
    print(f"  Max concurrent:  ~{ag.get('max_concurrent', '?')} ({basis})")

    print(f"\nContention\n")
    print(f"  Avg queue wait:        {cont.get('avg_queue_wait_s', 0):>6.1f}s")
    print(f"  Avg execution time:    {cont.get('avg_execution_s', 0):>6.1f}s")
    print(f"  Avg LLM wait:          {cont.get('avg_consciousness_wait_s', 0):>6.1f}s")
    print(f"  Avg heartbeat cycle:   {cont.get('avg_heartbeat_s', 0):>6.1f}s")
    print(f"  Heartbeat idle:        {cont.get('avg_heartbeat_idle_s', 0):>6.1f}s")
    print(f"  Heartbeat utilisation: {cont.get('heartbeat_utilisation_pct', 0):>5.0f}%")

    if share:
        print(f"\nHeartbeat Share (who's using the cycle)\n")
        for aid, pct in sorted(share.items(), key=lambda x: x[1], reverse=True):
            bar = "#" * int(pct / 2)
            print(f"  {aid:<20} {pct:>5.1f}%  {bar}")

    if tasks:
        print(f"\nRecent Tasks\n")
        print(f"  {'Agent':<16} {'Task':<10} {'Queue':>7} {'Exec':>7} {'LLM':>7}")
        print(f"  {'-'*16} {'-'*10} {'-'*7} {'-'*7} {'-'*7}")
        for t in tasks:
            print(
                f"  {t['agent_id']:<16} {t['task_id']:<10} "
                f"{t['queue_wait_s']:>6.1f}s {t['execution_s']:>6.1f}s "
                f"{t['consciousness_wait_s']:>6.1f}s"
            )


def cmd_agent_activity(args: argparse.Namespace) -> None:
    """Show detailed activity for a specific agent."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if not client.is_daemon_running():
        print("No running Cortiva daemon. Use 'cortiva start' first.")
        sys.exit(1)

    try:
        resp = client.send_sync("agent.activity", agent_id=args.id)
        if not resp or not resp.get("ok"):
            print(f"Failed: {resp.get('error', 'unknown') if resp else 'no response'}")
            sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    ts = resp.get("timesheet", {})
    print(f"Agent: {resp['agent_id']}  |  State: {resp['state']}")
    print(f"Today: {ts.get('date', '?')}  |  Hours: {ts.get('total_hours', 0):.1f}h")
    if ts.get("overtime_hours", 0) > 0:
        print(f"Overtime: +{ts['overtime_hours']:.1f}h (scheduled: {ts.get('scheduled_hours', 8)}h)")
    print()

    # Current task
    current = resp.get("current_task")
    if current:
        print(f"In Progress:")
        print(f"  {current['description']}")
        print()

    # Completed tasks
    completed = resp.get("completed_tasks", [])
    if completed:
        print(f"Completed ({len(completed)}):")
        for t in completed:
            print(f"  {t['description']}")
        print()

    # Pending tasks
    pending = resp.get("pending_tasks", [])
    if pending:
        print(f"Pending ({len(pending)}):")
        for t in pending:
            print(f"  {t['description']}")
        print()

    # Session turns
    turns = resp.get("session_turns", [])
    if turns:
        print(f"Session ({len(turns)} turns):")
        for turn in turns:
            label = turn.get("call_type") or turn.get("role", "?")
            content = turn.get("content", "")
            if len(content) > 80:
                content = content[:77] + "..."
            print(f"  [{label}] {content}")


def cmd_agent_hours(args: argparse.Namespace) -> None:
    """Show working hours for an agent."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    use_live = client.is_daemon_running()

    period = "week" if getattr(args, "week", False) else "today"

    if use_live:
        try:
            resp = client.send_sync("agent.hours", agent_id=args.id, period=period)
            if resp and resp.get("ok"):
                _print_hours(resp)
                return
        except Exception:
            pass

    # Fallback: read from filesystem
    from cortiva.core.timesheet import Timesheet

    agent_dir = Path("agents") / args.id
    if not agent_dir.exists():
        print(f"Agent '{args.id}' not found.")
        sys.exit(1)

    ts = Timesheet(agent_dir)
    if period == "week":
        week = ts.week()
        total_hours = sum(d.total_hours for d in week)
        total_overtime = sum(d.overtime_hours for d in week)
        resp = {
            "agent_id": args.id,
            "period": "week",
            "total_hours": round(total_hours, 2),
            "total_overtime": round(total_overtime, 2),
            "days": [d.to_dict() for d in week],
        }
    else:
        today = ts.today()
        resp = {"agent_id": args.id, "period": "today", **today.to_dict()}

    _print_hours(resp)


def _print_hours(resp: dict) -> None:
    """Format and print hours data."""
    agent_id = resp.get("agent_id", "?")

    if resp.get("period") == "week":
        total = resp.get("total_hours", 0)
        overtime = resp.get("total_overtime", 0)
        print(f"Agent: {agent_id}  |  This Week\n")
        print(f"  {'Day':<12} {'Hours':>7} {'Scheduled':>10} {'Overtime':>9} {'Tasks':>7}")
        print(f"  {'-'*12} {'-'*7} {'-'*10} {'-'*9} {'-'*7}")
        for day in resp.get("days", []):
            h = day.get("total_hours", 0)
            s = day.get("scheduled_hours", 8)
            ot = day.get("overtime_hours", 0)
            tasks = day.get("tasks_completed", 0)
            ot_str = f"+{ot:.1f}h" if ot > 0 else "-"
            print(f"  {day['date']:<12} {h:>6.1f}h {s:>9.0f}h {ot_str:>9} {tasks:>7}")
        print(f"  {'-'*12} {'-'*7} {'-'*10} {'-'*9}")
        ot_str = f"+{overtime:.1f}h" if overtime > 0 else "-"
        print(f"  {'Total':<12} {total:>6.1f}h {'':>10} {ot_str:>9}")
    else:
        h = resp.get("total_hours", 0)
        s = resp.get("scheduled_hours", 8)
        ot = resp.get("overtime_hours", 0)
        tasks = resp.get("tasks_completed", 0)
        escalated = resp.get("tasks_escalated", 0)
        print(f"Agent: {agent_id}  |  Today: {resp.get('date', '?')}\n")
        print(f"  Hours worked:  {h:.1f}h")
        print(f"  Scheduled:     {s:.0f}h")
        if ot > 0:
            print(f"  Overtime:      +{ot:.1f}h")
        print(f"  Tasks done:    {tasks}")
        print(f"  Escalated:     {escalated}")

        entries = resp.get("entries", [])
        if entries:
            print(f"\n  Work Periods:")
            for e in entries:
                wake = e.get("wake_time", "?")[:16]
                sleep = e.get("sleep_time")
                sleep_str = sleep[:16] if sleep else "still working"
                print(f"    {wake} → {sleep_str}  ({e.get('hours', 0):.1f}h)")


def cmd_skill_list(args: argparse.Namespace) -> None:
    """List available skills or installed skills for an agent."""
    from cortiva.core.skills import SkillRegistry

    registry = SkillRegistry()
    registry.load_bundled()

    agent_id = getattr(args, "agent", None)
    if agent_id:
        from cortiva.core.skills import installed_skills

        agent_dir = Path("agents") / agent_id
        if not agent_dir.exists():
            print(f"Agent '{agent_id}' not found.")
            sys.exit(1)
        skills = installed_skills(agent_dir)
        if not skills:
            print(f"No skills installed for {agent_id}.")
            return
        print(f"Skills installed for {agent_id} ({len(skills)}):\n")
        for name in skills:
            skill = registry.get(name)
            desc = skill.description if skill else ""
            print(f"  {name:<25} {desc}")
        return

    category = getattr(args, "category", None)
    query = getattr(args, "query", "") or ""

    if category:
        results = registry.search(query=query, category=category)
    elif query:
        results = registry.search(query=query)
    else:
        results = registry.all_skills()

    if not results:
        print("No skills found.")
        return

    cats = registry.categories()
    print(f"Cortiva Skill Registry — {registry.count} skills, {len(cats)} categories\n")

    if not query and not category:
        # Show category summary
        for cat, count in cats.items():
            print(f"  {cat:<30} {count:>4} skills")
        print(f"\nUse: cortiva skill list --category <name>")
        print(f"     cortiva skill search <query>")
        return

    print(f"  {'Skill':<25} {'Category':<22} Description")
    print(f"  {'-'*25} {'-'*22} {'-'*40}")
    for skill in results:
        desc = skill.description[:40] if skill.description else ""
        print(f"  {skill.name:<25} {skill.category:<22} {desc}")


def cmd_skill_search(args: argparse.Namespace) -> None:
    """Search for skills."""
    from cortiva.core.skills import SkillRegistry

    registry = SkillRegistry()
    registry.load_bundled()

    query = getattr(args, "query", "")
    results = registry.search(query=query)

    if not results:
        print(f"No skills matching '{query}'.")
        return

    print(f"Found {len(results)} skills matching '{query}':\n")
    print(f"  {'Skill':<25} {'Category':<22} Description")
    print(f"  {'-'*25} {'-'*22} {'-'*40}")
    for skill in results:
        desc = skill.description[:40] if skill.description else ""
        print(f"  {skill.name:<25} {skill.category:<22} {desc}")
    print(f"\nInstall with: cortiva skill install <name> --agent <agent-id>")


def cmd_skill_install(args: argparse.Namespace) -> None:
    """Install a skill for an agent."""
    from cortiva.core.skills import SkillRegistry, install_skill

    agent_dir = Path("agents") / args.agent
    if not agent_dir.exists():
        print(f"Agent '{args.agent}' not found.")
        sys.exit(1)

    registry = SkillRegistry()
    registry.load_bundled()

    skill = registry.get(args.name)
    if skill is None:
        print(f"Skill '{args.name}' not found in registry.")
        print(f"Use 'cortiva skill search' to find available skills.")
        sys.exit(1)

    try:
        modified = install_skill(agent_dir, skill)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    print(f"Installed skill: {skill.name}")
    print(f"  Agent: {args.agent}")
    print(f"  Description: {skill.description}")
    if skill.mcp:
        print(f"  MCP server: {skill.mcp.command}")
        if skill.mcp.env:
            print(f"  Required env: {', '.join(skill.mcp.env)}")
    print(f"  Files modified: {', '.join(modified)}")


def cmd_skill_uninstall(args: argparse.Namespace) -> None:
    """Uninstall a skill from an agent."""
    from cortiva.core.skills import uninstall_skill

    agent_dir = Path("agents") / args.agent
    if not agent_dir.exists():
        print(f"Agent '{args.agent}' not found.")
        sys.exit(1)

    try:
        modified = uninstall_skill(agent_dir, args.name)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    print(f"Uninstalled skill: {args.name}")
    print(f"  Agent: {args.agent}")
    print(f"  Files modified: {', '.join(modified)}")


def cmd_skill_info(args: argparse.Namespace) -> None:
    """Show detailed info about a skill."""
    from cortiva.core.skills import SkillRegistry

    registry = SkillRegistry()
    registry.load_bundled()

    skill = registry.get(args.name)
    if skill is None:
        print(f"Skill '{args.name}' not found.")
        sys.exit(1)

    print(f"Skill: {skill.name}")
    print(f"  Description: {skill.description}")
    print(f"  Category:    {skill.category}")
    print(f"  Version:     {skill.version}")
    if skill.tags:
        print(f"  Tags:        {', '.join(skill.tags)}")
    if skill.mcp:
        print(f"\n  MCP Server:")
        print(f"    Package:   {skill.mcp.package}")
        print(f"    Command:   {skill.mcp.command}")
        if skill.mcp.env:
            print(f"    Env vars:  {', '.join(skill.mcp.env)}")
    if skill.procedures:
        print(f"\n  Procedures:")
        for line in skill.procedures.strip().splitlines():
            print(f"    {line}")


def cmd_discover(args: argparse.Namespace) -> None:
    """Run node capability discovery and display results."""
    import asyncio as _asyncio

    from cortiva.core.discovery import NodeCapabilities

    custom_endpoints: list[dict] | None = None
    config_path = Path("cortiva.yaml")
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text()) or {}
        cluster = config.get("cluster", {})
        eps = cluster.get("endpoints")
        if eps and isinstance(eps, list):
            custom_endpoints = eps

    async def _run() -> NodeCapabilities:
        import os as _os
        import platform as _platform
        node_id = f"{_platform.node()}-{_os.getpid()}"
        return await NodeCapabilities.discover(
            node_id, custom_endpoints=custom_endpoints,
        )

    caps = _asyncio.run(_run())

    print(f"Node: {caps.node_id}\n")

    print("Terminal Agents:")
    if caps.terminal_agents:
        for t in caps.terminal_agents:
            status = "available" if t.available else "not found"
            auth = " (auth ok)" if t.auth_ok else ""
            ver = f" [{t.version}]" if t.version else ""
            print(f"  {t.name:<14} {status}{auth}{ver}")
    else:
        print("  (none discovered)")

    print(f"\nLocal Models ({len(caps.local_models)}):")
    if caps.local_models:
        for m in caps.local_models:
            size_gb = m.size_bytes / (1024 ** 3) if m.size_bytes else 0
            size_str = f"{size_gb:.1f}GB" if size_gb else ""
            print(f"  {m.name:<30} {m.parameter_size:<8} {size_str}")
    else:
        print("  (none — is Ollama running?)")

    if caps.custom_endpoints:
        print(f"\nCustom Endpoints ({len(caps.custom_endpoints)}):")
        for e in caps.custom_endpoints:
            health = "healthy" if e.healthy else "unreachable"
            print(f"  {e.name:<20} {e.url:<40} {health}")

    print(f"\nResources:")
    r = caps.resources
    print(f"  CPU:      {r.cpu_cores} cores")
    print(f"  RAM:      {r.ram_available_gb:.1f}GB free / {r.ram_total_gb:.1f}GB total")
    print(f"  Disk:     {r.disk_free_gb:.0f}GB free / {r.disk_total_gb:.0f}GB total")
    print(f"  Platform: {r.platform}")
    print(f"  Python:   {r.python_version}")


def cmd_bootstrap(args: argparse.Namespace) -> None:
    """Bootstrap the three-agent Cortiva development team."""
    workspace = Path(args.dir) if hasattr(args, "dir") and args.dir else Path(".")
    agents_dir = workspace / "agents"
    config_path = workspace / "cortiva.yaml"

    from cortiva.templates import apply_template, list_templates

    available = list_templates()
    team = ["dev-cortiva", "qa-cortiva", "pm-cortiva"]
    missing = [t for t in team if t not in available]
    if missing:
        print(f"Missing templates: {', '.join(missing)}")
        print(f"Available: {', '.join(available)}")
        sys.exit(1)

    # Create workspace structure
    agents_dir.mkdir(parents=True, exist_ok=True)

    created = []
    for agent_name in team:
        agent_dir = agents_dir / agent_name
        if agent_dir.exists():
            print(f"  {agent_name}: already exists, skipping")
            continue
        written = apply_template(agent_name, agent_dir)
        created.append(agent_name)
        print(f"  {agent_name}: created ({len(written)} files)")

    # Generate cortiva.yaml if it doesn't exist
    if not config_path.exists():
        config = {
            "fabric": {
                "name": "cortiva-bootstrap",
                "heartbeat_interval": 30,
            },
            "memory": {
                "adapter": "inmemory",
                "config": {},
            },
            "consciousness": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "budget": {
                    "daily_limit": 1000,
                },
            },
            "terminal": {
                "adapter": "claude-code",
            },
            "agents": {
                "directory": "./agents",
            },
            "schedules": {
                "dev-cortiva": {
                    "wake": "09:00 mon-fri",
                    "replan": "13:00",
                    "sleep": "17:00",
                },
                "qa-cortiva": {
                    "wake": "09:30 mon-fri",
                    "sleep": "17:00",
                },
                "pm-cortiva": {
                    "wake": "08:30 mon-fri",
                    "replan": "12:00,15:00",
                    "sleep": "17:30",
                },
            },
        }
        config_path.write_text(
            yaml.dump(config, default_flow_style=False, sort_keys=False)
        )
        print(f"\n  Config: {config_path}")
    else:
        print(f"\n  Config: {config_path} (already exists, not overwritten)")

    print(f"\nBootstrap complete: {len(created)} agent(s) created.")
    if created:
        print("\nNext steps:")
        print(f"  cd {workspace}")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        print("  cortiva start")
        print("  cortiva agent wake dev-cortiva")


def cmd_start(args: argparse.Namespace) -> None:
    """Start the Cortiva fabric."""
    # Set process name so it shows as "cortiva" in Activity Monitor / ps
    try:
        import setproctitle
        setproctitle.setproctitle("cortiva")
    except ImportError:
        pass

    config_path = Path("cortiva.yaml")
    if not config_path.exists():
        print("Not a Cortiva workspace. Run 'cortiva init <name>' first.")
        sys.exit(1)

    from cortiva.core.config import load_and_build
    from cortiva.core.ipc import (
        default_pid_path,
        default_socket_path,
        is_pid_alive,
        read_pid,
        remove_pid,
        write_pid,
    )

    # Check if another daemon is already running
    existing_pid = read_pid()
    if existing_pid is not None and is_pid_alive(existing_pid):
        print(f"Cortiva daemon already running (PID {existing_pid}).")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    try:
        fabric = load_and_build(config_path)
    except Exception as exc:
        print(f"Failed to load config: {exc}")
        sys.exit(1)

    socket_path = default_socket_path()
    pid_path = default_pid_path()
    loop = asyncio.new_event_loop()

    def _shutdown(signum: int, frame: object) -> None:
        print("\nShutting down...")
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Optional portal co-start
    portal_host = getattr(args, "portal_host", "127.0.0.1")
    portal_port = getattr(args, "portal_port", 8400)
    start_portal = getattr(args, "portal", False)

    async def _run() -> None:
        await fabric.start(ipc_socket=socket_path)
        write_pid(pid_path)
        print(f"Cortiva fabric running ({len(fabric.agents)} agents). Press Ctrl+C to stop.")

        if start_portal:
            try:
                import uvicorn

                from cortiva.portal.server import create_app

                agents_dir = str(fabric.agents_dir)
                app = create_app(agents_dir=agents_dir)
                portal_config = uvicorn.Config(
                    app, host=portal_host, port=portal_port, log_level="info",
                )
                server = uvicorn.Server(portal_config)
                print(f"Portal running on http://{portal_host}:{portal_port}")
                asyncio.ensure_future(server.serve())
            except ImportError:
                print("Portal requires uvicorn. Install with: pip install 'cortiva[portal]'")

        try:
            while fabric._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await fabric.stop()
            remove_pid(pid_path)
            print("Fabric stopped.")

    try:
        loop.run_until_complete(_run())
    except RuntimeError:
        # Loop was stopped by signal handler
        loop.run_until_complete(fabric.stop())
        remove_pid(pid_path)
        print("Fabric stopped.")
    finally:
        loop.close()


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop the running Cortiva daemon."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if not client.is_daemon_running():
        print("No running Cortiva daemon found.")
        sys.exit(1)

    try:
        resp = client.send_sync("shutdown")
        if resp.get("ok"):
            print("Shutdown signal sent. Daemon will stop after sleeping active agents.")
        else:
            print(f"Shutdown failed: {resp.get('error', 'unknown error')}")
            sys.exit(1)
    except Exception as exc:
        print(f"Failed to connect to daemon: {exc}")
        sys.exit(1)


def cmd_agent_wake(args: argparse.Namespace) -> None:
    """Wake an agent via the running daemon."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if not client.is_daemon_running():
        print("No running Cortiva daemon. Use 'cortiva start' first.")
        sys.exit(1)

    try:
        resp = client.send_sync("agent.wake", agent_id=args.id)
        if resp.get("ok"):
            print(f"Agent {args.id} is now {resp.get('state', 'active')}.")
        else:
            print(f"Failed: {resp.get('error', 'unknown error')}")
            sys.exit(1)
    except Exception as exc:
        print(f"Failed to connect to daemon: {exc}")
        sys.exit(1)


def cmd_agent_sleep(args: argparse.Namespace) -> None:
    """Put an agent to sleep via the running daemon."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if not client.is_daemon_running():
        print("No running Cortiva daemon. Use 'cortiva start' first.")
        sys.exit(1)

    try:
        resp = client.send_sync("agent.sleep", agent_id=args.id)
        if resp.get("ok"):
            print(f"Agent {args.id} is now {resp.get('state', 'sleeping')}.")
        else:
            print(f"Failed: {resp.get('error', 'unknown error')}")
            sys.exit(1)
    except Exception as exc:
        print(f"Failed to connect to daemon: {exc}")
        sys.exit(1)


def cmd_agent_snapshot(args: argparse.Namespace) -> None:
    """Create a snapshot of an agent."""
    from cortiva.core.snapshots import create_snapshot

    agent_dir = Path("agents") / args.id
    if not agent_dir.exists():
        print(f"Agent '{args.id}' not found.")
        sys.exit(1)

    meta = create_snapshot(agent_dir, name=args.name, description=args.description)
    print(f"Snapshot created: {meta.snapshot_id}")
    if meta.name != meta.snapshot_id:
        print(f"  Name: {meta.name}")
    print(f"  Agent: {meta.agent_id}")
    print(f"  Time: {meta.created_at}")


def cmd_agent_snapshots(args: argparse.Namespace) -> None:
    """List snapshots for an agent."""
    from cortiva.core.snapshots import list_snapshots

    agent_dir = Path("agents") / args.id
    if not agent_dir.exists():
        print(f"Agent '{args.id}' not found.")
        sys.exit(1)

    snapshots = list_snapshots(agent_dir)
    if not snapshots:
        print(f"No snapshots for {args.id}.")
        return

    print(f"Snapshots for {args.id} ({len(snapshots)}):\n")
    for s in snapshots:
        name = f" ({s.name})" if s.name != s.snapshot_id else ""
        print(f"  {s.snapshot_id}{name}  [{s.trigger}]  {s.created_at}")


def cmd_agent_rollback(args: argparse.Namespace) -> None:
    """Rollback an agent to a snapshot."""
    from cortiva.core.snapshots import restore_snapshot

    agent_dir = Path("agents") / args.id
    if not agent_dir.exists():
        print(f"Agent '{args.id}' not found.")
        sys.exit(1)

    restore_journal = not getattr(args, "no_journal", False)
    if restore_snapshot(agent_dir, args.snapshot, restore_journal=restore_journal):
        print(f"Agent {args.id} restored from snapshot {args.snapshot}.")
    else:
        print(f"Snapshot '{args.snapshot}' not found.")
        sys.exit(1)


def cmd_agent_clone(args: argparse.Namespace) -> None:
    """Clone an agent from a snapshot."""
    from cortiva.core.snapshots import clone_from_snapshot, list_snapshots

    agent_dir = Path("agents") / args.id
    if not agent_dir.exists():
        print(f"Source agent '{args.id}' not found.")
        sys.exit(1)

    new_dir = Path("agents") / args.new_id
    if new_dir.exists():
        print(f"Agent '{args.new_id}' already exists.")
        sys.exit(1)

    snapshot_id = args.from_snapshot
    if snapshot_id == "latest":
        snapshots = list_snapshots(agent_dir)
        if not snapshots:
            print(f"No snapshots for {args.id}. Create one first: cortiva agent snapshot {args.id}")
            sys.exit(1)
        snapshot_id = snapshots[0].snapshot_id

    if clone_from_snapshot(agent_dir, snapshot_id, new_dir):
        print(f"Cloned {args.id} -> {args.new_id} (from snapshot {snapshot_id})")
    else:
        print(f"Snapshot '{snapshot_id}' not found.")
        sys.exit(1)


def cmd_agent_promote(args: argparse.Namespace) -> None:
    """Promote an agent to a new role."""
    from cortiva.core.promotion import initiate_promotion
    from cortiva.templates import get_template_path

    agent_dir = Path("agents") / args.id
    if not agent_dir.exists():
        print(f"Agent '{args.id}' not found.")
        sys.exit(1)

    try:
        tpl_path = get_template_path(args.to)
    except KeyError:
        # Try as a direct agent directory
        tpl_path = Path("agents") / args.to
        if not tpl_path.exists():
            print(f"Role template '{args.to}' not found as template or agent directory.")
            sys.exit(1)

    record = initiate_promotion(agent_dir, tpl_path, probation_days=args.probation)
    print(f"Promotion initiated for {args.id}")
    print(f"  {record.source_role} -> {record.target_role}")
    print(f"  Probation: {record.probation_config.duration_days} days (until {record.probation_end[:10]})")
    print(f"  Snapshot: {record.pre_promotion_snapshot}")


def cmd_agent_probation(args: argparse.Namespace) -> None:
    """Manage agent probation."""
    from cortiva.core.promotion import (
        confirm_promotion,
        extend_probation,
        get_promotion,
        revert_promotion,
    )

    agent_dir = Path("agents") / args.id
    if not agent_dir.exists():
        print(f"Agent '{args.id}' not found.")
        sys.exit(1)

    if getattr(args, "confirm", False):
        record = confirm_promotion(agent_dir)
        if record:
            print(f"Promotion confirmed for {args.id}.")
        else:
            print(f"No active probation for {args.id}.")
            sys.exit(1)
    elif getattr(args, "revert", False):
        record = revert_promotion(agent_dir)
        if record:
            print(f"Promotion reverted for {args.id}. Restored from snapshot {record.pre_promotion_snapshot}.")
        else:
            print(f"No active probation for {args.id}.")
            sys.exit(1)
    elif args.extend:
        record = extend_probation(agent_dir, additional_days=args.extend)
        if record:
            print(f"Probation extended by {args.extend} days for {args.id}.")
            print(f"  New end: {record.probation_end[:10]}")
        else:
            print(f"No active probation for {args.id}.")
            sys.exit(1)


def cmd_agent_export(args: argparse.Namespace) -> None:
    """Export an agent to a tarball."""
    import shutil
    import tarfile
    import tempfile

    agent_dir = Path("agents") / args.id
    if not agent_dir.exists():
        print(f"Agent '{args.id}' not found.")
        sys.exit(1)

    output = Path(args.output) if args.output else Path(f"{args.id}.tar.gz")
    sanitise = getattr(args, "sanitise", False)

    with tempfile.TemporaryDirectory() as tmp:
        export_dir = Path(tmp) / args.id
        shutil.copytree(agent_dir, export_dir)

        # Remove runtime state — only export identity and journal
        for subdir in ("today", "outbox", "workspace"):
            d = export_dir / subdir
            if d.is_dir():
                shutil.rmtree(d)

        # Optionally sanitise: strip potential company-specific data
        if sanitise:
            for md_file in export_dir.rglob("*.md"):
                content = md_file.read_text(encoding="utf-8")
                # Remove email addresses and URLs
                import re
                content = re.sub(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b', '[REDACTED]', content)
                content = re.sub(r'https?://\S+', '[URL_REDACTED]', content)
                md_file.write_text(content, encoding="utf-8")

        with tarfile.open(output, "w:gz") as tar:
            tar.add(export_dir, arcname=args.id)

    print(f"Exported {args.id} -> {output}")
    if sanitise:
        print("  (sanitised: emails and URLs redacted)")


def cmd_agent_import(args: argparse.Namespace) -> None:
    """Import an agent from a tarball."""
    import tarfile

    archive = Path(args.archive)
    if not archive.exists():
        print(f"Archive not found: {archive}")
        sys.exit(1)

    new_id = args.new_id
    agent_dir = Path("agents") / new_id

    if agent_dir.exists():
        print(f"Agent '{new_id}' already exists.")
        sys.exit(1)

    with tarfile.open(archive, "r:gz") as tar:
        # Security: check for path traversal
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in member.name:
                print(f"Unsafe path in archive: {member.name}")
                sys.exit(1)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tar.extractall(tmp)
            # Find the extracted directory (first dir in tmp)
            extracted = list(Path(tmp).iterdir())
            if not extracted:
                print("Empty archive.")
                sys.exit(1)
            src = extracted[0]
            import shutil
            shutil.copytree(src, agent_dir)

    # Ensure workspace dirs exist
    from cortiva.core.agent import WORKSPACE_DIRS
    for subdir in WORKSPACE_DIRS:
        (agent_dir / subdir).mkdir(parents=True, exist_ok=True)

    print(f"Imported {archive.name} -> {new_id}")
    print(f"  Directory: {agent_dir}/")


def cmd_portal(args: argparse.Namespace) -> None:
    """Start the Cortiva web portal."""
    config_path = Path("cortiva.yaml")
    if not config_path.exists():
        print("Not a Cortiva workspace. Run 'cortiva init <name>' first.")
        sys.exit(1)

    try:
        import uvicorn
    except ImportError:
        print("Portal requires uvicorn. Install with: pip install 'cortiva[portal]'")
        sys.exit(1)

    from cortiva.portal.server import create_app

    agents_dir = "./agents"
    if config_path.exists():
        import yaml as _yaml
        cfg = _yaml.safe_load(config_path.read_text()) or {}
        agents_dir = cfg.get("agents", {}).get("directory", "./agents")

    app = create_app(agents_dir=agents_dir)
    print(f"Cortiva Portal starting on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


def cmd_cluster_status(args: argparse.Namespace) -> None:
    """Show cluster status: nodes, registry, and available models."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if not client.is_daemon_running():
        print("No running Cortiva daemon. Use 'cortiva start' first.")
        sys.exit(1)

    try:
        resp = client.send_sync("cluster.status")
        if not resp or not resp.get("ok"):
            print(f"Failed: {resp.get('error', 'unknown error') if resp else 'no response'}")
            sys.exit(1)
    except Exception as exc:
        print(f"Failed to connect to daemon: {exc}")
        sys.exit(1)

    print(f"Cluster Status\n")
    print(f"  Local node:     {resp.get('local_node_id', '?')}")
    print(f"  Discovery:      {resp.get('discovery_mode', '?')}")
    print(f"  Nodes:          {resp.get('node_count', 0)}")
    print(f"  Single-node:    {'yes' if resp.get('single_node') else 'no'}")

    registry = resp.get("registry", {})
    if registry:
        print(f"\n  Agent Registry ({len(registry)}):")
        for agent_id, node_id in registry.items():
            print(f"    {agent_id:<20} -> {node_id}")

    models = resp.get("models", [])
    if models:
        print(f"\n  Available Models ({len(models)}):")
        for m in models:
            print(f"    {m}")


def cmd_cluster_nodes(args: argparse.Namespace) -> None:
    """Show cluster nodes with details."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if not client.is_daemon_running():
        print("No running Cortiva daemon. Use 'cortiva start' first.")
        sys.exit(1)

    try:
        resp = client.send_sync("cluster.nodes")
        if not resp or not resp.get("ok"):
            print(f"Failed: {resp.get('error', 'unknown error') if resp else 'no response'}")
            sys.exit(1)
    except Exception as exc:
        print(f"Failed to connect to daemon: {exc}")
        sys.exit(1)

    nodes = resp.get("nodes", [])
    if not nodes:
        print("No nodes in cluster.")
        return

    print(f"Cluster Nodes ({len(nodes)})\n")
    for node in nodes:
        status = node.get("status", "unknown")
        print(f"  {node.get('node_id', '?')} ({status})")
        print(f"    Host:    {node.get('host', '?')}:{node.get('port', '?')}")
        agents = node.get("agents", [])
        if agents:
            print(f"    Agents:  {', '.join(agents)}")
        else:
            print(f"    Agents:  (none)")
        hb = node.get("last_heartbeat", "")
        if hb:
            print(f"    Heartbeat: {hb}")
        print()


def cmd_agent_move(args: argparse.Namespace) -> None:
    """Move an agent to another node via the running daemon."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if not client.is_daemon_running():
        print("No running Cortiva daemon. Use 'cortiva start' first.")
        sys.exit(1)

    try:
        resp = client.send_sync("agent.move", agent_id=args.id, target_node=args.to)
        if resp.get("ok"):
            print(
                f"Agent {args.id} moved: "
                f"{resp.get('source_node', '?')} -> {resp.get('target_node', '?')}"
            )
        else:
            error = resp.get("error", "unknown error")
            print(f"Move failed: {error}")
            sys.exit(1)
    except Exception as exc:
        print(f"Failed to connect to daemon: {exc}")
        sys.exit(1)


def cmd_cluster_load(args: argparse.Namespace) -> None:
    """Show cluster load metrics and balancing suggestions."""
    from cortiva.core.ipc import FabricClient

    client = FabricClient()
    if client.is_daemon_running():
        try:
            result = client.send_sync("cluster.load")
            if result and result.get("ok"):
                _print_cluster_load(
                    result.get("nodes", []),
                    result.get("affinities", {}),
                    result.get("moves", []),
                )
                return
        except Exception:
            pass

    # Offline mode: run discovery locally and show snapshot
    import asyncio as _asyncio
    import os as _os
    import platform as _platform

    from cortiva.core.balancer import ClusterMetrics, CommunicationTracker
    from cortiva.core.discovery import NodeCapabilities

    custom_endpoints: list[dict] | None = None
    config_path = Path("cortiva.yaml")
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text()) or {}
        cluster = config.get("cluster", {})
        eps = cluster.get("endpoints")
        if eps and isinstance(eps, list):
            custom_endpoints = eps

    async def _run() -> NodeCapabilities:
        node_id = f"{_platform.node()}-{_os.getpid()}"
        return await NodeCapabilities.discover(
            node_id, custom_endpoints=custom_endpoints,
        )

    caps = _asyncio.run(_run())
    tracker = CommunicationTracker()
    metrics = ClusterMetrics(communication_tracker=tracker)
    nodes = metrics.snapshot(caps, {})
    affinities = metrics.agent_affinity_scores()
    moves = metrics.suggest_moves()

    _print_cluster_load(
        [n.to_dict() for n in nodes],
        {f"{a}->{b}": s for (a, b), s in affinities.items()},
        [m.to_dict() for m in moves],
    )


def _print_cluster_load(
    nodes: list[dict],
    affinities: dict[str, float],
    moves: list[dict],
) -> None:
    """Format and print cluster load information."""
    if not nodes:
        print("No node data available.")
        return

    print("Cluster Load\n")
    for node in nodes:
        print(f"  Node: {node.get('node_id', '?')}")
        total = node.get("agent_count", 0)
        active = node.get("active_agent_count", 0)
        print(f"    Agents:  {total} total, {active} active")
        ram = node.get("ram_usage_ratio", 0)
        budget = node.get("budget_exhaustion_ratio", 0)
        print(f"    RAM:     {ram:.0%} used")
        print(f"    Budget:  {budget:.0%} exhausted")
        res = node.get("resources", {})
        if res:
            print(f"    CPU:     {res.get('cpu_cores', '?')} cores")
            avail = res.get("ram_available_gb", "?")
            total_ram = res.get("ram_total_gb", "?")
            print(f"    RAM:     {avail}GB free / {total_ram}GB total")
        print()

    if affinities:
        print("Agent Affinities:")
        for pair, score in sorted(affinities.items(), key=lambda x: x[1], reverse=True):
            print(f"  {pair}: {score:.2f}")
        print()

    if moves:
        print("Suggested Moves:")
        for m in moves:
            print(
                f"  {m['agent_id']}: {m['source_node']} -> {m['target_node']} "
                f"(score={m['priority_score']:.2f}, reason={m['reason']})"
            )
    else:
        print("No moves suggested — cluster is balanced.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortiva",
        description="Cortiva — The organisational fabric for autonomous agent teams",
    )
    subparsers = parser.add_subparsers(dest="command")

    # init
    init_parser = subparsers.add_parser("init", help="Initialise a new workspace")
    init_parser.add_argument("name", help="Workspace name")

    # bootstrap
    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="Bootstrap the three-agent development team"
    )
    bootstrap_parser.add_argument(
        "--dir", default=".", help="Workspace directory (default: current)"
    )

    # discover
    subparsers.add_parser("discover", help="Discover node capabilities")

    # start
    start_parser = subparsers.add_parser("start", help="Start the fabric")
    start_parser.add_argument("--portal", action="store_true", help="Also start the web portal")
    start_parser.add_argument("--portal-host", default="127.0.0.1", help="Portal bind host")
    start_parser.add_argument("--portal-port", type=int, default=8400, help="Portal bind port")

    # stop
    subparsers.add_parser("stop", help="Stop the fabric (sends signal to running instance)")

    # status
    subparsers.add_parser("status", help="Show agent status")

    # watch
    subparsers.add_parser("watch", help="Live dashboard of all agents")

    # capacity
    subparsers.add_parser("capacity", help="Show node capacity and contention metrics")

    # agent
    agent_parser = subparsers.add_parser("agent", help="Agent management")
    agent_sub = agent_parser.add_subparsers(dest="agent_command")

    create_parser = agent_sub.add_parser("create", help="Register a new agent")
    create_parser.add_argument("id", help="Agent ID")
    create_parser.add_argument(
        "-t", "--template",
        help="Create agent from a bundled template",
    )

    agent_sub.add_parser("list", help="List all agents")

    activity_parser = agent_sub.add_parser("activity", help="Show detailed agent activity")
    activity_parser.add_argument("id", help="Agent ID")

    hours_parser = agent_sub.add_parser("hours", help="Show agent working hours")
    hours_parser.add_argument("id", help="Agent ID")
    hours_parser.add_argument("--week", action="store_true", help="Show weekly summary")

    wake_parser = agent_sub.add_parser("wake", help="Wake an agent")
    wake_parser.add_argument("id", help="Agent ID")

    sleep_parser = agent_sub.add_parser("sleep", help="Put an agent to sleep")
    sleep_parser.add_argument("id", help="Agent ID")

    move_parser = agent_sub.add_parser("move", help="Move an agent to another node")
    move_parser.add_argument("id", help="Agent ID")
    move_parser.add_argument("--to", required=True, help="Target node ID")

    snap_parser = agent_sub.add_parser("snapshot", help="Create a snapshot of an agent")
    snap_parser.add_argument("id", help="Agent ID")
    snap_parser.add_argument("--name", default="", help="Snapshot name")
    snap_parser.add_argument("--description", default="", help="Description")

    snapshots_parser = agent_sub.add_parser("snapshots", help="List snapshots for an agent")
    snapshots_parser.add_argument("id", help="Agent ID")

    rollback_parser = agent_sub.add_parser("rollback", help="Rollback an agent to a snapshot")
    rollback_parser.add_argument("id", help="Agent ID")
    rollback_parser.add_argument("--snapshot", required=True, help="Snapshot ID")
    rollback_parser.add_argument("--no-journal", action="store_true", help="Skip restoring journal")

    clone_parser = agent_sub.add_parser("clone", help="Clone an agent from a snapshot")
    clone_parser.add_argument("id", help="Source agent ID")
    clone_parser.add_argument("--as", dest="new_id", required=True, help="New agent ID")
    clone_parser.add_argument("--from-snapshot", default="latest", help="Snapshot ID (default: latest)")

    promote_parser = agent_sub.add_parser("promote", help="Promote an agent to a new role")
    promote_parser.add_argument("id", help="Agent ID")
    promote_parser.add_argument("--to", required=True, help="Target role template name")
    promote_parser.add_argument("--probation", type=int, default=14, help="Probation days (default: 14)")

    probation_parser = agent_sub.add_parser("probation", help="Manage agent probation")
    probation_parser.add_argument("id", help="Agent ID")
    probation_group = probation_parser.add_mutually_exclusive_group(required=True)
    probation_group.add_argument("--confirm", action="store_true", help="Confirm promotion")
    probation_group.add_argument("--revert", action="store_true", help="Revert promotion")
    probation_group.add_argument("--extend", type=int, metavar="DAYS", help="Extend probation")

    export_parser = agent_sub.add_parser("export", help="Export an agent to a tarball")
    export_parser.add_argument("id", help="Agent ID")
    export_parser.add_argument("--output", "-o", help="Output file path (default: <id>.tar.gz)")
    export_parser.add_argument("--sanitise", action="store_true", help="Redact emails and URLs")

    import_parser = agent_sub.add_parser("import", help="Import an agent from a tarball")
    import_parser.add_argument("archive", help="Path to tarball")
    import_parser.add_argument("--as", dest="new_id", required=True, help="New agent ID")

    # template
    template_parser = subparsers.add_parser("template", help="Template management")
    template_sub = template_parser.add_subparsers(dest="template_command")
    template_sub.add_parser("list", help="List available templates")

    # skill
    skill_parser = subparsers.add_parser("skill", help="Skill management")
    skill_sub = skill_parser.add_subparsers(dest="skill_command")

    skill_list_parser = skill_sub.add_parser("list", help="List available or installed skills")
    skill_list_parser.add_argument("--agent", help="Show skills installed for an agent")
    skill_list_parser.add_argument("--category", help="Filter by category")
    skill_list_parser.add_argument("--query", help="Search query", nargs="?", default="")

    skill_search_parser = skill_sub.add_parser("search", help="Search for skills")
    skill_search_parser.add_argument("query", help="Search query")

    skill_install_parser = skill_sub.add_parser("install", help="Install a skill for an agent")
    skill_install_parser.add_argument("name", help="Skill name")
    skill_install_parser.add_argument("--agent", required=True, help="Agent ID")

    skill_uninstall_parser = skill_sub.add_parser("uninstall", help="Uninstall a skill")
    skill_uninstall_parser.add_argument("name", help="Skill name")
    skill_uninstall_parser.add_argument("--agent", required=True, help="Agent ID")

    skill_info_parser = skill_sub.add_parser("info", help="Show skill details")
    skill_info_parser.add_argument("name", help="Skill name")

    # budget
    budget_parser = subparsers.add_parser("budget", help="Show consciousness budget status")
    budget_parser.add_argument("--agent", help="Show detail for a specific agent")

    # portal
    portal_parser = subparsers.add_parser("portal", help="Start the web portal")
    portal_parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    portal_parser.add_argument("--port", type=int, default=8400, help="Bind port (default: 8400)")

    # cluster
    cluster_parser = subparsers.add_parser("cluster", help="Cluster management")
    cluster_sub = cluster_parser.add_subparsers(dest="cluster_command")
    cluster_sub.add_parser("load", help="Show cluster load and balancing suggestions")
    cluster_sub.add_parser("status", help="Show cluster status")
    cluster_sub.add_parser("nodes", help="Show cluster nodes")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "bootstrap":
        cmd_bootstrap(args)
    elif args.command == "discover":
        cmd_discover(args)
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "watch":
        cmd_watch(args)
    elif args.command == "capacity":
        cmd_capacity(args)
    elif args.command == "agent":
        if args.agent_command == "create":
            cmd_agent_create(args)
        elif args.agent_command == "list":
            cmd_agent_list(args)
        elif args.agent_command == "activity":
            cmd_agent_activity(args)
        elif args.agent_command == "hours":
            cmd_agent_hours(args)
        elif args.agent_command == "wake":
            cmd_agent_wake(args)
        elif args.agent_command == "sleep":
            cmd_agent_sleep(args)
        elif args.agent_command == "move":
            cmd_agent_move(args)
        elif args.agent_command == "snapshot":
            cmd_agent_snapshot(args)
        elif args.agent_command == "snapshots":
            cmd_agent_snapshots(args)
        elif args.agent_command == "rollback":
            cmd_agent_rollback(args)
        elif args.agent_command == "clone":
            cmd_agent_clone(args)
        elif args.agent_command == "promote":
            cmd_agent_promote(args)
        elif args.agent_command == "probation":
            cmd_agent_probation(args)
        elif args.agent_command == "export":
            cmd_agent_export(args)
        elif args.agent_command == "import":
            cmd_agent_import(args)
        else:
            parser.parse_args(["agent", "--help"])
    elif args.command == "portal":
        cmd_portal(args)
    elif args.command == "budget":
        cmd_budget(args)
    elif args.command == "cluster":
        if args.cluster_command == "load":
            cmd_cluster_load(args)
        elif args.cluster_command == "status":
            cmd_cluster_status(args)
        elif args.cluster_command == "nodes":
            cmd_cluster_nodes(args)
        else:
            parser.parse_args(["cluster", "--help"])
    elif args.command == "skill":
        if args.skill_command == "list":
            cmd_skill_list(args)
        elif args.skill_command == "search":
            cmd_skill_search(args)
        elif args.skill_command == "install":
            cmd_skill_install(args)
        elif args.skill_command == "uninstall":
            cmd_skill_uninstall(args)
        elif args.skill_command == "info":
            cmd_skill_info(args)
        else:
            parser.parse_args(["skill", "--help"])
    elif args.command == "template":
        if args.template_command == "list":
            cmd_template_list(args)
        else:
            parser.parse_args(["template", "--help"])
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
