# Agent Lifecycle and Termination

Cortiva agents are not disposable functions. They are persistent entities that sleep, wake, plan, work, reflect, and learn. This guide covers the full lifecycle state machine, how to manage agent states through the CLI, and how to formally retire an agent through termination.

## The Agent State Machine

Every agent is in exactly one state at any time. The valid states and transitions are:

```
onboarding --> sleeping <--> waking --> planning --> executing <--> replanning --> reflecting --> sleeping
```

### States

| State | Description |
|---|---|
| `onboarding` | First-time setup. The agent has just been created and has no experience. Transitions to `sleeping` once initial identity files are written. |
| `sleeping` | Idle. Identity persists on disk. The agent waits for a wake signal. |
| `waking` | Loading identity files, checking for pending messages, resetting the day's workspace. |
| `planning` | The conscious layer builds a plan for the day based on identity, memory, and messages. |
| `executing` | Working through the task queue. Tasks are routed by the subconscious; novel situations go to the conscious layer. |
| `replanning` | Adjusting the plan mid-cycle in response to exceptions, new messages, or changed conditions. |
| `reflecting` | End-of-day review. The conscious layer updates the Living Summary, writes a journal entry, and stores memories. |

### Transition Rules

Not every state can reach every other state. The valid transitions are:

| From | Allowed targets |
|---|---|
| `onboarding` | `sleeping` |
| `sleeping` | `waking` |
| `waking` | `planning`, `sleeping` |
| `planning` | `executing`, `sleeping` |
| `executing` | `replanning`, `reflecting`, `sleeping` |
| `replanning` | `executing`, `reflecting`, `sleeping` |
| `reflecting` | `sleeping` |

Attempting an invalid transition raises a `ValueError`. The `can_transition` method lets you check before committing:

```python
from cortiva.core.agent import Agent, AgentState

agent = Agent(id="dev-cortiva", directory=Path("agents/dev-cortiva"))

if agent.can_transition(AgentState.WAKING):
    agent.transition(AgentState.WAKING)
```

### What Happens on Wake

When an agent transitions to `waking`:

1. `last_wake` is set to the current time.
2. Runtime counters are reset: `consciousness_budget_used`, `tasks_completed_today`, `tasks_escalated_today`.
3. The `today/` directory is cleared for a fresh day cycle.
4. Identity files are loaded into context.
5. Pending messages are checked from the channel adapter.
6. The agent transitions to `planning` and the conscious layer builds the day's plan.
7. The agent transitions to `executing`.

### What Happens on Sleep

When an agent transitions to `sleeping`:

1. `last_sleep` is set to the current time.
2. If the agent was in `executing` or `replanning`, it first transitions through `reflecting`.
3. During reflection, the conscious layer reviews the day, updates the Living Summary, writes a journal entry, and stores memories.
4. The final plan state is persisted to disk.
5. The session (if managed) is ended and discarded.

## The Daily Cycle

A typical day looks like this:

```
sleeping -> waking -> planning -> executing -> reflecting -> sleeping
                                      |
                                      v
                                  replanning -> executing -> ...
```

1. **Wake** -- Load identity, check messages, reset workspace.
2. **Plan** -- The conscious layer creates a structured task checklist.
3. **Execute** -- Tasks are executed one by one. The subconscious assesses familiarity; routine tasks stay local, novel tasks go to the conscious layer.
4. **Replan** (optional) -- If exceptions accumulate or new inputs arrive, the plan is adjusted. Up to 3 replans per cycle.
5. **Reflect** -- End-of-day review produces a journal entry, updates the Living Summary, and stores key memories.
6. **Sleep** -- Identity persists. Ephemeral state (task queue, session) is discarded.

## Agent Termination

Termination is the formal retirement of an agent. It is a permanent, irreversible operation. Use it when an agent is no longer needed, is being replaced by a successor, or needs to be decommissioned.

### What Termination Does

The `terminate_agent` function performs these steps in order:

1. **Final snapshot** -- Creates a snapshot of the agent's entire directory, preserving its state at the moment of termination.
2. **Knowledge export** -- Extracts high-importance memories (procedures, skills, identity summary, recent journal entries) into a `knowledge_export.md` file.
3. **Termination record** -- Writes a `.terminated.json` file containing the agent ID, reason, timestamp, successor ID, snapshot ID, and whether knowledge was exported.
4. **Successor handover** -- If a successor is specified and exists, copies the agent's procedures and knowledge export to the successor's `identity/` directory.
5. **Archival** -- Moves the agent directory from `agents/` to `agents/.archive/`. The agent is no longer active.

### Using terminate_agent

```python
from pathlib import Path
from cortiva.core.lifecycle import terminate_agent

agent_dir = Path("agents/dev-cortiva")
record = terminate_agent(agent_dir, reason="retirement")

print(record.agent_id)          # "dev-cortiva"
print(record.snapshot_id)       # "snap-..."
print(record.knowledge_exported) # True
```

### Termination with a Successor

When you specify a `successor_id`, the framework copies key knowledge to the successor automatically:

```python
record = terminate_agent(
    agent_dir,
    reason="replaced by v2",
    successor_id="dev-cortiva-v2",
)
```

The successor receives two files in its `identity/` directory:

- `predecessor_dev-cortiva_procedures.md` -- The terminated agent's promoted procedures.
- `predecessor_dev-cortiva_knowledge.md` -- The full knowledge export.

If the successor directory does not exist, termination still succeeds -- the handover is skipped without error.

### The TerminationRecord

The `TerminationRecord` dataclass captures everything about a termination:

| Field | Type | Description |
|---|---|---|
| `agent_id` | `str` | ID of the terminated agent |
| `reason` | `str` | Why the agent was terminated |
| `terminated_at` | `str` | ISO 8601 timestamp |
| `successor_id` | `str \| None` | ID of the successor agent, if any |
| `snapshot_id` | `str` | ID of the final snapshot |
| `knowledge_exported` | `bool` | Whether a knowledge export was produced |

The record supports serialization:

```python
data = record.to_dict()                      # dict
restored = TerminationRecord.from_dict(data)  # TerminationRecord
```

### Checking Termination Status

```python
from cortiva.core.lifecycle import is_terminated, get_termination_record

# Returns True if the agent has been terminated (checks both active dir and archive)
if is_terminated(agent_dir):
    record = get_termination_record(agent_dir)
    print(f"Terminated: {record.reason}")
```

## CLI Commands

### Wake an agent

```bash
cortiva agent wake dev-cortiva
```

Sends a wake signal to the running fabric. The agent transitions through `waking -> planning -> executing`.

### Sleep an agent

```bash
cortiva agent sleep dev-cortiva
```

Triggers reflection and puts the agent to sleep. The agent transitions through `reflecting -> sleeping`.

### Check status

```bash
cortiva status
```

Shows the current state of all agents, including their lifecycle state, tasks completed, and consciousness budget usage.

### Promote an agent

```bash
cortiva agent promote dev-cortiva --to senior-dev --probation 14
```

Initiates a role promotion with a probation period. During probation, the agent operates under the new role template. After the probation period, the promotion can be confirmed or reverted.

### Manage probation

```bash
# Confirm the promotion
cortiva agent probation dev-cortiva --confirm

# Revert to the previous role
cortiva agent probation dev-cortiva --revert

# Extend the probation period
cortiva agent probation dev-cortiva --extend 7
```

### Terminate an agent

Termination is not yet exposed as a CLI command. Use the Python API directly:

```python
from pathlib import Path
from cortiva.core.lifecycle import terminate_agent

record = terminate_agent(Path("agents/dev-cortiva"), reason="retirement")
```

## Archival and Recovery

Terminated agents are moved to `agents/.archive/`. The archive preserves:

- All identity files
- The final snapshot (in `.snapshots/`)
- The termination record (`.terminated.json`)
- The knowledge export (`knowledge_export.md`)
- Journal entries

The archive is a read-only record. To recover a terminated agent, you would need to manually move its directory back from `.archive/` and remove the `.terminated.json` file.
