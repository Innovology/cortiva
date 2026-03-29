# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Core framework** — Fabric orchestrator for autonomous agent teams with plan-execute-replan
  lifecycle, signal handling, and graceful shutdown.
- **Agent model** — Task queue, subdirectory workspace layout, flat-to-nested migration, and
  workspace helpers for managing agent state.
- **Reflection engine** — Structured JSON metadata parsing from consciousness responses using a
  `---REFLECTION---` delimiter to extract learnings, prediction errors, procedure updates,
  inter-agent messages, and escalation requests.
- **Context builder** — Assembles planning, execution, reflection, and replan contexts for agents.
- **Scheduler** — Time-based agent lifecycle management with cron-like schedule parsing.
- **Emotion model** — Dimensional emotion tracking (valence/arousal/dominance) with decay over time.
- **Familiarity engine** — Memory-based pattern recognition for routing routine vs novel tasks.
- **Governance** — Inter-agent authority rules, delegation chains, and escalation policies.
- **IPC server** — Unix socket server/client for daemon communication with PID management.
- **Consciousness router** — Per-call-type provider overrides allowing different backends for plan,
  execute, and replan phases.
- **Living summary** — Experience-based `identity.md` rewriting during reflection cycles.
- **Budget manager** — Multi-backend budget tracking with fallback chains, per-backend call/token
  limits, priority escalation, and alert callbacks.
- **Cluster architecture** — Multi-node management with heartbeats, timeout detection, and agent
  registry.
- **Model registry** — Unified model view across nodes with local-first resolution.
- **Node discovery** — Auto-detect terminal agents, Ollama models, custom endpoints, and system
  resources. Supports static, config file, and mDNS/Bonjour discovery modes.
- **Agent mobility** — Migration between nodes with sync, validation, and atomic registry updates.
- **Cluster balancer** — Communication tracker, cluster metrics, and load-aware agent migration
  suggestions.
- **Snapshot engine** — Create, list, restore, clone, and delete snapshots with automatic pre-edit
  safety snapshots.
- **Promotion engine** — Initiate, confirm, revert, and extend probation with pre-promotion
  snapshots and role-and-responsibility transitions.
- **Runtime state persistence** — Serialize task queue, exceptions, and familiarity signals to the
  `today/` workspace directory with daily reset on each wake cycle.
- **Event emitter** — Event bus system for portal and WebSocket integration.
- **Adapters — Consciousness**: Anthropic, Google (Gemini), and OpenAI-compatible providers.
- **Adapters — Memory**: In-memory store and Neo4j graph memory with Cypher queries and
  edge/emotion support.
- **Adapters — Channel**: Slack adapter with async polling, thread support, and message formatting.
- **Adapters — Terminal**: Claude Code, Codex (stub), and Aider (stub) adapters.
- **Adapters — Routine**: Ollama and simple local routine adapters for procedural tasks.
- **Portal auth** — SQLite-backed user management, JWT tokens (access and refresh), role-based
  permissions (owner/admin/manager/observer), audit logging, org settings, and first-user
  bootstrap.
- **Portal server** — FastAPI app with REST endpoints for agents, identity files, journals,
  snapshots, cluster, budget, and a WebSocket feed. Includes a plugin system for extensions.
- **Agent templates** — Bundled dev-cortiva, pm-cortiva, and qa-cortiva templates with full
  identity files, procedures, skills, and soul definitions.
- **Config loader** — YAML-based configuration with adapter registries for all provider types,
  budget manager builder, and cluster config support.
- **CLI** — `cortiva start` with `--portal` flag, `agent create/status/export/import`,
  `template list`, `start/stop/wake/sleep`, `budget`, `logs`, `snapshot/snapshots/rollback/clone`,
  `promote/probation`, `portal`, `bootstrap`, `discover`, and `cluster status/nodes/load/move`
  commands.
