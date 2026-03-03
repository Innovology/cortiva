"""
Cortiva configuration loader.

Reads ``cortiva.yaml``, validates the structure, and constructs a
:class:`~cortiva.core.fabric.Fabric` with the appropriate adapters.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from cortiva.core.fabric import Fabric

# ---------------------------------------------------------------------------
# Adapter registry — maps config names to (module, class) pairs.
# Imports are lazy so optional deps don't explode at import time.
# ---------------------------------------------------------------------------

_MEMORY_ADAPTERS: dict[str, tuple[str, str]] = {
    "inmemory": ("cortiva.adapters.memory.inmemory", "InMemoryAdapter"),
    "engram": ("cortiva.adapters.memory.engram", "EngramMemoryAdapter"),
}

_CONSCIOUSNESS_ADAPTERS: dict[str, tuple[str, str]] = {
    "anthropic": ("cortiva.adapters.consciousness.anthropic", "AnthropicConsciousnessAdapter"),
}

_CHANNEL_ADAPTERS: dict[str, tuple[str, str]] = {
    "slack": ("cortiva.adapters.channel.slack", "SlackChannelAdapter"),
}


def _import_adapter(registry: dict[str, tuple[str, str]], name: str, kind: str) -> type:
    """Look up *name* in *registry* and return the class (lazy import)."""
    if name not in registry:
        available = ", ".join(sorted(registry))
        raise ValueError(f"Unknown {kind} adapter: {name!r}. Available: {available}")
    module_path, class_name = registry[name]
    import importlib

    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(path: str | Path = "cortiva.yaml") -> dict[str, Any]:
    """Read and validate a ``cortiva.yaml`` file.

    Returns the parsed config dict.  Raises ``FileNotFoundError`` if the
    file is missing and ``ValueError`` if required keys are absent.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    with open(path, encoding="utf-8") as f:
        config: dict[str, Any] = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError(f"Invalid config: expected a YAML mapping, got {type(config).__name__}")

    # Ensure minimum required sections exist with defaults
    config.setdefault("fabric", {})
    config["fabric"].setdefault("name", "cortiva")
    config["fabric"].setdefault("heartbeat_interval", 30)

    config.setdefault("memory", {"adapter": "inmemory", "config": {}})
    config.setdefault("consciousness", {"provider": "anthropic"})
    config.setdefault("agents", {"directory": "./agents"})

    return config


def build_fabric(config: dict[str, Any]) -> Fabric:
    """Construct a :class:`Fabric` from a parsed config dict.

    Instantiates the adapter classes named in the config, passing through
    any ``config`` sub-keys as constructor kwargs.  Environment variables
    are used as fallbacks for secrets (e.g. ``ANTHROPIC_API_KEY``).
    """
    # --- Memory adapter ---
    mem_section = config.get("memory", {})
    mem_name = mem_section.get("adapter", "inmemory")
    mem_cls = _import_adapter(_MEMORY_ADAPTERS, mem_name, "memory")
    mem_kwargs: dict[str, Any] = dict(mem_section.get("config", {}))
    memory = mem_cls(**mem_kwargs)

    # --- Consciousness adapter ---
    con_section = config.get("consciousness", {})
    con_name = con_section.get("provider", "anthropic")
    con_cls = _import_adapter(_CONSCIOUSNESS_ADAPTERS, con_name, "consciousness")
    con_kwargs: dict[str, Any] = {}
    if "model" in con_section:
        con_kwargs["model"] = con_section["model"]
    api_key = con_section.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        con_kwargs["api_key"] = api_key
    consciousness = con_cls(**con_kwargs)

    # --- Channel adapter (optional) ---
    channel = None
    chan_section = config.get("channel")
    if chan_section:
        chan_name = chan_section.get("adapter")
        if chan_name:
            chan_cls = _import_adapter(_CHANNEL_ADAPTERS, chan_name, "channel")
            chan_kwargs: dict[str, Any] = dict(chan_section.get("config", {}))
            # Slack token from config or env
            if chan_name == "slack":
                token = chan_kwargs.pop("token", None) or os.environ.get("SLACK_BOT_TOKEN")
                if token:
                    chan_kwargs["token"] = token
            channel = chan_cls(**chan_kwargs)

    # --- Fabric ---
    agents_dir = Path(config.get("agents", {}).get("directory", "./agents"))
    heartbeat = config.get("fabric", {}).get("heartbeat_interval", 30)
    budget = config.get("consciousness", {}).get("budget", {}).get("daily_limit", 1000)

    return Fabric(
        agents_dir=agents_dir,
        memory=memory,
        consciousness=consciousness,
        channel=channel,
        heartbeat_interval=float(heartbeat),
        daily_consciousness_limit=int(budget),
    )


def load_and_build(path: str | Path = "cortiva.yaml") -> Fabric:
    """Convenience: load config and build fabric in one call."""
    config = load_config(path)
    return build_fabric(config)
