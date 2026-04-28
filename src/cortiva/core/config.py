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

from cortiva.core.budget import BackendType, ConsciousnessBudgetManager
from cortiva.core.credentials import CredentialConfig, CredentialProvider
from cortiva.core.data_boundary import DataBoundaryConfig, DataBoundaryEnforcer
from cortiva.core.encryption import EncryptionConfig, EncryptionVault
from cortiva.core.fabric import Fabric
from cortiva.core.isolation import IsolationConfig, IsolationTier, build_enforcer
from cortiva.core.memory_guard import GuardedMemoryAdapter
from cortiva.core.org import parse_org_config

# ---------------------------------------------------------------------------
# Adapter registry — maps config names to (module, class) pairs.
# Imports are lazy so optional deps don't explode at import time.
# ---------------------------------------------------------------------------

_MEMORY_ADAPTERS: dict[str, tuple[str, str]] = {
    "inmemory": ("cortiva.adapters.memory.inmemory", "InMemoryAdapter"),
    "engram": ("cortiva.adapters.memory.engram", "EngramMemoryAdapter"),
    "neo4j": ("cortiva.adapters.memory.neo4j", "Neo4jMemoryAdapter"),
}

_CONSCIOUSNESS_ADAPTERS: dict[str, tuple[str, str]] = {
    "anthropic": ("cortiva.adapters.consciousness.anthropic", "AnthropicConsciousnessAdapter"),
    "openai": ("cortiva.adapters.consciousness.openai_compat", "OpenAICompatibleAdapter"),
    "openai-compatible": ("cortiva.adapters.consciousness.openai_compat", "OpenAICompatibleAdapter"),
    "google": ("cortiva.adapters.consciousness.google", "GoogleAdapter"),
    "cortiva-routed": (
        "cortiva.adapters.consciousness.cortiva_routed",
        "CortivaRoutedConsciousnessAdapter",
    ),
}

_CHANNEL_ADAPTERS: dict[str, tuple[str, str]] = {
    "slack": ("cortiva.adapters.channel.slack", "SlackChannelAdapter"),
}

_ROUTINE_ADAPTERS: dict[str, tuple[str, str]] = {
    "simple": ("cortiva.adapters.routine.simple", "SimpleRoutineAdapter"),
    "ollama": ("cortiva.adapters.routine.ollama", "OllamaRoutineAdapter"),
}

_TERMINAL_ADAPTERS: dict[str, tuple[str, str]] = {
    "claude-code": ("cortiva.adapters.terminal.claude_code", "ClaudeCodeAdapter"),
    "codex": ("cortiva.adapters.terminal.codex", "CodexAdapter"),
    "aider": ("cortiva.adapters.terminal.aider", "AiderAdapter"),
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


_BACKEND_TYPE_MAP: dict[str, BackendType] = {
    "terminal": BackendType.TERMINAL,
    "api": BackendType.API,
    "local": BackendType.LOCAL,
}


def _build_budget_manager(config: dict[str, Any]) -> ConsciousnessBudgetManager | None:
    """Build a budget manager from config, or return None for legacy mode.

    Supports both extended config (with backend_type/fallback_chain) and
    legacy format (just daily_limit) for backward compatibility.
    """
    budget_section = config.get("consciousness", {}).get("budget")
    if not budget_section or not isinstance(budget_section, dict):
        return None

    # Determine primary backend
    backend_name = budget_section.get("backend_type", "api")
    default_backend = _BACKEND_TYPE_MAP.get(backend_name, BackendType.API)

    # Build fallback chain
    chain_names = budget_section.get("fallback_chain", [backend_name])
    fallback_chain = [
        _BACKEND_TYPE_MAP.get(n, BackendType.API) for n in chain_names
    ]

    # Build per-backend configs
    backend_configs: dict[BackendType, dict[str, Any]] = {}
    for bt_name, bt_enum in _BACKEND_TYPE_MAP.items():
        if bt_name in budget_section and isinstance(budget_section[bt_name], dict):
            backend_configs[bt_enum] = budget_section[bt_name]

    # Legacy compat: if no per-backend config exists, create one from daily_limit
    if not backend_configs:
        daily_limit = budget_section.get("daily_limit", 1000)
        backend_configs[default_backend] = {"calls_limit": daily_limit}

    alert_threshold = float(budget_section.get("alert_threshold", 0.8))

    return ConsciousnessBudgetManager(
        default_backend=default_backend,
        fallback_chain=fallback_chain,
        backend_configs=backend_configs,
        alert_threshold=alert_threshold,
    )


_PROVIDER_ENV_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compatible": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def _build_consciousness_adapter(con_section: dict[str, Any]) -> Any:
    """Instantiate a consciousness adapter from a config section.

    Supports:
      consciousness:
        provider: anthropic | openai | openai-compatible | google
        model: <model-name>
        api_key: <key>          # or from env
        base_url: <url>         # for openai-compatible
    """
    con_name = con_section.get("provider", "anthropic")
    con_cls = _import_adapter(_CONSCIOUSNESS_ADAPTERS, con_name, "consciousness")
    con_kwargs: dict[str, Any] = {}
    if "model" in con_section:
        con_kwargs["model"] = con_section["model"]
    env_key = _PROVIDER_ENV_KEYS.get(con_name, "ANTHROPIC_API_KEY")
    api_key = con_section.get("api_key") or os.environ.get(env_key)
    if api_key:
        con_kwargs["api_key"] = api_key
    if "base_url" in con_section:
        con_kwargs["base_url"] = con_section["base_url"]
    if "max_tokens" in con_section:
        con_kwargs["max_tokens"] = con_section["max_tokens"]
    return con_cls(**con_kwargs)


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

    # --- Consciousness adapter (with optional per-call-type routing) ---
    con_section = config.get("consciousness", {})
    consciousness = _build_consciousness_adapter(con_section)

    overrides_section = con_section.get("overrides")
    if overrides_section and isinstance(overrides_section, dict):
        from cortiva.core.consciousness_router import ConsciousnessRouter

        override_adapters: dict[str, Any] = {}
        for call_type, override_cfg in overrides_section.items():
            if isinstance(override_cfg, dict):
                override_adapters[call_type] = _build_consciousness_adapter(override_cfg)
            # String values (e.g. "terminal") are reserved for future
            # terminal-agent routing and are ignored for now.
        if override_adapters:
            consciousness = ConsciousnessRouter(
                default=consciousness, overrides=override_adapters
            )

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

    # --- Routine adapter (optional) ---
    routine = None
    routine_section = config.get("routine")
    if routine_section:
        routine_name = routine_section.get("adapter")
        if routine_name:
            routine_cls = _import_adapter(_ROUTINE_ADAPTERS, routine_name, "routine")
            routine_kwargs: dict[str, Any] = dict(routine_section.get("config", {}))
            routine = routine_cls(**routine_kwargs)

    # --- Terminal adapter (optional) ---
    terminal = None
    term_section = config.get("terminal")
    if term_section:
        term_name = term_section.get("adapter")
        if term_name:
            term_cls = _import_adapter(_TERMINAL_ADAPTERS, term_name, "terminal")
            term_kwargs: dict[str, Any] = dict(term_section.get("config", {}))
            terminal = term_cls(**term_kwargs)

    # --- Budget manager (optional) ---
    budget_manager = _build_budget_manager(config)

    # --- Isolation ---
    isolation_section = config.get("isolation", {})
    isolation_config = IsolationConfig.from_dict(isolation_section) if isolation_section else None
    agents_dir = Path(config.get("agents", {}).get("directory", "./agents"))
    enforcer = build_enforcer(agents_dir=agents_dir, config=isolation_config)

    # Wrap memory in GuardedMemoryAdapter when isolation is active
    if isolation_config and isolation_config.tier != IsolationTier.NONE:
        memory = GuardedMemoryAdapter(inner=memory, enforcer=enforcer)

    # --- Fabric ---
    heartbeat = config.get("fabric", {}).get("heartbeat_interval", 30)
    budget = config.get("consciousness", {}).get("budget", {}).get("daily_limit", 1000)

    fabric = Fabric(
        agents_dir=agents_dir,
        memory=memory,
        consciousness=consciousness,
        routine=routine,
        channel=channel,
        terminal=terminal,
        heartbeat_interval=float(heartbeat),
        daily_consciousness_limit=int(budget),
        budget_manager=budget_manager,
        isolation=enforcer,
    )

    # --- Agent schedules (optional) ---
    schedules = config.get("schedules")
    if schedules and isinstance(schedules, dict):
        fabric.load_schedules(schedules)

    # --- Org model (optional) ---
    org_section = config.get("org")
    fabric.org = parse_org_config(org_section)

    # --- Encryption at rest (optional) ---
    encryption_section = config.get("encryption", {})
    encryption_config = EncryptionConfig.from_dict(encryption_section)
    fabric.encryption_vault = EncryptionVault.from_config(encryption_config, agents_dir)

    # --- Credential delegation (optional) ---
    cred_section = config.get("credentials", {})
    cred_config = CredentialConfig.from_dict(cred_section)
    fabric.credential_provider = CredentialProvider(cred_config)

    # --- Data boundary (optional) ---
    boundary_section = config.get("data_boundary", {})
    boundary_config = DataBoundaryConfig.from_dict(boundary_section)
    fabric.data_boundary = DataBoundaryEnforcer(boundary_config)

    # --- Hooks (optional) ---
    hooks_section = config.get("hooks")
    if hooks_section and isinstance(hooks_section, dict):
        fabric.hook_router.load(hooks_section)

    # --- Plugins (optional) ---
    plugins_list = config.get("plugins")
    if plugins_list and isinstance(plugins_list, list):
        from cortiva.core.plugins import load_plugins_from_config
        for plugin in load_plugins_from_config(plugins_list):
            fabric.plugin_manager.register(plugin)

    # --- Reactive triggers (optional) ---
    triggers_list = config.get("triggers")
    if triggers_list and isinstance(triggers_list, list):
        fabric.reactive_engine.load(triggers_list)

    # --- Resource limits (optional) ---
    resource_section = config.get("resource_limits")
    if resource_section and isinstance(resource_section, dict):
        fabric.resource_guard.load(resource_section)

    # --- Execution policies (optional) ---
    policies_section = config.get("policies")
    if policies_section and isinstance(policies_section, dict):
        fabric.policy_manager.load(policies_section)

    # --- Cluster config (optional) ---
    cluster_section = config.get("cluster", {})
    endpoints = cluster_section.get("endpoints")
    if endpoints and isinstance(endpoints, list):
        fabric._custom_endpoints = endpoints
    else:
        fabric._custom_endpoints = []

    fabric._cluster_config = cluster_section

    return fabric


def load_and_build(path: str | Path = "cortiva.yaml") -> Fabric:
    """Convenience: load config and build fabric in one call."""
    config = load_config(path)
    return build_fabric(config)
