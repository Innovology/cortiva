# Observability

Cortiva provides built-in monitoring for agent activity, resource consumption, and system health. All monitoring flows through the event bus, which means the same data is available via the CLI, the portal WebSocket API, and persistent log files.

## cortiva watch

The `watch` command streams live events from the running Fabric:

```bash
# Watch all events
cortiva watch

# Filter by event type
cortiva watch --filter agent.*
cortiva watch --filter task.*
cortiva watch --filter delegation.*

# Filter by agent
cortiva watch --agent dev-cortiva

# Filter by department
cortiva watch --department engineering

# Combine filters
cortiva watch --agent dev-cortiva --filter task.*
```

Output is one event per line, formatted for terminal readability:

```
09:01:12 agent.wake        dev-cortiva    Woke up, loading identity
09:01:14 agent.plan        dev-cortiva    Planning 5 tasks for today
09:01:15 task.started      dev-cortiva    Implement user settings page
09:03:42 task.completed    dev-cortiva    Implement user settings page (conscious, 12 calls)
09:03:43 task.started      dev-cortiva    Write tests for settings page
09:05:10 task.completed    dev-cortiva    Write tests for settings page (procedural, 0 calls)
```

The watch command connects to the Fabric daemon over the IPC socket and subscribes to the event bus with the specified filters. It stays connected until interrupted with Ctrl+C.

## cortiva capacity

Shows current resource utilisation across all agents:

```bash
cortiva capacity
```

Output:

```
Agent           Budget   Used   Remaining   Tasks   Status
dev-cortiva     50       23     27          3/5     awake
qa-cortiva      50       8      42          1/3     awake
pm-cortiva      50       31     19          4/6     awake
---
Total           150      62     88
```

Fields:

| Column | Description |
|--------|-------------|
| Budget | Daily consciousness call allocation for this agent. |
| Used | Consciousness calls consumed today. |
| Remaining | Calls remaining before the agent hits its daily limit. |
| Tasks | Completed tasks / total planned tasks for today. |
| Status | Current lifecycle state (`awake`, `sleeping`, `planning`, `reflecting`). |

Use `--json` for machine-readable output:

```bash
cortiva capacity --json
```

## cortiva agent activity

Shows recent activity for a specific agent:

```bash
cortiva agent activity dev-cortiva
```

Output:

```
Time     Type                  Details
09:01    agent.wake            Loaded identity, 12 memories recalled
09:01    agent.plan            5 tasks planned
09:01    task.started          Implement user settings page
09:03    task.completed        Implement user settings page (conscious)
09:03    task.started          Write tests for settings page
09:05    task.completed        Write tests for settings page (procedural)
09:05    task.started          Review PR #42
```

Options:

```bash
cortiva agent activity dev-cortiva --limit 50     # Show more events (default: 20)
cortiva agent activity dev-cortiva --since 08:00   # Events after a time
cortiva agent activity dev-cortiva --json          # Machine-readable output
```

## cortiva agent hours

Shows working hours for an agent over a time period:

```bash
cortiva agent hours dev-cortiva
```

Output:

```
Date         Wake     Sleep    Hours   Tasks   Calls
2025-06-10   09:00    17:02    8.0     12      47
2025-06-09   09:01    17:00    8.0     10      38
2025-06-08   09:00    16:58    7.9     11      42
---
Week total                     23.9    33      127
```

Options:

```bash
cortiva agent hours dev-cortiva --days 30     # Last 30 days (default: 7)
cortiva agent hours dev-cortiva --week        # Current week
cortiva agent hours dev-cortiva --month       # Current month
```

## Timesheet Tracking

Cortiva tracks working hours for every agent. The timesheet records when each agent wakes and sleeps, how many tasks it completed, and how many consciousness calls it consumed.

Timesheet data is stored in the agent's workspace at `today/timesheet.json`:

```json
{
  "date": "2025-06-10",
  "wake_time": "2025-06-10T09:00:00Z",
  "sleep_time": null,
  "tasks_completed": 7,
  "tasks_planned": 12,
  "consciousness_calls": 31,
  "consciousness_tokens_in": 45000,
  "consciousness_tokens_out": 12000,
  "exceptions": 1,
  "replans": 1
}
```

The timesheet is updated throughout the day as events occur. When the agent sleeps, `sleep_time` is set and the record is finalized. Historical timesheets are archived in `journal/timesheets/YYYY-MM-DD.json`.

### Daily Reset

Timesheets reset at the start of each day. When an agent wakes, a fresh timesheet is created for the current date. The previous day's timesheet is moved to the archive directory. This reset also applies to the consciousness budget -- each agent's daily call count returns to zero.

### Contention Metrics

When multiple agents compete for shared resources (consciousness budget, channel bandwidth, terminal adapter slots), contention events are recorded:

| Metric | Description |
|--------|-------------|
| `budget_contention` | Number of times an agent was denied a consciousness call because the global budget was exhausted. |
| `terminal_contention` | Number of times an agent waited for a terminal adapter slot. |
| `channel_contention` | Number of times a message send was delayed due to rate limiting. |

Contention metrics are included in the timesheet and visible in `cortiva capacity --verbose`:

```bash
cortiva capacity --verbose
```

```
Agent           Budget   Used   Remaining   Contention
dev-cortiva     50       23     27          budget:0 terminal:2 channel:0
qa-cortiva      50       8      42          budget:0 terminal:0 channel:0
pm-cortiva      50       31     19          budget:3 terminal:1 channel:0
```

High contention numbers indicate resource pressure. Consider increasing the global budget, adding terminal adapter capacity, or adjusting agent schedules to reduce overlap.

## Event Bus

All monitoring data flows through the event bus (`EventBus`). The bus supports:

- **Subscriptions with filtering**: Subscribe to specific event types, agent IDs, or departments.
- **In-memory ring buffer**: Recent events are kept in memory for fast retrieval (default: 1000 events).
- **Persistent log**: Events are appended to a JSON Lines file for historical analysis.

The portal WebSocket API connects to the same event bus, so the web dashboard shows the same data as the CLI.

### Event Types

See the full list of standard event types in the source at `src/cortiva/core/events.py`. The main categories:

- `agent.*` -- Lifecycle events (wake, sleep, plan, reflect)
- `task.*` -- Task execution events (started, completed, failed)
- `budget.*` -- Budget events (warning, exhausted, recharged)
- `delegation.*` -- Delegation events (created, accepted, completed)
- `policy.*` -- Policy enforcement events (denied, approved)
- `cluster.*` -- Cluster events (node joined, agent moved)
- `snapshot.*` -- Snapshot events (created, restored)
- `promotion.*` -- Promotion events (initiated, confirmed, reverted)

## Persistent Event Log

The event bus writes to a JSON Lines file at `.cortiva/events.jsonl` by default. Each line is a JSON object:

```json
{"event_id":"a1b2c3d4e5f6","event_type":"task.completed","agent_id":"dev-cortiva","timestamp":1718020622.5,"data":{"task":"Implement settings page","mode":"conscious","calls":12},"department":"engineering"}
```

This file is append-only and rolls over daily. Use standard JSON Lines tools (`jq`, `grep`) for ad-hoc analysis:

```bash
# Count tasks completed by each agent today
cat .cortiva/events.jsonl | jq -r 'select(.event_type == "task.completed") | .agent_id' | sort | uniq -c

# Find all budget warnings
cat .cortiva/events.jsonl | jq 'select(.event_type == "budget.warning")'
```
