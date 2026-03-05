"""
Node Capability Auto-Discovery.

On startup, Cortiva scans the local environment to build a manifest of
available resources: terminal agents (Claude Code, Codex, Aider), local
models (via Ollama), system resources, and any custom endpoints from config.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TerminalAgentInfo:
    """Discovered terminal agent CLI tool."""
    name: str                    # "claude-code", "codex", "aider"
    binary: str                  # path to binary
    version: str = ""            # version string if available
    available: bool = True       # True if binary found
    auth_ok: bool = False        # True if authentication check passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "binary": self.binary,
            "version": self.version,
            "available": self.available,
            "auth_ok": self.auth_ok,
        }


@dataclass
class LocalModelInfo:
    """A local model discovered via Ollama or similar."""
    name: str                    # e.g. "qwen3.5:35b-a3b"
    size_bytes: int = 0          # model file size
    family: str = ""             # model family
    parameter_size: str = ""     # e.g. "35B"
    quantization: str = ""       # e.g. "Q4_K_M"
    provider: str = "ollama"     # "ollama", "vllm", "llamacpp"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "family": self.family,
            "parameter_size": self.parameter_size,
            "quantization": self.quantization,
            "provider": self.provider,
        }


@dataclass
class EndpointInfo:
    """A custom API endpoint (vLLM, llama.cpp, remote service)."""
    name: str
    url: str
    provider: str = "custom"     # "vllm", "llamacpp", "custom"
    models: list[str] = field(default_factory=list)
    healthy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "provider": self.provider,
            "models": self.models,
            "healthy": self.healthy,
        }


@dataclass
class ResourceSnapshot:
    """System resource information."""
    cpu_cores: int = 0
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    disk_total_gb: float = 0.0
    disk_free_gb: float = 0.0
    platform: str = ""
    python_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_cores": self.cpu_cores,
            "ram_total_gb": round(self.ram_total_gb, 1),
            "ram_available_gb": round(self.ram_available_gb, 1),
            "disk_total_gb": round(self.disk_total_gb, 1),
            "disk_free_gb": round(self.disk_free_gb, 1),
            "platform": self.platform,
            "python_version": self.python_version,
        }


@dataclass
class NodeCapabilities:
    """Complete capability manifest for this node."""
    node_id: str
    terminal_agents: list[TerminalAgentInfo] = field(default_factory=list)
    local_models: list[LocalModelInfo] = field(default_factory=list)
    custom_endpoints: list[EndpointInfo] = field(default_factory=list)
    resources: ResourceSnapshot = field(default_factory=ResourceSnapshot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "terminal_agents": [t.to_dict() for t in self.terminal_agents],
            "local_models": [m.to_dict() for m in self.local_models],
            "custom_endpoints": [e.to_dict() for e in self.custom_endpoints],
            "resources": self.resources.to_dict(),
        }

    @property
    def summary(self) -> str:
        """One-line summary of discovered capabilities."""
        agents = [t.name for t in self.terminal_agents if t.available]
        models = len(self.local_models)
        endpoints = len([e for e in self.custom_endpoints if e.healthy])
        parts = []
        if agents:
            parts.append(f"terminal: {', '.join(agents)}")
        if models:
            parts.append(f"models: {models}")
        if endpoints:
            parts.append(f"endpoints: {endpoints}")
        parts.append(
            f"resources: {self.resources.cpu_cores} cores, "
            f"{self.resources.ram_available_gb:.0f}GB RAM free"
        )
        return " | ".join(parts)

    @classmethod
    async def discover(
        cls,
        node_id: str,
        *,
        custom_endpoints: list[dict[str, Any]] | None = None,
    ) -> NodeCapabilities:
        """Auto-discover everything available on this node."""
        # Run independent discovery tasks concurrently
        terminal_task = asyncio.create_task(_discover_terminal_agents())
        models_task = asyncio.create_task(_discover_ollama_models())
        resources = _discover_resources()

        terminal_agents = await terminal_task
        local_models = await models_task

        # Check custom endpoints
        endpoints: list[EndpointInfo] = []
        if custom_endpoints:
            checks = [
                _check_endpoint(ep) for ep in custom_endpoints
            ]
            endpoints = await asyncio.gather(*checks)

        return cls(
            node_id=node_id,
            terminal_agents=terminal_agents,
            local_models=local_models,
            custom_endpoints=endpoints,
            resources=resources,
        )


# ---------------------------------------------------------------------------
# Terminal agent discovery
# ---------------------------------------------------------------------------

_TERMINAL_AGENTS = [
    ("claude-code", "claude", ["claude", "--version"]),
    ("codex", "codex", ["codex", "--version"]),
    ("aider", "aider", ["aider", "--version"]),
]


async def _run_cmd(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a command and return (returncode, stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        return -1, ""
    except (TimeoutError, asyncio.TimeoutError):
        return -2, ""
    except Exception:
        return -3, ""


async def _discover_terminal_agents() -> list[TerminalAgentInfo]:
    """Check which terminal agent CLIs are available."""
    results: list[TerminalAgentInfo] = []

    for name, binary_name, version_cmd in _TERMINAL_AGENTS:
        binary_path = shutil.which(binary_name)
        if not binary_path:
            results.append(TerminalAgentInfo(
                name=name, binary="", available=False,
            ))
            continue

        code, output = await _run_cmd(version_cmd)
        version = ""
        if code == 0 and output:
            # Take first line as version
            version = output.splitlines()[0][:100]

        # Check auth status for claude (ANTHROPIC_API_KEY)
        auth_ok = False
        if name == "claude-code":
            auth_ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
        elif name == "codex":
            auth_ok = bool(os.environ.get("OPENAI_API_KEY"))
        elif name == "aider":
            auth_ok = bool(
                os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
            )

        results.append(TerminalAgentInfo(
            name=name,
            binary=binary_path,
            version=version,
            available=True,
            auth_ok=auth_ok,
        ))

    return results


# ---------------------------------------------------------------------------
# Ollama model discovery
# ---------------------------------------------------------------------------

_OLLAMA_API_URL = "http://localhost:11434"


async def _discover_ollama_models(
    base_url: str = _OLLAMA_API_URL,
) -> list[LocalModelInfo]:
    """Query Ollama API for available local models."""
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, _fetch_ollama_tags, base_url)
    except Exception:
        return []

    models: list[LocalModelInfo] = []
    for entry in data.get("models", []):
        details = entry.get("details", {})
        models.append(LocalModelInfo(
            name=entry.get("name", ""),
            size_bytes=entry.get("size", 0),
            family=details.get("family", ""),
            parameter_size=details.get("parameter_size", ""),
            quantization=details.get("quantization_level", ""),
            provider="ollama",
        ))

    return models


def _fetch_ollama_tags(base_url: str) -> dict[str, Any]:
    """Synchronous fetch of Ollama /api/tags (runs in executor)."""
    req = Request(f"{base_url}/api/tags", method="GET")
    req.add_header("Accept", "application/json")
    with urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Custom endpoint checks
# ---------------------------------------------------------------------------


async def _check_endpoint(config: dict[str, Any]) -> EndpointInfo:
    """Check if a custom endpoint is reachable."""
    name = config.get("name", "custom")
    url = config.get("url", "")
    provider = config.get("provider", "custom")
    models = config.get("models", [])

    info = EndpointInfo(
        name=name, url=url, provider=provider, models=models,
    )

    if not url:
        return info

    loop = asyncio.get_event_loop()
    try:
        healthy = await loop.run_in_executor(None, _ping_endpoint, url)
        info.healthy = healthy
    except Exception:
        pass

    return info


def _ping_endpoint(url: str) -> bool:
    """Synchronous health check for an endpoint."""
    # Try common health check paths
    for path in ["/health", "/v1/models", "/api/tags", ""]:
        try:
            check_url = url.rstrip("/") + path
            req = Request(check_url, method="GET")
            with urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# System resource discovery
# ---------------------------------------------------------------------------


def _discover_resources() -> ResourceSnapshot:
    """Gather system resource information."""
    import sys

    snap = ResourceSnapshot(
        platform=platform.platform(),
        python_version=sys.version.split()[0],
    )

    # CPU cores
    snap.cpu_cores = os.cpu_count() or 0

    # RAM — try psutil, fall back to platform-specific
    try:
        import psutil
        mem = psutil.virtual_memory()
        snap.ram_total_gb = mem.total / (1024 ** 3)
        snap.ram_available_gb = mem.available / (1024 ** 3)
    except ImportError:
        snap.ram_total_gb = _get_ram_total_gb()
        snap.ram_available_gb = snap.ram_total_gb  # can't determine free without psutil

    # Disk
    try:
        usage = shutil.disk_usage(".")
        snap.disk_total_gb = usage.total / (1024 ** 3)
        snap.disk_free_gb = usage.free / (1024 ** 3)
    except Exception:
        pass

    return snap


def _get_ram_total_gb() -> float:
    """Get total RAM in GB without psutil."""
    system = platform.system()
    if system == "Darwin":
        try:
            import subprocess
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return int(result.stdout.strip()) / (1024 ** 3)
        except Exception:
            pass
    elif system == "Linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb / (1024 ** 2)
        except Exception:
            pass
    return 0.0
