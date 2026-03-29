# Quick Start

This guide covers installing Cortiva, bootstrapping a team, configuring your environment, and waking your first agent.

## Installation

Install Cortiva from PyPI:

```bash
pip install cortiva
```

To install with specific adapter dependencies:

```bash
# With Anthropic consciousness adapter
pip install cortiva[anthropic]

# With Slack channel adapter
pip install cortiva[slack]

# With all adapters
pip install cortiva[all]
```

## Bootstrap a Team

The `bootstrap` command creates a starter team with three agents: a developer, a QA engineer, and a project manager.

```bash
cortiva bootstrap
```

This creates the following structure in your current directory:

```
agents/
  dev-cortiva/
    identity/
      identity.md
      soul.md
      skills.md
      responsibilities.md
      procedures.md
    today/
      plan.md
      task_queue.json
      familiarity_signals.json
    outbox/
    journal/
    workspace/
  qa-cortiva/
    ...
  pm-cortiva/
    ...
cortiva.yaml
```

Each agent has a directory of human-readable markdown files that define its identity, responsibilities, and accumulated knowledge. Agents read and write these files during their lifecycle.

## Configuration

The bootstrap command generates a `cortiva.yaml` file. At minimum you need to set your LLM provider API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

The default configuration uses the Anthropic provider, in-memory storage, and the Ollama routine adapter. See the [Configuration Reference](configuration.md) for full details on all available options.

## Start the Fabric

The fabric is the runtime daemon that manages agents, heartbeats, and scheduling:

```bash
cortiva start
```

The fabric communicates with the CLI over a Unix socket. Use `cortiva status` to verify it is running.

## Wake Your First Agent

```bash
cortiva agent wake dev-cortiva
```

When an agent wakes, the framework:

1. Loads the agent's identity files into context.
2. Scans the familiarity engine for relevant accumulated experience.
3. Invokes the conscious layer to build a plan for the day.
4. Begins executing tasks from the plan.

You can check the agent's status at any time:

```bash
cortiva status
```

## The Agent Lifecycle

Once awake, an agent follows this cycle:

- **Plan** -- The conscious layer builds a plan based on identity, memory, and any pending messages.
- **Execute** -- Tasks are routed by the subconscious. Routine work stays local; novel situations go to the conscious layer.
- **Replan** -- If conditions change (exceptions, new inputs), the plan is adjusted mid-day.
- **Reflect** -- At the end of the cycle, the conscious layer updates the Living Summary and writes a journal entry.
- **Sleep** -- Identity persists. The agent waits for the next wake signal.

## Scheduled Cycles

You can configure agents to wake and sleep on a schedule in `cortiva.yaml`:

```yaml
schedules:
  dev-cortiva:
    wake: "09:00 mon-fri"
    replan: "13:00"
    sleep: "17:00"
```

See [Configuration Reference](configuration.md) for the full schedule syntax.

## Next Steps

- Configure additional adapters (memory, channels, terminal agents) in the [Configuration Reference](configuration.md).
- Use `cortiva agent create <id>` to add more agents.
- Use `cortiva agent snapshot <id>` to create restore points before major changes.
