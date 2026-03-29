# Roadmap: Agent Autonomy and Fabric Decomposition

This document describes the architectural evolution from the current centralised Fabric to a model where agents own their own cognitive loops, enforce their own budgets, and communicate exclusively through well-defined channel boundaries.

## The Problem

The Fabric is currently a god object. It reads every agent's identity, assembles every prompt, calls the LLM on every agent's behalf, and manages all budgets and schedules centrally. This creates a single trust domain — all agents share everything through the Fabric.

This works for teams of agents within a single project. It breaks when:

- Agents handle data from different compliance regimes
- Budget enforcement needs to be tamper-proof
- Agents need to grow their memory systems independently without cross-contamination
- The Fabric itself becomes a target for tampering

## Target Architecture

```
CURRENT                              TARGET
────────                             ──────

┌──────────────────┐                 ┌──────────────┐
│     Fabric       │                 │   Fabric     │
│                  │                 │              │
│ - Reads identity │                 │ - Scheduler  │
│ - Builds context │                 │ - Lifecycle  │
│ - Calls LLM      │    ────►       │ - Routing    │
│ - Manages budget │                 │              │
│ - Manages memory │                 │  (never sees │
│ - Routes messages│                 │  agent data) │
└──────────────────┘                 └──────┬───────┘
        │                                   │
   ┌────┴────┐                     ┌────────┼────────┐
   │ Agent-1 │                     │        │        │
   │ Agent-2 │              ┌──────┴──┐ ┌───┴────┐ ┌─┴──────┐
   │ Agent-3 │              │ Agent-1 │ │ Agent-2│ │ Agent-3│
   └─────────┘              │         │ │        │ │        │
   (just data)              │ Memory  │ │ Memory │ │ Memory │
                            │ Context │ │ Context│ │ Context│
                            │ LLM     │ │ LLM    │ │ LLM    │
                            │ Budget  │ │ Budget │ │ Budget │
                            └────┬────┘ └───┬────┘ └───┬────┘
                                 │          │          │
                            ┌────┴──────────┴──────────┴────┐
                            │    Channel Adapters            │
                            │  (Slack, internal chat, etc.)  │
                            └────────────────────────────────┘
```

## Work Items

### 1. Agent-Owned Cognitive Loop

**Status:** Not started
**Complexity:** Large — this is the core architectural change

Move the following from Fabric-level to agent-level:

- `ContextBuilder` — context assembly from identity + memories + task
- `FamiliarityEngine` — memory search for familiarity signals
- `LivingSummaryRegenerator` — end-of-day identity regeneration
- `_execute_task()` flow — routine assessment, consciousness invocation, reflection parsing

The Fabric becomes a lifecycle manager:

- `wake(agent_id)` — sends a wake signal to the agent's runtime
- `sleep(agent_id)` — sends a sleep signal
- `heartbeat()` — checks schedules, sends lifecycle signals
- Does **not** read agent identity, build prompts, or call LLM APIs

Each agent runs its own cognitive loop (plan-execute-replan-reflect) inside its isolation boundary. In Tier 3, this means inside the container. In Tier 1-2, this means in a separate async context with enforced boundaries.

**Key design decision:** The agent runtime needs its own memory adapter instance, consciousness adapter instance, and budget tracker. These are configured per-agent or per-deployment, not shared through the Fabric.

### 2. Per-Agent Memory Stores

**Status:** Not started
**Complexity:** Medium
**Depends on:** Item 1

Currently all agents share a single memory adapter instance, scoped by `agent_id` parameter. The `GuardedMemoryAdapter` blocks cross-agent queries but the underlying store is shared.

Change to per-agent memory adapter instances:

```yaml
agents:
  dev-cortiva:
    memory:
      adapter: neo4j
      config:
        uri: bolt://localhost:7687
        database: dev_cortiva

  qa-cortiva:
    memory:
      adapter: neo4j
      config:
        uri: bolt://localhost:7687
        database: qa_cortiva
```

This provides physical memory isolation — the healthcare agent's memories are in a different database than the banking agent's. Memory growth is fully independent.

### 3. Agent-Side Budget Enforcement

**Status:** Not started
**Complexity:** Medium
**Depends on:** Item 1

Add a `contract.yaml` to the agent's identity directory:

```yaml
# agents/dev-cortiva/identity/contract.yaml
budget:
  daily_calls: 50
  daily_tokens: 200000
  fallback_chain: [api, local]

schedule:
  wake: "09:00 mon-fri"
  sleep: "17:00"
  max_hours_per_day: 8

authority:
  may_refuse_out_of_hours: true
  may_refuse_over_budget: true
```

The agent reads its own contract and enforces it:

- Refuses to `think()` if daily calls are exhausted
- Refuses to wake if outside scheduled hours
- Logs refusals to its journal (inside its isolation boundary)

The Fabric cannot override these limits because:
- In Tier 2+, the agent's identity directory can be mounted read-only for the Fabric
- In Tier 3, the contract lives inside the container
- The budget counter lives in the agent's runtime, not in the Fabric's memory

**Note:** Agent-side enforcement is trust-based within the agent's own process. For hard enforcement against a compromised agent process, use a budget proxy (Item 4).

### 4. Budget Proxy Consciousness Adapter

**Status:** Not started
**Complexity:** Medium
**Depends on:** None (can be built independently)

A drop-in `ConsciousnessAdapter` that enforces hard budget limits externally:

```
Agent → BudgetProxyAdapter → Budget Proxy Service → Real LLM API
                                    │
                              Holds real API key
                              Enforces hard limits
                              Per-agent tokens
                              Audit logging
```

The agent never sees the real API key. It authenticates to the proxy with a per-agent token. The proxy:

- Counts calls and tokens per agent
- Hard-refuses when budget is exhausted (no override possible)
- Logs all requests for audit
- Rotates per-agent tokens on schedule

Configuration:

```yaml
consciousness:
  provider: budget-proxy
  config:
    proxy_url: "https://internal-proxy.corp/v1"
    agent_token_env: "CORTIVA_PROXY_TOKEN"
```

This plugs in via the existing adapter registry — no Fabric changes needed. The adapter protocol already passes `agent_id`, `priority`, and returns `tokens_in`/`tokens_out`, so the proxy has all the information it needs.

### 5. Per-Agent Credentials

**Status:** Not started
**Complexity:** Medium
**Depends on:** Item 1

Each agent gets its own API credentials, managed by an external secret store:

```yaml
agents:
  dev-cortiva:
    consciousness:
      provider: anthropic
      api_key_env: "DEV_CORTIVA_API_KEY"

  qa-cortiva:
    consciousness:
      provider: openai
      api_key_env: "QA_CORTIVA_API_KEY"
```

Or via a secrets manager adapter:

```yaml
secrets:
  adapter: vault
  config:
    url: "https://vault.corp:8200"
    path: "secret/cortiva/agents"
```

This ensures agent prompts (which contain sensitive context) are billed to separate accounts and are distinguishable in API-level audit logs.

### 6. Internal Channel Adapter ("Walk Up to the Desk")

**Status:** Not started
**Complexity:** Medium
**Depends on:** Item 1

Currently all inter-agent communication goes through Slack. This works but has latency and requires an external service.

Add an internal channel adapter for agents within the same Fabric:

```yaml
channel:
  adapter: internal    # or slack, discord, etc.
```

The metaphor: walking up to a colleague's desk to ask a question. The message goes through the same `ChannelAdapter` protocol — `send()`, `receive()`, `listen()` — but routes locally instead of through Slack's API.

Key constraint: **messages still flow through the channel adapter interface**, not through shared memory. Agent-1 cannot read Agent-2's identity files or memory. It can only send a message and receive a response. The channel adapter is the isolation boundary.

Implementation options:
- In-process message queue (for Tier 0-2)
- Unix socket per agent (for Tier 2)
- Container-to-container networking (for Tier 3)

### 7. Signed Lifecycle Commands

**Status:** Not started
**Complexity:** Small
**Depends on:** Item 3

The Fabric signs wake/sleep commands with a deployment key. Agents verify the signature before obeying. This prevents a compromised Fabric from issuing unauthorized lifecycle commands.

```
Fabric signs: { "command": "wake", "agent": "dev-cortiva", "timestamp": "..." }
Agent verifies signature against known public key before executing
```

This is a small addition once agents own their own lifecycle processing (Item 3).

### 8. Tamper-Evident Audit Log

**Status:** Not started
**Complexity:** Small
**Depends on:** None

Extend the event bus to produce a hash-chained audit log:

- Each event includes the hash of the previous event
- The log is append-only (new file per day, old files are immutable)
- Optional forwarding to an external SIEM via syslog or webhook

This doesn't prevent tampering by a compromised Fabric, but it makes tampering detectable after the fact.

## Implementation Order

The recommended order balances value and dependency:

```
Phase 1 (independent, high value):
  4. Budget Proxy Adapter        — drop-in, no Fabric changes
  8. Tamper-Evident Audit Log    — small, no dependencies

Phase 2 (core refactor):
  1. Agent-Owned Cognitive Loop  — the big change
  2. Per-Agent Memory Stores     — follows naturally from Item 1

Phase 3 (builds on refactor):
  3. Agent-Side Budget Enforcement
  5. Per-Agent Credentials
  7. Signed Lifecycle Commands

Phase 4 (communication):
  6. Internal Channel Adapter
```

Items 4 and 8 can be built today without any architectural changes. Item 1 is the gating refactor — everything else in Phases 2-4 depends on it.

## Design Principles

1. **Agents are employees, not functions.** They have contracts, working hours, budgets, and the right to refuse unreasonable requests.

2. **The Fabric is a building manager, not a boss.** It controls the lights and the schedule, not what happens at the desk.

3. **Communication goes through channels, not shared memory.** Agents talk to each other via Slack, internal chat, or future channels — never by reading each other's files or memory stores.

4. **Budgets are enforced by someone other than the spender.** Agent-side enforcement is the first line of defence. A budget proxy is the hard limit.

5. **Every adapter is a plugin.** Memory, consciousness, channels, budget enforcement — all swappable via config. The architecture should never require a specific implementation.
