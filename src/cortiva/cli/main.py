"""
Cortiva CLI — manage your agent organisation from the terminal.

Usage:
    cortiva init <name>              Initialise a new workspace
    cortiva start                    Start the fabric
    cortiva stop                     Stop the fabric
    cortiva status                   Show agent status
    cortiva agent create <id>        Register a new agent
    cortiva agent wake <id>          Wake an agent
    cortiva agent sleep <id>         Put an agent to sleep
    cortiva agent list               List all agents
    cortiva config set <key> <val>   Set configuration
"""

from __future__ import annotations

import argparse
import json
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

    agents = sorted(p.name for p in agents_dir.iterdir() if p.is_dir() and not p.name.startswith("."))

    if not agents:
        print("No agents registered.")
        return

    print(f"Cortiva workspace — {len(agents)} agent(s)\n")
    for agent_id in agents:
        agent_dir = agents_dir / agent_id
        identity = agent_dir / "identity.md"
        has_identity = "✓" if identity.exists() else "✗"
        plan = agent_dir / "plan.md"
        has_plan = "✓" if plan.exists() else "✗"
        print(f"  {agent_id:<20} identity:{has_identity}  plan:{has_plan}")


def cmd_agent_create(args: argparse.Namespace) -> None:
    """Register a new agent."""
    config_path = Path("cortiva.yaml")
    if not config_path.exists():
        print("Not a Cortiva workspace. Run 'cortiva init <name>' first.")
        sys.exit(1)

    agent_dir = Path("agents") / args.id
    if agent_dir.exists():
        print(f"Agent '{args.id}' already exists.")
        sys.exit(1)

    agent_dir.mkdir(parents=True)
    (agent_dir / "journal").mkdir()

    files = {
        "identity.md": f"# {args.id}\n\nNewly created agent. No experiences yet.\n",
        "soul.md": f"# {args.id} — Persona\n\nDefault persona. Configure disposition parameters.\n",
        "skills.md": f"# {args.id} — Skills\n\nNo skills defined yet.\n",
        "responsibilities.md": f"# {args.id} — Responsibilities\n\n## Primary\n\n## Secondary\n\n## Escalation\n",
        "procedures.md": f"# {args.id} — Procedures\n\nNo procedures promoted yet.\n",
        "plan.md": f"# {args.id} — Plan\n\nNo plan yet. Awaiting first wake cycle.\n",
    }

    for filename, content in files.items():
        (agent_dir / filename).write_text(content)

    print(f"Created agent: {args.id}")
    print(f"  Directory: {agent_dir}/")
    print()
    print(f"Edit the identity files to configure this agent:")
    print(f"  {agent_dir}/soul.md              — personality and disposition")
    print(f"  {agent_dir}/skills.md            — domain knowledge")
    print(f"  {agent_dir}/responsibilities.md  — authority boundaries")


def cmd_agent_list(args: argparse.Namespace) -> None:
    """List all agents."""
    cmd_status(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortiva",
        description="Cortiva — The organisational fabric for autonomous agent teams",
    )
    subparsers = parser.add_subparsers(dest="command")

    # init
    init_parser = subparsers.add_parser("init", help="Initialise a new workspace")
    init_parser.add_argument("name", help="Workspace name")

    # status
    subparsers.add_parser("status", help="Show agent status")

    # agent
    agent_parser = subparsers.add_parser("agent", help="Agent management")
    agent_sub = agent_parser.add_subparsers(dest="agent_command")

    create_parser = agent_sub.add_parser("create", help="Register a new agent")
    create_parser.add_argument("id", help="Agent ID")

    agent_sub.add_parser("list", help="List all agents")

    wake_parser = agent_sub.add_parser("wake", help="Wake an agent")
    wake_parser.add_argument("id", help="Agent ID")

    sleep_parser = agent_sub.add_parser("sleep", help="Put an agent to sleep")
    sleep_parser.add_argument("id", help="Agent ID")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "agent":
        if args.agent_command == "create":
            cmd_agent_create(args)
        elif args.agent_command == "list":
            cmd_agent_list(args)
        elif args.agent_command in ("wake", "sleep"):
            print(f"Agent {args.agent_command} requires a running fabric. Use 'cortiva start' first.")
        else:
            parser.parse_args(["agent", "--help"])
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
