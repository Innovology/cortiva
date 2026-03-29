"""
Three-tier agent isolation system.

Cortiva agents can be isolated at different levels depending on
deployment requirements.  The ``isolation`` section in ``cortiva.yaml``
selects the tier:

- **none** — no enforcement (backward-compatible default).
- **soft** — path-traversal prevention, memory-access guards,
  terminal cwd enforcement.
- **os** — soft protections *plus* environment-variable filtering,
  per-agent TMPDIR, per-agent IPC socket paths.
- **container** — OS protections *plus* each agent runs in its own
  Docker/Podman container with CPU/memory/network limits.

Each tier inherits all protections from lower tiers via class
inheritance.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortiva.isolation")

# ---------------------------------------------------------------------------
# Tier enum
# ---------------------------------------------------------------------------


class IsolationTier(Enum):
    """Isolation level for agent workspaces."""

    NONE = "none"
    SOFT = "soft"
    OS = "os"
    CONTAINER = "container"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ContainerConfig:
    """Container-specific isolation settings (Tier 3)."""

    runtime: str = "docker"
    """Container runtime: ``docker`` or ``podman``."""

    cpu_limit: str = "1.0"
    """CPU limit per agent container."""

    memory_limit: str = "512m"
    """Memory limit per agent container."""

    shm_size: str = "256m"
    """Shared memory size.  Increase for agents that drive a browser."""

    network: str = "bridge"
    """Network mode: ``bridge`` (default — agents can reach external APIs),
    ``none`` (air-gapped), or ``host``."""

    image: str = "python:3.13-slim"
    """Base image for agent containers."""

    browser_endpoint: str = ""
    """WebSocket URL of a shared browser service (e.g. Browserless,
    Chrome DevTools Protocol).  When set, ``BROWSER_WS_ENDPOINT`` is
    injected into the container environment so agents can drive a
    browser without Chrome installed locally."""


@dataclass
class IsolationConfig:
    """Parsed ``isolation:`` section from cortiva.yaml."""

    tier: IsolationTier = IsolationTier.NONE

    allowed_env: list[str] = field(default_factory=lambda: [
        "PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
    ])
    """Environment variables allowed through to agent subprocesses (Tier 2+)."""

    container: ContainerConfig = field(default_factory=ContainerConfig)
    """Container settings (Tier 3 only)."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IsolationConfig:
        """Build from a parsed YAML dict."""
        tier = IsolationTier(data.get("tier", "none"))
        default_env = cls.__dataclass_fields__["allowed_env"].default_factory()  # type: ignore[misc]
        allowed_env = data.get("allowed_env", default_env)

        container_data = data.get("container", {})
        container = ContainerConfig(**{
            k: v for k, v in container_data.items()
            if k in ContainerConfig.__dataclass_fields__
        }) if container_data else ContainerConfig()

        return cls(tier=tier, allowed_env=allowed_env, container=container)


# ---------------------------------------------------------------------------
# Subprocess envelope — the output of prepare_terminal_env()
# ---------------------------------------------------------------------------


@dataclass
class SubprocessEnvelope:
    """Encapsulates the environment for launching an agent subprocess.

    Produced by :meth:`IsolationEnforcer.prepare_terminal_env` and consumed
    by terminal adapters.
    """

    cmd: list[str]
    """The command to execute (may be wrapped with ``docker run …``)."""

    cwd: Path
    """Working directory for the subprocess."""

    env: dict[str, str] | None = None
    """Environment variables.  ``None`` means inherit parent env."""

    container_id: str | None = None
    """Container ID when running under Tier 3."""

    tmpdir: Path | None = None
    """Agent-specific temporary directory (Tier 2+)."""


# ---------------------------------------------------------------------------
# Enforcer implementations
# ---------------------------------------------------------------------------


class NoIsolation:
    """Tier 0 — no isolation enforcement (backward-compatible default)."""

    tier: IsolationTier = IsolationTier.NONE

    def __init__(self, agents_dir: Path, config: IsolationConfig | None = None) -> None:
        self.agents_dir = agents_dir.resolve()
        self.config = config or IsolationConfig()

    def validate_path(self, agent_id: str, path: Path) -> Path:
        """Validate and return the resolved path.

        In ``NoIsolation`` this is a pass-through.
        """
        return path.resolve()

    def validate_memory_access(self, caller_id: str, target_id: str) -> bool:
        """Check whether *caller_id* may access *target_id*'s memories.

        In ``NoIsolation`` all access is permitted.
        """
        return True

    def prepare_terminal_env(
        self,
        agent_id: str,
        cmd: list[str],
        cwd: Path,
    ) -> SubprocessEnvelope:
        """Prepare the subprocess envelope for a terminal adapter call.

        In ``NoIsolation`` the command and cwd are returned unchanged.
        """
        return SubprocessEnvelope(cmd=cmd, cwd=cwd, env=None)

    def cleanup(self, agent_id: str) -> None:
        """Clean up any resources created for an agent.

        In ``NoIsolation`` this is a no-op.
        """


class SoftIsolation(NoIsolation):
    """Tier 1 — process-level enforcement.

    - Path traversal prevention via ``Path.resolve()``.
    - Cross-agent memory access blocking.
    - Terminal cwd locked to agent workspace.
    """

    tier: IsolationTier = IsolationTier.SOFT

    def validate_path(self, agent_id: str, path: Path) -> Path:
        """Ensure *path* resolves inside the agent's directory.

        Raises :class:`PermissionError` on directory traversal.
        """
        agent_dir = (self.agents_dir / agent_id).resolve()
        resolved = path.resolve()
        if not (resolved == agent_dir or str(resolved).startswith(str(agent_dir) + os.sep)):
            raise PermissionError(
                f"Agent {agent_id!r} attempted to access path outside its "
                f"workspace: {resolved}"
            )
        return resolved

    def validate_memory_access(self, caller_id: str, target_id: str) -> bool:
        """Block cross-agent memory access."""
        if caller_id != target_id:
            logger.warning(
                "Blocked cross-agent memory access: %s tried to access %s",
                caller_id, target_id,
            )
            return False
        return True

    def prepare_terminal_env(
        self,
        agent_id: str,
        cmd: list[str],
        cwd: Path,
    ) -> SubprocessEnvelope:
        """Lock terminal cwd to the agent's workspace directory."""
        agent_dir = (self.agents_dir / agent_id).resolve()
        resolved_cwd = cwd.resolve()

        if not (resolved_cwd == agent_dir or str(resolved_cwd).startswith(str(agent_dir) + os.sep)):
            logger.warning(
                "Agent %s terminal cwd %s outside workspace, resetting to %s",
                agent_id, resolved_cwd, agent_dir / "workspace",
            )
            resolved_cwd = agent_dir / "workspace"
            resolved_cwd.mkdir(parents=True, exist_ok=True)

        return SubprocessEnvelope(cmd=cmd, cwd=resolved_cwd, env=None)


class OSIsolation(SoftIsolation):
    """Tier 2 — OS-level isolation.

    Inherits all Tier 1 protections plus:

    - Environment variable filtering via allowlist.
    - Per-agent TMPDIR inside workspace.
    - Per-agent IPC socket paths.
    """

    tier: IsolationTier = IsolationTier.OS

    def __init__(self, agents_dir: Path, config: IsolationConfig | None = None) -> None:
        super().__init__(agents_dir, config)
        self._tmpdirs: dict[str, Path] = {}

    def _ensure_tmpdir(self, agent_id: str) -> Path:
        """Create and return a per-agent temporary directory."""
        if agent_id not in self._tmpdirs:
            agent_dir = self.agents_dir / agent_id
            tmpdir = agent_dir / ".tmp"
            tmpdir.mkdir(parents=True, exist_ok=True)
            self._tmpdirs[agent_id] = tmpdir
        return self._tmpdirs[agent_id]

    def _filter_env(self, agent_id: str) -> dict[str, str]:
        """Build a filtered environment for the agent subprocess."""
        allowed = set(self.config.allowed_env)
        filtered: dict[str, str] = {}
        for key, value in os.environ.items():
            if key in allowed:
                filtered[key] = value

        # Override TMPDIR to agent-specific dir
        tmpdir = self._ensure_tmpdir(agent_id)
        filtered["TMPDIR"] = str(tmpdir)
        filtered["TEMP"] = str(tmpdir)
        filtered["TMP"] = str(tmpdir)

        # Tag the agent ID in the env for auditing
        filtered["CORTIVA_AGENT_ID"] = agent_id

        return filtered

    def agent_socket_path(self, agent_id: str) -> Path:
        """Return the per-agent IPC socket path."""
        return self.agents_dir / agent_id / ".cortiva" / "agent.sock"

    def prepare_terminal_env(
        self,
        agent_id: str,
        cmd: list[str],
        cwd: Path,
    ) -> SubprocessEnvelope:
        """Lock cwd and filter environment variables."""
        envelope = super().prepare_terminal_env(agent_id, cmd, cwd)
        envelope.env = self._filter_env(agent_id)
        envelope.tmpdir = self._ensure_tmpdir(agent_id)
        return envelope

    def cleanup(self, agent_id: str) -> None:
        """Remove agent tmpdir."""
        tmpdir = self._tmpdirs.pop(agent_id, None)
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


class ContainerIsolation(OSIsolation):
    """Tier 3 — container isolation.

    Inherits all Tier 2 protections plus:

    - Each agent runs in its own Docker/Podman container.
    - CPU, memory, and network limits per container.
    - Volume mounts restricted to agent directory.
    """

    tier: IsolationTier = IsolationTier.CONTAINER

    def __init__(self, agents_dir: Path, config: IsolationConfig | None = None) -> None:
        super().__init__(agents_dir, config)
        self._containers: dict[str, str] = {}

    @property
    def _runtime(self) -> str:
        return self.config.container.runtime

    def _runtime_available(self) -> bool:
        """Check if the container runtime is on PATH."""
        return shutil.which(self._runtime) is not None

    def _container_name(self, agent_id: str) -> str:
        """Generate a deterministic container name."""
        return f"cortiva-agent-{agent_id}"

    def prepare_terminal_env(
        self,
        agent_id: str,
        cmd: list[str],
        cwd: Path,
    ) -> SubprocessEnvelope:
        """Wrap the command in a container invocation."""
        if not self._runtime_available():
            logger.warning(
                "%s not found on PATH, falling back to OS isolation for %s",
                self._runtime, agent_id,
            )
            return super().prepare_terminal_env(agent_id, cmd, cwd)

        cc = self.config.container
        agent_dir = (self.agents_dir / agent_id).resolve()
        container_name = self._container_name(agent_id)

        # Build filtered env for the container
        env_vars = self._filter_env(agent_id)

        container_cmd: list[str] = [
            self._runtime, "run",
            "--rm",
            "--name", container_name,
            # Resource limits
            "--cpus", cc.cpu_limit,
            "--memory", cc.memory_limit,
            f"--shm-size={cc.shm_size}",
            # Network isolation
            f"--network={cc.network}",
            # Mount agent directory only
            "-v", f"{agent_dir}:/agent:rw",
            # Working directory inside container
            "-w", "/agent/workspace",
        ]

        # Pass through allowed env vars
        for key, value in env_vars.items():
            container_cmd.extend(["-e", f"{key}={value}"])

        # Inject browser sidecar endpoint if configured
        if cc.browser_endpoint:
            container_cmd.extend([
                "-e", f"BROWSER_WS_ENDPOINT={cc.browser_endpoint}",
            ])

        # Run as non-root (UID 1000)
        container_cmd.extend(["--user", "1000:1000"])

        # Image
        container_cmd.append(cc.image)

        # The original command
        container_cmd.extend(cmd)

        return SubprocessEnvelope(
            cmd=container_cmd,
            cwd=agent_dir,  # Host-side cwd (for the docker command itself)
            env=env_vars,
            container_id=container_name,
        )

    def cleanup(self, agent_id: str) -> None:
        """Stop and remove the agent container, then clean up tmpdir."""
        container_name = self._container_name(agent_id)
        if self._runtime_available():
            # Force-remove the container (ignore errors if already stopped)
            import subprocess

            try:
                subprocess.run(
                    [self._runtime, "rm", "-f", container_name],
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                logger.warning("Failed to remove container %s", container_name)

        super().cleanup(agent_id)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_TIER_CLASSES: dict[IsolationTier, type[NoIsolation]] = {
    IsolationTier.NONE: NoIsolation,
    IsolationTier.SOFT: SoftIsolation,
    IsolationTier.OS: OSIsolation,
    IsolationTier.CONTAINER: ContainerIsolation,
}


def build_enforcer(
    agents_dir: Path,
    config: IsolationConfig | None = None,
) -> NoIsolation:
    """Construct the appropriate isolation enforcer from config.

    Parameters
    ----------
    agents_dir:
        Root directory containing agent subdirectories.
    config:
        Parsed isolation config.  If ``None``, returns ``NoIsolation``.
    """
    if config is None:
        config = IsolationConfig()
    cls = _TIER_CLASSES.get(config.tier, NoIsolation)
    enforcer = cls(agents_dir=agents_dir, config=config)
    if config.tier != IsolationTier.NONE:
        logger.info("Agent isolation: tier=%s", config.tier.value)
    return enforcer
