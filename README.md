```
     ██████╗ ██████╗ ██████╗ ████████╗██╗██╗   ██╗ █████╗
    ██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝██║██║   ██║██╔══██╗
    ██║     ██║   ██║██████╔╝   ██║   ██║██║   ██║███████║
    ██║     ██║   ██║██╔══██╗   ██║   ██║╚██╗ ██╔╝██╔══██║
    ╚██████╗╚██████╔╝██║  ██║   ██║   ██║ ╚████╔╝ ██║  ██║
     ╚═════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═══╝  ╚═╝  ╚═╝

        the organisational fabric for autonomous agent teams
```

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
- **Consciousness**: Anthropic, OpenAI, Google, or any LLM API
- **Routine**: Ollama, Simple (local), or any local model
- **Terminal**: Claude Code, Codex, Aider — for agents that work through a terminal
- **Channel**: Slack, or any messaging platform

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
cortiva init <name>                    Initialise a new workspace
cortiva bootstrap [--dir <path>]       Bootstrap the dev team (dev, qa, pm agents)
cortiva start                          Start the fabric daemon
cortiva stop                           Stop the fabric daemon
cortiva status                         Show agent status
cortiva discover                       Discover node capabilities
cortiva budget [--agent <id>]          Show consciousness budget status
cortiva portal [--host H] [--port P]   Start the web portal

cortiva agent create <id> [-t <tpl>]   Register a new agent (optionally from template)
cortiva agent list                     List all agents
cortiva agent wake <id>                Wake an agent
cortiva agent sleep <id>               Put an agent to sleep
cortiva agent move <id> --to <node>    Move an agent to another cluster node
cortiva agent snapshot <id>            Create a snapshot
cortiva agent snapshots <id>           List snapshots
cortiva agent rollback <id> --snapshot <sid>  Rollback to a snapshot
cortiva agent clone <id> --as <new>    Clone an agent from a snapshot
cortiva agent promote <id> --to <role> Promote an agent to a new role
cortiva agent probation <id>           Manage probation (--confirm | --revert | --extend N)

cortiva template list                  List available agent templates

cortiva cluster status                 Show cluster status
cortiva cluster nodes                  Show cluster nodes with details
cortiva cluster load                   Show load metrics and balancing suggestions
```

## Project Status

**Pre-alpha.** The bootstrap team (dev-cortiva, qa-cortiva, pm-cortiva) builds the framework while running on it. Expect breaking changes.

## Roadmap

### Implemented

- Agent lifecycle with subdirectory workspace (register, wake, sleep, status)
- Plan-execute-replan cycle with exception batching
- Pluggable adapters: memory (InMemory, Engram, Neo4j), consciousness (Anthropic, OpenAI, Google), routine (Ollama, Simple), channel (Slack), terminal (Claude Code, Codex, Aider)
- Consciousness budget manager with backend fallback chains
- ConsciousnessRouter for per-call-type backend selection
- Familiarity engine and Living Summary regeneration
- Multi-node cluster with discovery, model registry, agent mobility, auto-balancing
- Snapshot engine with rollback, clone, and pre-edit safety snapshots
- Promotion engine with probation
- Web portal API (FastAPI) with auth, JWT, roles, audit logging
- CLI with 25+ commands
- IPC daemon communication via Unix sockets
- Scheduled wake/replan/sleep cycles
- Agent templates and bootstrap workflow

- Three-tier agent isolation (none, soft, os, container)
- Governance enforcement via keyword-overlap AuthorityValidator
- GuardedMemoryAdapter for cross-agent memory isolation
- Container templates (Dockerfile, docker-compose)

### In Progress — Agent Autonomy ([roadmap](docs/roadmap-agent-autonomy.md))

- Agent-owned cognitive loop (move context/LLM/memory from Fabric to agent boundary)
- Per-agent memory stores (physical isolation, independent growth)
- Agent-side budget and schedule enforcement (contract.yaml)
- Budget proxy consciousness adapter (hard external limits)
- Per-agent API credentials via secret store
- Internal channel adapter (agent-to-agent without Slack)
- Signed lifecycle commands
- Tamper-evident audit logging

### Remaining

- Persona parameter evolution
- AR evaluation (comparing outputs against outcomes)
- Template marketplace
- Discord channel adapter
- Dashboard UI (portal API exists, frontend pending)

## Contributing

Cortiva is MIT licensed. Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
