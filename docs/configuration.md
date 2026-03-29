# Configuration Reference

Cortiva is configured through a `cortiva.yaml` file in the project root. This page documents every configuration section.

## fabric

Top-level settings for the Cortiva runtime.

```yaml
fabric:
  name: cortiva-bootstrap
  heartbeat_interval: 30
```

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | `cortiva` | Name of this fabric instance. |
| `heartbeat_interval` | number | `30` | Seconds between heartbeat ticks. |

## memory

Configures the persistent memory adapter.

```yaml
memory:
  adapter: inmemory
  config:
    namespace: default
```

| Key | Type | Default | Description |
|---|---|---|---|
| `adapter` | string | `inmemory` | Memory backend. One of `inmemory`, `engram`, `neo4j`. |
| `config` | object | `{}` | Adapter-specific settings passed to the constructor. |

### Adapter-specific config

**inmemory** -- No additional config required. Data is held in process memory and lost on restart.

**engram** -- Connects to an Engram server. Each agent gets its own namespace for memory isolation.

```yaml
memory:
  adapter: engram
  config:
    namespace: cortiva
```

**neo4j** -- Connects to a Neo4j graph database.

```yaml
memory:
  adapter: neo4j
  config:
    uri: bolt://localhost:7687
    user: neo4j
    password: secret
```

## consciousness

Configures the primary LLM provider for the conscious layer.

```yaml
consciousness:
  provider: anthropic
  model: claude-sonnet-4-20250514
  api_key: sk-ant-...
  max_tokens: 4096
```

| Key | Type | Default | Description |
|---|---|---|---|
| `provider` | string | `anthropic` | LLM provider. One of `anthropic`, `openai`, `openai-compatible`, `google`. |
| `model` | string | Provider default | Model name to use. |
| `api_key` | string | From env | API key. Falls back to `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GOOGLE_API_KEY`. |
| `base_url` | string | Provider default | Base URL for the API. Useful for `openai-compatible` providers. |
| `max_tokens` | integer | Provider default | Maximum tokens per response. |

### Per-call-type overrides

You can route different call types to different providers using the `overrides` section:

```yaml
consciousness:
  provider: anthropic
  model: claude-sonnet-4-20250514
  overrides:
    reflection:
      provider: openai
      model: gpt-4o
    planning:
      provider: google
      model: gemini-2.0-flash
```

Each override key is a call type, and the value is a full consciousness config block.

### budget

Controls consciousness spending across agents and backends.

```yaml
consciousness:
  provider: anthropic
  model: claude-sonnet-4-20250514
  budget:
    daily_limit: 1000
    per_agent_default: 50
    alert_threshold: 0.8
    backend_type: api
    fallback_chain:
      - api
      - local
    backends:
      anthropic:
        calls_limit: 500
        tokens_limit: 200000
      openai:
        calls_limit: 300
        tokens_limit: 150000
```

| Key | Type | Default | Description |
|---|---|---|---|
| `daily_limit` | integer | `1000` | Total daily consciousness calls across all agents. |
| `per_agent_default` | integer | `50` | Default daily call budget per agent. |
| `alert_threshold` | float | `0.8` | Fraction of budget at which alerts fire. |
| `backend_type` | string | `api` | Primary backend type. One of `api`, `terminal`, `local`. |
| `fallback_chain` | list | `[backend_type]` | Ordered list of backends to try when the primary is exhausted. |
| `backends` | object | -- | Per-backend limits keyed by backend name. |

## routine

Configures the local LLM used for the subconscious layer.

```yaml
routine:
  adapter: ollama
  model: qwen3.5:35b-a3b
  config: {}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `adapter` | string | -- | Routine backend. One of `ollama`, `simple`. |
| `model` | string | -- | Model name for the routine adapter. |
| `config` | object | `{}` | Adapter-specific settings. |

## channel

Configures the messaging adapter for peer communication.

```yaml
channel:
  adapter: slack
  config:
    token: xoxb-...
```

| Key | Type | Default | Description |
|---|---|---|---|
| `adapter` | string | -- | Channel backend. Currently `slack`. |
| `config` | object | `{}` | Adapter-specific settings. |

The Slack adapter reads the bot token from `config.token` or the `SLACK_BOT_TOKEN` environment variable.

## terminal

Configures terminal agent adapters for agents that work through a CLI tool.

```yaml
terminal:
  adapter: claude-code
  config: {}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `adapter` | string | -- | Terminal backend. One of `claude-code`, `codex`, `aider`. |
| `config` | object | `{}` | Adapter-specific settings. |

## agents

Configures where agent workspaces live on disk.

```yaml
agents:
  directory: ./agents
```

| Key | Type | Default | Description |
|---|---|---|---|
| `directory` | string | `./agents` | Path to the agents directory. |

## schedules

Defines automated wake/replan/sleep schedules per agent.

```yaml
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

Each agent ID maps to a schedule object:

| Key | Type | Description |
|---|---|---|
| `wake` | string | Time and optional day filter for the wake signal. Format: `HH:MM [day-range]`. |
| `replan` | string | Time(s) for mid-day replanning. Comma-separated for multiple times. |
| `sleep` | string | Time for the sleep signal. |

## cluster

Configures multi-node clustering for distributed agent teams.

```yaml
cluster:
  endpoints:
    - http://node-1:8100
    - http://node-2:8100
```

| Key | Type | Default | Description |
|---|---|---|---|
| `endpoints` | list | `[]` | List of peer node URLs for discovery and agent mobility. |

Clustering enables agent mobility (`cortiva agent move`), distributed discovery (`cortiva discover`), and automatic load balancing across nodes.

## Full Example

```yaml
fabric:
  name: my-team
  heartbeat_interval: 30

memory:
  adapter: engram
  config:
    namespace: my-team

consciousness:
  provider: anthropic
  model: claude-sonnet-4-20250514
  budget:
    daily_limit: 1000
    per_agent_default: 50
    backend_type: api
    fallback_chain:
      - api
      - local

routine:
  adapter: ollama
  model: qwen3.5:35b-a3b

channel:
  adapter: slack
  config:
    token: xoxb-...

terminal:
  adapter: claude-code

agents:
  directory: ./agents

schedules:
  dev-01:
    wake: "09:00 mon-fri"
    replan: "13:00"
    sleep: "17:00"

cluster:
  endpoints:
    - http://node-1:8100
    - http://node-2:8100
```
