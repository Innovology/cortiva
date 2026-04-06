# Session Management

Sessions track conversation history within a single agent wake cycle. Each time an agent wakes, a session is created. As the agent plans, executes, and reflects, turns accumulate in the session. The session is injected into the LLM context so the conscious layer has continuity across turns. When the agent sleeps, the session ends.

## What Is a Session

A session is a bounded conversation buffer tied to one agent and one wake cycle. It holds an ordered list of turns -- each turn records a role (agent, system, or user), a lifecycle phase (plan, execute, reflect, replan), and the content of that interaction.

Sessions solve two problems:

1. **Continuity** -- Without session history, each LLM call would start from scratch. The session gives the conscious layer memory of what happened earlier in the same wake cycle.
2. **Bounded growth** -- Without limits, conversation history would grow until it exceeded the model's context window. The rolling buffer evicts old turns automatically.

## How Turns Accumulate

Every interaction during a wake cycle adds a turn to the session:

- **Plan phase** -- The conscious layer produces a plan. That plan is recorded as a turn.
- **Execute phase** -- Each task execution adds turns: the task prompt and the agent's response.
- **Replan phase** -- If the plan is adjusted mid-cycle, the replan output is recorded.
- **Reflect phase** -- The end-of-day reflection adds a final turn before sleep.

```python
from cortiva.core.session import Session

session = Session(agent_id="dev-cortiva")
session.add_turn("system", "plan", "Build your plan for today.")
session.add_turn("agent", "plan", "- [ ] Implement feature X\n- [ ] Write tests")
session.add_turn("system", "execute", "Execute task: Implement feature X")
session.add_turn("agent", "execute", "Feature X implemented in src/feature.py")
```

Each turn automatically estimates its token count (1 token per 4 characters). You can also provide an explicit estimate:

```python
from cortiva.core.session import Turn

turn = Turn(role="agent", phase="execute", content="...", token_estimate=350)
```

## Rolling Buffer with Eviction

Sessions enforce two limits to prevent unbounded context growth:

| Limit | Default | Description |
|---|---|---|
| `max_turns` | 50 | Maximum number of turns retained |
| `max_tokens` | 32,000 | Maximum estimated tokens across all turns |

When either limit is exceeded, the oldest turns are evicted first. At least one turn is always retained, even if it alone exceeds the token budget.

```python
session = Session(agent_id="dev-cortiva", max_turns=20, max_tokens=16_000)
```

Eviction happens automatically on every `add_turn` call. You do not need to manage it manually.

### Eviction order

1. If `turn_count > max_turns`, drop the oldest turns until the count is within limit.
2. If `total_tokens > max_tokens`, drop the oldest turns until the total is within budget (keeping at least one turn).

This means recent context is always preserved at the expense of older history.

## Context Injection into LLM Prompts

Call `to_context_string()` to render the session as a markdown block suitable for inclusion in an LLM prompt:

```python
context_block = session.to_context_string()
```

This produces output like:

```markdown
## Conversation History

[system/plan] Build your plan for today.

[agent/plan] - [ ] Implement feature X
- [ ] Write tests

[system/execute] Execute task: Implement feature X

[agent/execute] Feature X implemented in src/feature.py
```

The context builder can include this block alongside identity files, memories, and other context sections when assembling the full prompt for the conscious layer.

## Cross-Contamination Guard

Each session is bound to a specific agent ID. The `validate_agent` method prevents one agent's session from accidentally being used for another agent:

```python
session = Session(agent_id="dev-cortiva")

session.validate_agent("dev-cortiva")   # OK
session.validate_agent("qa-cortiva")    # Raises ValueError
```

The error message explicitly names both the session owner and the requesting agent:

```
Session belongs to agent 'dev-cortiva', not 'qa-cortiva'.
Context cross-contamination prevented.
```

The `SessionManager.add_turn` method calls `validate_agent` internally, so cross-contamination is caught automatically when using the manager.

## The SessionManager API

`SessionManager` is the top-level interface for session lifecycle. It manages one active session per agent.

### Creating a manager

```python
from cortiva.core.session import SessionManager

manager = SessionManager(default_max_turns=50, default_max_tokens=32_000)
```

### Starting a session

```python
session = manager.start_session("dev-cortiva")
```

If a session already exists for that agent, it is replaced. This is the expected behavior on wake -- any stale session from a previous cycle is discarded.

### Adding turns

```python
turn = manager.add_turn("dev-cortiva", "agent", "execute", "Completed task 1")
```

Returns `None` if no session exists for the agent.

### Getting a session

```python
session = manager.get_session("dev-cortiva")
if session is not None:
    print(session.turn_count)
```

### Ending a session

```python
ended = manager.end_session("dev-cortiva")
```

Marks the session as ended (sets `ended_at`) and removes it from the manager. Returns `None` if no session exists.

### Listing active sessions

```python
agent_ids = manager.active_sessions  # ["dev-cortiva", "qa-cortiva"]
```

## Sessions in the Wake/Sleep Lifecycle

Sessions are tied to the wake/sleep cycle:

1. **Wake** -- The fabric calls `manager.start_session(agent_id)` when an agent transitions to the waking state. This creates a fresh session.
2. **Plan / Execute / Replan** -- Each LLM interaction adds turns via `manager.add_turn(...)`.
3. **Reflect** -- The reflection phase adds its turns, then the fabric calls `manager.end_session(agent_id)`.
4. **Sleep** -- The session is gone. Identity and journal entries persist on disk, but the ephemeral conversation history does not survive across sleep cycles.

This design is intentional. Long-term memory belongs in the memory adapter (InMemory, Engram, Neo4j). Sessions are for short-term, intra-cycle continuity only.
