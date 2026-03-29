"""
Cortiva Isolation Example
=========================

Demonstrates the three-tier agent isolation system.  Shows path
validation, memory access guards, environment filtering, and
container command generation — all without requiring Docker.

Run from the repository root:

    PYTHONPATH=src python3 examples/isolation_example.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from cortiva.core.isolation import (
    ContainerConfig,
    ContainerIsolation,
    IsolationConfig,
    IsolationTier,
    NoIsolation,
    OSIsolation,
    SoftIsolation,
    build_enforcer,
)


def demo_build_enforcer(agents_dir: Path) -> None:
    """Show how the factory selects the right enforcer."""
    print("=" * 60)
    print("build_enforcer() — factory selects tier from config")
    print("=" * 60)

    for tier_name in ("none", "soft", "os", "container"):
        config = IsolationConfig.from_dict({"tier": tier_name})
        enforcer = build_enforcer(agents_dir, config)
        print(f"  tier={tier_name:>10}  →  {type(enforcer).__name__}")
    print()


def demo_path_validation(agents_dir: Path) -> None:
    """Show how Soft isolation prevents directory traversal."""
    print("=" * 60)
    print("Tier 1 (Soft) — Path traversal prevention")
    print("=" * 60)

    agent_dir = agents_dir / "agent-1"
    agent_dir.mkdir(parents=True, exist_ok=True)
    enforcer = SoftIsolation(agents_dir=agents_dir)

    # Allowed: path inside agent directory
    safe_path = agent_dir / "workspace" / "file.py"
    result = enforcer.validate_path("agent-1", safe_path)
    print(f"  ALLOWED: {safe_path}")
    print(f"    resolved to: {result}")

    # Blocked: path traversal to another agent
    bad_path = agent_dir / ".." / "agent-2" / "identity" / "soul.md"
    try:
        enforcer.validate_path("agent-1", bad_path)
        print(f"  ERROR: should have been blocked")
    except PermissionError as e:
        print(f"  BLOCKED: {bad_path}")
        print(f"    reason: {e}")

    # Blocked: absolute path escape
    try:
        enforcer.validate_path("agent-1", Path("/etc/passwd"))
    except PermissionError as e:
        print(f"  BLOCKED: /etc/passwd")
        print(f"    reason: {e}")
    print()


def demo_memory_guard(agents_dir: Path) -> None:
    """Show how cross-agent memory access is blocked."""
    print("=" * 60)
    print("Tier 1 (Soft) — Cross-agent memory guard")
    print("=" * 60)

    enforcer = SoftIsolation(agents_dir=agents_dir)

    # Same agent: allowed
    allowed = enforcer.validate_memory_access("agent-1", "agent-1")
    print(f"  agent-1 accessing agent-1 memories: {'ALLOWED' if allowed else 'BLOCKED'}")

    # Cross-agent: blocked
    blocked = enforcer.validate_memory_access("agent-1", "agent-2")
    print(f"  agent-1 accessing agent-2 memories: {'ALLOWED' if blocked else 'BLOCKED'}")

    # No isolation: always allowed
    no_iso = NoIsolation(agents_dir=agents_dir)
    allowed = no_iso.validate_memory_access("agent-1", "agent-2")
    print(f"  (NoIsolation) agent-1 accessing agent-2: {'ALLOWED' if allowed else 'BLOCKED'}")
    print()


def demo_env_filtering(agents_dir: Path) -> None:
    """Show how OS isolation filters environment variables."""
    print("=" * 60)
    print("Tier 2 (OS) — Environment variable filtering")
    print("=" * 60)

    agent_dir = agents_dir / "agent-1"
    agent_dir.mkdir(parents=True, exist_ok=True)

    config = IsolationConfig(
        tier=IsolationTier.OS,
        allowed_env=["PATH", "HOME"],
    )
    enforcer = OSIsolation(agents_dir=agents_dir, config=config)
    envelope = enforcer.prepare_terminal_env("agent-1", ["echo", "hello"], agent_dir)

    print(f"  Allowed env vars: PATH, HOME")
    print(f"  Resulting env keys: {sorted(envelope.env.keys())}")
    print(f"  CORTIVA_AGENT_ID: {envelope.env.get('CORTIVA_AGENT_ID', 'not set')}")
    print(f"  TMPDIR: {envelope.env.get('TMPDIR', 'not set')}")
    print(f"  (Secrets like AWS_SECRET_KEY would be stripped)")

    enforcer.cleanup("agent-1")
    print()


def demo_container_command(agents_dir: Path) -> None:
    """Show the Docker command that container isolation generates."""
    print("=" * 60)
    print("Tier 3 (Container) — Docker command generation")
    print("=" * 60)

    agent_dir = agents_dir / "agent-1"
    agent_dir.mkdir(parents=True, exist_ok=True)

    config = IsolationConfig(
        tier=IsolationTier.CONTAINER,
        allowed_env=["PATH"],
        container=ContainerConfig(
            runtime="docker",
            cpu_limit="0.5",
            memory_limit="256m",
            shm_size="128m",
            network="bridge",
            image="python:3.13-slim",
            browser_endpoint="ws://browserless:3000",
        ),
    )

    # Use ContainerIsolation directly but skip runtime check
    # (we just want to see the command it would generate)
    enforcer = ContainerIsolation(agents_dir=agents_dir, config=config)

    # Monkey-patch to pretend docker is available
    enforcer._runtime_available = lambda: True  # type: ignore[assignment]

    envelope = enforcer.prepare_terminal_env(
        "agent-1", ["claude", "-p", "fix the bug"], agent_dir,
    )

    print(f"  Container ID: {envelope.container_id}")
    print(f"  Command ({len(envelope.cmd)} args):")
    # Print command in a readable way
    cmd_str = " \\\n    ".join(envelope.cmd)
    print(f"    {cmd_str}")
    print()

    # Show browser endpoint was injected
    has_browser = any(
        "BROWSER_WS_ENDPOINT" in arg for arg in envelope.cmd
    )
    print(f"  Browser endpoint injected: {has_browser}")
    print()


def main() -> None:
    """Run all isolation demos."""
    print()
    print("Cortiva Agent Isolation — Three-Tier Demo")
    print()

    with tempfile.TemporaryDirectory(prefix="cortiva-iso-demo-") as tmpdir:
        agents_dir = Path(tmpdir) / "agents"
        agents_dir.mkdir()

        demo_build_enforcer(agents_dir)
        demo_path_validation(agents_dir)
        demo_memory_guard(agents_dir)
        demo_env_filtering(agents_dir)
        demo_container_command(agents_dir)

    print("Done. All demos completed successfully.")


if __name__ == "__main__":
    main()
