# Cortiva

**The organisational fabric for autonomous agent teams.**

Cortiva is an open-source Python framework for deploying teams of AI agents that operate as an organisation. Agents have persistent identity, experiential learning, governance hierarchies, peer communication, and consciousness budgeting.

Every other framework treats agents as workflow nodes or conversation participants. Cortiva treats them as **employees in a company** -- they wake up, plan their day, do their work, talk to colleagues, learn from experience, and reflect before they sleep.

## What Makes Cortiva Different

| Concept | Pipeline Frameworks | Cortiva |
|---|---|---|
| Agent identity | Config string | Living Summary that evolves from experience |
| Memory | Stateless or simple RAG | Pluggable persistent memory (InMemory, Engram, Neo4j) |
| Communication | Function calls between nodes | Peer messaging via real channels (Slack) |
| Governance | None | Role-based authority boundaries with approval workflows |
| Learning | None | Familiarity signals from accumulated experience |
| Lifecycle | Instantiate, run, dispose | Sleep, wake, plan, execute, reflect, sleep |
| Consciousness | Every call uses the best model | Budget-managed with backend fallback chains |

## Architecture

Cortiva has three cognitive layers, inspired by how biological nervous systems work:

- **Conscious layer** -- LLM APIs (Anthropic, OpenAI, Google) or terminal agents (Claude Code, Codex, Aider). Thinks, decides, acts. Reads identity from context. Produces output and reflection.
- **Subconscious layer** -- Ollama or local LLMs. Always running. Monitors, computes, routes. Assembles context. Invokes the conscious layer when real thought is needed.
- **Memory layer** -- InMemory, Engram, or Neo4j. Persistent experience. Written from reflections. Scanned for familiarity. Retrieved during conscious processing.

## Pluggable Everything

Cortiva does not build what already exists. Every component is an adapter:

- **Memory**: InMemory, Engram, Neo4j, or bring your own
- **Consciousness**: Anthropic, OpenAI, Google, or any LLM API
- **Routine**: Ollama, Simple (local), or any local model
- **Terminal**: Claude Code, Codex, Aider -- for agents that work through a terminal
- **Channel**: Slack, Discord, Teams, Internal, or any messaging platform

Use what works for you. Swap later without changing your agents.

## Core Concepts

### Agent Identity

Each agent is a directory of human-readable markdown files. Agents self-edit these files. The Living Summary regenerates from accumulated experience. Procedures get promoted from experiential memory. The file system is identity.

### The Cycle

Agents follow a daily cycle: **Wake, Plan, Execute, Replan, Reflect, Sleep**. The subconscious loads identity, checks the queue, and scans familiarity. The conscious layer builds plans, handles decisions, adjusts based on results, and writes reflections. Identity persists across cycles.

### Consciousness Budget

Not every thought needs the expensive model. The budget manager tracks daily allocation per agent, supports backend fallback chains, and routes routine work to the local model. Agents get more efficient over time as they build procedural knowledge.

### Governance

Authority boundaries are defined per agent. The framework enforces primary responsibilities (unilateral), secondary (requires approval), and escalation (beyond authority). Governance flows through the same communication channels agents use for everything else.

## Next Steps

- [Quick Start](quickstart.md) -- Install and run your first agent team
- [Configuration Reference](configuration.md) -- Full reference for cortiva.yaml
- [Channel Adapters](channels.md) -- Slack, Discord, Teams, and internal messaging
- [Writing Custom Adapters](adapters.md) -- Build your own memory, channel, or consciousness adapter
