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
| Memory | Stateless or simple RAG | Pluggable persistent memory (Engram, Mem0, Letta, Neo4j) |
| Communication | Function calls between nodes | Peer messaging via real channels (Slack) |
| Governance | None | Role-based authority boundaries with approval workflows |
| Learning | None | Familiarity signals from accumulated experience |
| Lifecycle | Instantiate → run → dispose | Sleep → wake → plan → execute → reflect → sleep |
| Consciousness | Every call uses the best model | Budget-managed: routine work stays local, hard problems get the big model |

## Architecture

Cortiva has three cognitive layers, inspired by how biological nervous systems work:

```
┌─────────────────────────────────────────────┐
│  CONSCIOUS        Claude / LLM API          │
│  Invoked per decision. Thinks, decides,     │
│  acts. Reads identity from context.         │
│  Produces output + reflection. Then gone.   │
├─────────────────────────────────────────────┤
│  SUBCONSCIOUS     Qwen / Ollama / Local LLM │
│  Always running. Monitors, computes,        │
│  routes. Assembles context. Invokes the     │
│  conscious layer when real thought needed.  │
├─────────────────────────────────────────────┤
│  MEMORY           Engram / Mem0 / Neo4j     │
│  Persistent experience. Written from        │
│  reflections. Scanned for familiarity.      │
│  Retrieved during conscious processing.     │
└─────────────────────────────────────────────┘
```

### Pluggable Everything

Cortiva doesn't build what already exists. Every component is an adapter:

- **Memory**: Engram, Mem0, Letta, Neo4j, or bring your own
- **Consciousness**: Claude (API or Max plan), OpenAI, Anthropic, any LLM API
- **Routine**: Qwen via Ollama, llama.cpp, vLLM, or any local model
- **Channel**: Slack, Discord, or any messaging platform

Use what works for you. Swap later without changing your agents.

## Core Concepts

### Agent Identity (Markdown Files)

Each agent is a directory of human-readable markdown files:

```
agents/bookkeep-01/
├── identity.md          # Living Summary — who am I today
├── soul.md              # Persona — disposition parameters
├── skills.md            # Domain knowledge and capabilities
├── responsibilities.md  # R&R — authority boundaries
├── procedures.md        # Promoted procedural knowledge
├── plan.md              # Today's plan
└── journal/
    └── 2026-03-03.md    # Daily reflection
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

Not every thought needs the expensive model. The budget manager tracks daily allocation per agent, routes routine work to the local model, and reserves conscious thought for what matters: novel situations, ambiguous judgements, creative problem-solving, and reflection.

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

# Initialise a new Cortiva workspace
cortiva init my-org

# Register your first agent
cortiva agent create bookkeep-01 --template bookkeeper

# Configure adapters
cortiva config set memory.adapter engram
cortiva config set consciousness.provider anthropic
cortiva config set consciousness.model claude-sonnet-4-20250514
cortiva config set channel.adapter slack

# Start the fabric
cortiva start

# Check status
cortiva status
```

## Configuration

```yaml
# cortiva.yaml
fabric:
  name: my-organisation
  heartbeat_interval: 30  # seconds

memory:
  adapter: engram  # engram | mem0 | letta | neo4j | custom
  config:
    # adapter-specific configuration

consciousness:
  provider: anthropic  # anthropic | openai | custom
  model: claude-sonnet-4-20250514
  budget:
    daily_limit: 1000  # calls per day across all agents
    per_agent_default: 50

routine:
  adapter: ollama  # ollama | llamacpp | vllm | custom
  model: qwen3.5:35b-a3b

channel:
  adapter: slack  # slack | discord | custom
  config:
    # adapter-specific configuration

agents:
  directory: ./agents
```

## Project Status

🚧 **Pre-alpha.** We're building v0.1 with three agents (Chief of Staff, BookKeep-01, Dev-Acct-01) to validate the architecture. Expect breaking changes.

## Roadmap

### v0.1 — Foundation
- [ ] Agent lifecycle management (register, wake, sleep, status)
- [ ] Markdown identity files with standard structure
- [ ] Plan-execute-replan cycle
- [ ] Pluggable memory adapter (Engram first)
- [ ] Pluggable consciousness adapter (Anthropic first)
- [ ] Pluggable routine adapter (Ollama first)
- [ ] Slack integration for inter-agent messaging
- [ ] Consciousness budget tracking
- [ ] Scheduled heartbeats
- [ ] CLI: `cortiva start`, `cortiva status`, `cortiva agent wake bookkeep-01`

### v0.2 — Learning
- [ ] Familiarity signal computation
- [ ] Emotion derivation from task outcomes
- [ ] Procedural promotion from experience to indexed knowledge
- [ ] Living Summary auto-regeneration
- [ ] Decision audit trail

### v0.3 — Governance
- [ ] R&R authority validation
- [ ] Approval workflows
- [ ] Adversarial Challenge / QA auditing
- [ ] Cross-department communication patterns
- [ ] Goal routing with authority boundaries

### v0.4 — Evolution
- [ ] Persona parameter evolution
- [ ] Agent cloning
- [ ] AR evaluation (comparing outputs against outcomes)
- [ ] Web dashboard
- [ ] Template marketplace

## Contributing

Cortiva is MIT licensed. Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
