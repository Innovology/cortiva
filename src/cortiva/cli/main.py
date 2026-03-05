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


def cmd_status(args: argparse.Namespace) -> None:
    """Show fabric status."""
    config_path = Path("cortiva.yaml")
    if not config_path.exists():
        print("Not a Cortiva workspace. Run 'cortiva init <name>' first.")
        sys.exit(1)

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

    print(f"Cortiva workspace — {len(agents)} agent(s)\n")
    for agent_id in agents:
        agent_dir = agents_dir / agent_id
        # Check new subdirectory layout, fall back to flat layout
        identity = agent_dir / "identity" / "identity.md"
        if not identity.exists():
            identity = agent_dir / "identity.md"
        has_identity = "✓" if identity.exists() else "✗"
        plan = agent_dir / "today" / "plan.md"
        if not plan.exists():
            plan = agent_dir / "plan.md"
        has_plan = "✓" if plan.exists() else "✗"
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


def cmd_start(args: argparse.Namespace) -> None:
    """Start the Cortiva fabric."""
    config_path = Path("cortiva.yaml")
    if not config_path.exists():
        print("Not a Cortiva workspace. Run 'cortiva init <name>' first.")
        sys.exit(1)

    from cortiva.core.config import load_and_build

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    try:
        fabric = load_and_build(config_path)
    except Exception as exc:
        print(f"Failed to load config: {exc}")
        sys.exit(1)

    loop = asyncio.new_event_loop()

    def _shutdown(signum: int, frame: object) -> None:
        print("\nShutting down...")
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    async def _run() -> None:
        await fabric.start()
        print(f"Cortiva fabric running ({len(fabric.agents)} agents). Press Ctrl+C to stop.")
        try:
            while fabric._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await fabric.stop()
            print("Fabric stopped.")

    try:
        loop.run_until_complete(_run())
    except RuntimeError:
        # Loop was stopped by signal handler
        loop.run_until_complete(fabric.stop())
        print("Fabric stopped.")
    finally:
        loop.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortiva",
        description="Cortiva — The organisational fabric for autonomous agent teams",
    )
    subparsers = parser.add_subparsers(dest="command")

    # init
    init_parser = subparsers.add_parser("init", help="Initialise a new workspace")
    init_parser.add_argument("name", help="Workspace name")

    # start
    subparsers.add_parser("start", help="Start the fabric")

    # stop
    subparsers.add_parser("stop", help="Stop the fabric (sends signal to running instance)")

    # status
    subparsers.add_parser("status", help="Show agent status")

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

    wake_parser = agent_sub.add_parser("wake", help="Wake an agent")
    wake_parser.add_argument("id", help="Agent ID")

    sleep_parser = agent_sub.add_parser("sleep", help="Put an agent to sleep")
    sleep_parser.add_argument("id", help="Agent ID")

    # template
    template_parser = subparsers.add_parser("template", help="Template management")
    template_sub = template_parser.add_subparsers(dest="template_command")
    template_sub.add_parser("list", help="List available templates")

    # budget
    budget_parser = subparsers.add_parser("budget", help="Show consciousness budget status")
    budget_parser.add_argument("--agent", help="Show detail for a specific agent")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        print("Send SIGINT or SIGTERM to the running 'cortiva start' process.")
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "agent":
        if args.agent_command == "create":
            cmd_agent_create(args)
        elif args.agent_command == "list":
            cmd_agent_list(args)
        elif args.agent_command in ("wake", "sleep"):
            print(
                f"Agent {args.agent_command} requires a running fabric."
                " Use 'cortiva start' first."
            )
        else:
            parser.parse_args(["agent", "--help"])
    elif args.command == "budget":
        cmd_budget(args)
    elif args.command == "template":
        if args.template_command == "list":
            cmd_template_list(args)
        else:
            parser.parse_args(["template", "--help"])
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
