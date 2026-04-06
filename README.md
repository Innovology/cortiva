```
     ██████╗ ██████╗ ██████╗ ████████╗██╗██╗   ██╗ █████╗
    ██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝██║██║   ██║██╔══██╗
    ██║     ██║   ██║██████╔╝   ██║   ██║██║   ██║███████║
    ██║     ██║   ██║██╔══██╗   ██║   ██║╚██╗ ██╔╝██╔══██║
    ╚██████╗╚██████╔╝██║  ██║   ██║   ██║ ╚████╔╝ ██║  ██║
     ╚═════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═══╝  ╚═╝  ╚═╝

        the organisational fabric for autonomous agent teams
```

<p align="center">

![CI](https://github.com/Innovology/cortiva/actions/workflows/ci.yml/badge.svg) ![Python](https://img.shields.io/badge/python-3.11%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![Status](https://img.shields.io/badge/status-pre--alpha-orange)

</p>

Cortiva is an open-source framework for deploying teams of AI agents that operate as an organisation — with persistent identity, experiential learning, governance hierarchies, peer communication, and consciousness budgeting.

Every other framework treats agents as workflow nodes or conversation participants. Cortiva treats them as **employees in a company** — they wake up, plan their day, do their work, talk to colleagues, learn from experience, and reflect before they sleep.

## What Makes Cortiva Different

| Concept | Pipeline Frameworks | Cortiva |
|---|---|---|
| Agent identity | Config string | Living Summary that evolves from experience |
| Memory | Stateless or simple RAG | Pluggable persistent memory (InMemory, Engram, Neo4j) |
| Communication | Function calls between nodes | Peer messaging via real channels (Slack) |
| Governance | None | Role-based authority boundaries with approval workflows |
| Learning | None | Familiarity signals from accumulated experience |
| Lifecycle | Instantiate → run → dispose | Sleep → wake → plan → execute → reflect → sleep |
| Consciousness | Every call uses the best model | Budget-managed with backend fallback chains |

## Architecture

Cortiva has three cognitive layers, inspired by how biological nervous systems work:

```
┌─────────────────────────────────────────────────────────────┐
│  CONSCIOUS        LLM API / Terminal Agent                  │
│  Anthropic, OpenAI, Google APIs; or terminal agents         │
│  (Claude Code, Codex, Aider). Thinks, decides, acts.       │
│  Reads identity from context. Produces output + reflection. │
├─────────────────────────────────────────────────────────────┤
│  SUBCONSCIOUS     Ollama / Local LLM                        │
│  Always running. Monitors, computes, routes. Assembles      │
│  context. Invokes the conscious layer when real thought     │
│  needed. ConsciousnessRouter selects backend per call type. │
├─────────────────────────────────────────────────────────────┤
│  MEMORY           InMemory / Engram / Neo4j                 │
│  Persistent experience. Written from reflections. Scanned   │
│  for familiarity. Retrieved during conscious processing.    │
└─────────────────────────────────────────────────────────────┘
```

### Pluggable Everything

Cortiva doesn't build what already exists. Every component is an adapter:

- **Memory**: InMemory, Engram, Neo4j, or bring your own
- **Consciousness**: Anthropic, OpenAI, OpenAI-compatible, Google, or any LLM API
- **Routine**: Ollama, Simple (local), or any local model
- **Terminal**: Claude Code, Codex, Aider — for agents that work through a terminal
- **Channel**: Slack, Discord, Microsoft Teams, Internal (in-process), or any messaging platform

Use what works for you. Swap later without changing your agents.

## Agent Isolation

Agents can be isolated from each other at different levels. Configure via `cortiva.yaml`:

```yaml
isolation:
  tier: soft              # none | soft | os | container
  container:              # tier 3 only
    runtime: docker
    cpu_limit: "1.0"
    memory_limit: "512m"
    network: "none"
```

| Tier | What It Does |
|------|-------------|
| **none** | No enforcement (default, backward compatible) |
| **soft** | Path traversal prevention, cross-agent memory guards, governance enforcement |
| **os** | + env var filtering, per-agent TMPDIR, per-agent IPC sockets |
| **container** | + Docker/Podman per agent with CPU/memory/network limits |

Each tier includes all protections from lower tiers. See [docs/isolation.md](docs/isolation.md) for the full guide and [docs/security.md](docs/security.md) for the trust model.

## Core Concepts

### Agent Identity (Subdirectory Workspace)

Each agent is a directory of human-readable markdown files, organised into subdirectories:

```
agents/bookkeep-01/
├── identity/
│   ├── identity.md          # Living Summary
│   ├── soul.md              # Persona
│   ├── skills.md            # Domain knowledge
│   ├── responsibilities.md  # R&R boundaries
│   └── procedures.md        # Procedural knowledge
├── today/
│   ├── plan.md              # Today's plan
│   ├── task_queue.json      # Runtime metrics
│   └── familiarity_signals.json
├── outbox/                  # Pending messages and escalations
├── journal/
│   └── YYYY-MM-DD.md        # Daily reflections
└── workspace/               # Working files
```

Agents self-edit these files. The Living Summary regenerates from accumulated experience. Procedures get promoted from experiential memory. The file system IS identity.

### The Cycle (Plan → Execute → Replan → Reflect)

```
WAKE    → Subconscious loads identity, checks queue, scans familiarity
PLAN    → Conscious builds today's plan from context + memory
EXECUTE → Subconscious routes tasks; conscious handles decisions
REPLAN  → Conscious adjusts plan based on results + new inputs
REFLECT → Conscious updates Living Summary, writes journal
SLEEP   → Identity persists, agent waits for next wake signal
```

### Consciousness Budget

Not every thought needs the expensive model. The budget manager tracks daily allocation per agent, supports backend fallback chains, and routes routine work to the local model. The ConsciousnessRouter selects the right backend for each call type: novel situations and reflection get the big model, routine work stays local.

Agents get more efficient over time. A new bookkeeper escalates 80% of tasks to the conscious layer. After six months of accumulated experience, it handles 80% procedurally and only escalates the genuinely novel.

### Governance

Authority boundaries are defined per agent in `responsibilities.md`. The framework enforces them:

- **Primary**: Tasks the agent handles unilaterally
- **Secondary**: Tasks requiring Head of Department approval
- **Escalation**: Tasks beyond the agent's authority

Governance flows through the same communication channels agents use for everything else. An approval request is a Slack message. An escalation is a message to the right person. The audit trail is the channel history.

## Quick Start

```bash
pip install cortiva

# Bootstrap the development team
cortiva bootstrap
cd .
export ANTHROPIC_API_KEY=sk-ant-...
cortiva start
cortiva agent wake dev-cortiva
```

## Configuration

```yaml
# cortiva.yaml
fabric:
  name: cortiva-bootstrap
  heartbeat_interval: 30

memory:
  adapter: inmemory       # inmemory | engram | neo4j | custom

consciousness:
  provider: anthropic     # anthropic | openai | google | custom
  model: claude-sonnet-4-20250514
  budget:
    daily_limit: 1000
    per_agent_default: 50
    backends:
      anthropic:
        calls_limit: 500
        tokens_limit: 200000
      openai:
        calls_limit: 300
        tokens_limit: 150000

terminal:
  adapter: claude-code    # claude-code | codex | aider

routine:
  adapter: ollama         # ollama | simple | custom
  model: qwen3.5:35b-a3b

channel:
  adapter: slack          # slack | custom

isolation:
  tier: soft              # none | soft | os | container

agents:
  directory: ./agents

schedules:
  dev-cortiva:
    wake: "09:00 mon-fri"
    replan: "13:00"
    sleep: "17:00"
  qa-cortiva:
    wake: "09:30 mon-fri"
    sleep: "17:00"
  pm-cortiva:
    wake: "08:30 mon-fri"
    replan: "12:00,15:00"
    sleep: "17:30"
```

## CLI Reference

```
# Workspace
cortiva init <name>                    Initialise a new workspace
cortiva bootstrap [--dir <path>]       Bootstrap the dev team (dev, qa, pm agents)
cortiva start                          Start the fabric daemon
cortiva stop                           Stop the fabric daemon
cortiva status                         Show agent status

# Monitoring
cortiva watch                          Live dashboard of all agents
cortiva capacity                       Node capacity and contention metrics
cortiva budget [--agent <id>]          Show consciousness budget status
cortiva discover                       Discover node capabilities

# Agent management
cortiva agent create <id> [-t <tpl>]   Register a new agent (optionally from template)
cortiva agent list                     List all agents
cortiva agent wake <id>                Wake an agent
cortiva agent sleep <id>               Put an agent to sleep
cortiva agent activity <id>            Show detailed agent activity and current task
cortiva agent hours <id> [--week]      Show working hours and overtime
cortiva agent move <id> --to <node>    Move an agent to another cluster node
cortiva agent snapshot <id>            Create a snapshot
cortiva agent snapshots <id>           List snapshots
cortiva agent rollback <id> --snapshot <sid>  Rollback to a snapshot
cortiva agent clone <id> --as <new>    Clone an agent from a snapshot
cortiva agent promote <id> --to <role> Promote an agent to a new role
cortiva agent probation <id>           Manage probation (--confirm | --revert | --extend N)
cortiva agent export <id>              Export an agent to a tarball
cortiva agent import <tarball> --as <id>  Import an agent from a tarball

# Skills (13,000+ from MCP ecosystem)
cortiva skill list [--category <cat>]  List available skills
cortiva skill search <query>           Search for skills
cortiva skill install <name> --agent <id>  Install a skill for an agent
cortiva skill uninstall <name> --agent <id>  Uninstall a skill
cortiva skill info <name>              Show skill details

# Organisation
cortiva org status                     Show org structure and reporting lines
cortiva delegate <from> <to> <desc>    Delegate work to an agent
cortiva approve list                   List pending approvals
cortiva approve accept <id>            Approve a request
cortiva approve reject <id>            Reject a request

# Templates & Portal
cortiva template list                  List available agent templates
cortiva portal [--host H] [--port P]   Start the web portal

# Cluster
cortiva cluster status                 Show cluster status
cortiva cluster nodes                  Show cluster nodes with details
cortiva cluster load                   Show load metrics and balancing suggestions
```

## Project Status

**Pre-alpha.** The bootstrap team (dev-cortiva, qa-cortiva, pm-cortiva) builds the framework while running on it. Expect breaking changes.

## Roadmap

### Implemented

**Core Framework**
- Agent lifecycle with subdirectory workspace (register, wake, sleep, status)
- Plan-execute-replan cycle with exception batching and parallel heartbeat
- Session management for conversation continuity within wake cycles
- Per-agent API clients for credential isolation

**Adapters**
- Memory: InMemory, Engram, Neo4j (with graph operations)
- Consciousness: Anthropic, OpenAI, OpenAI-compatible, Google
- Routine: Ollama, Simple
- Terminal: Claude Code, Codex, Aider
- Channel: Slack, Discord, Microsoft Teams, Internal (in-process)

**Organisation**
- Org model with departments, reporting lines, and roles
- Work delegation from managers to subordinates
- Shared org-wide knowledge tier
- Approval workflow with policy-driven routing
- OKR/Goals system for org-level objective tracking
- Performance reviews with periodic metrics aggregation
- Agent termination with knowledge handover and archival

**Security & Isolation**
- Three-tier agent isolation (none, soft, os, container)
- Execution policies: tool permissions, action approvals, filesystem restrictions
- Governance enforcement via keyword-overlap AuthorityValidator
- GuardedMemoryAdapter for cross-agent memory isolation
- Context cross-contamination guard
- Tamper-evident hash-chained audit log
- Container templates (Dockerfile, docker-compose) with browser sidecar

**Observability**
- Live dashboard TUI (`cortiva watch`)
- Node capacity and contention tracking
- Agent timesheet with working hours and overtime
- Consciousness budget manager with backend fallback chains

**Ecosystem**
- 13,000+ skills synced from the MCP server registry
- Skill install/uninstall/search CLI
- ConsciousnessRouter for per-call-type backend selection
- Familiarity engine and Living Summary regeneration
- Multi-node cluster with discovery, model registry, agent mobility
- Snapshot engine with rollback, clone, and pre-edit safety snapshots
- Promotion engine with probation and auto-assessment
- Web portal API (FastAPI) with auth, JWT, roles
- 40+ CLI commands
- PyPI release workflow

### Planned — Agent Autonomy ([roadmap](docs/roadmap-agent-autonomy.md))

- Agent-owned cognitive loop (move context/LLM/memory from Fabric to agent boundary)
- Per-agent memory stores (physical isolation, independent growth)
- Agent-side budget and schedule enforcement (contract.yaml)
- Budget proxy consciousness adapter (hard external limits)
- Per-agent API credentials via secret store
- Signed lifecycle commands

### Remaining

- Persona parameter evolution
- AR evaluation (comparing outputs against outcomes)
- Template marketplace
- Dashboard web UI (portal API exists, frontend pending)

## Contributing

Cortiva is MIT licensed. Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
