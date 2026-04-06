# OKR / Goals System

Cortiva includes a built-in OKR (Objectives and Key Results) system for tracking org-level goals. Goals are assigned to departments and agents, progress is tracked through measurable key results, and the current state is automatically injected into agent planning prompts.

## Concepts

An **Objective** is a high-level goal with a title, description, owner, optional department, quarter label, and status (`active`, `completed`, or `cancelled`).

A **Key Result** is a measurable outcome attached to an objective. Each key result has a target value, current value, unit, and optional agent assignment. Progress on the objective is the average completion ratio across its key results.

## Configuration

Goals are managed through `GoalManager`, which persists objectives as JSON in a data directory you specify:

```python
from cortiva.core.goals import GoalManager

gm = GoalManager(data_dir="./data/goals")
```

On construction, `GoalManager` loads any existing objectives from `{data_dir}/objectives.json`. The directory is created automatically on the first write.

In a typical Cortiva deployment, the data directory lives alongside your agent directories. You can also point it at any path you like.

## Creating Objectives

Use `create_objective` to define a new objective with its key results:

```python
from cortiva.core.goals import GoalManager, KeyResult

gm = GoalManager(data_dir="./data/goals")

obj = gm.create_objective(
    title="Improve uptime",
    description="Reach 99.9% SLA across all services",
    owner="sre-lead",
    department="engineering",
    quarter="2026-Q2",
    key_results=[
        KeyResult(
            id="kr-p1",
            description="Reduce P1 incidents",
            target_value=5.0,
            unit="incidents",
            agent_id="sre-01",
        ),
        KeyResult(
            id="kr-mttr",
            description="Reduce mean time to recovery",
            target_value=30.0,
            unit="minutes",
            agent_id="sre-02",
        ),
    ],
)
```

Each objective gets a unique auto-generated ID. The objective and its key results are persisted to disk immediately.

### Key Result Fields

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique identifier (you provide this) |
| `description` | `str` | What this key result measures |
| `target_value` | `float` | The target number to reach |
| `current_value` | `float` | Current progress (default `0.0`) |
| `unit` | `str` | Unit label (e.g. "incidents", "deals") |
| `agent_id` | `str` or `None` | Agent responsible for this key result |

## Tracking Progress

### Updating Key Results

Update progress on a key result by objective ID and key result ID:

```python
gm.update_key_result(obj.id, "kr-p1", current_value=3.0)
```

The updated value is persisted immediately.

### Computing Progress

The `progress` method returns the objective's overall completion as a float between 0.0 and 1.0. It is computed as the average completion ratio across all key results, where each ratio is capped at 1.0:

```python
pct = gm.progress(obj.id)
print(f"Objective is {pct:.0%} complete")
```

If a key result has a `target_value` of 0, it is treated as already met (ratio = 1.0). If the objective has no key results, progress returns 0.0.

## Filtering Objectives

Use `get_objectives` to list objectives with optional filters:

```python
# All objectives
all_objs = gm.get_objectives()

# Filter by quarter
q2 = gm.get_objectives(quarter="2026-Q2")

# Filter by department
eng = gm.get_objectives(department="engineering")

# Filter by agent (matches owner OR any key result assignment)
mine = gm.get_objectives(agent_id="sre-01")

# Combine filters
eng_q2 = gm.get_objectives(quarter="2026-Q2", department="engineering")
```

The `agent_id` filter matches objectives where the agent is either the objective owner or is assigned to at least one key result.

## Agent Context Injection

The `agent_goals_context` method renders an agent's relevant goals as markdown, suitable for injection into LLM planning prompts:

```python
context = gm.agent_goals_context("sre-01")
```

This returns a string like:

```markdown
## My OKR Goals

### Improve uptime (2026-Q2) -- 30%
_Reach 99.9% SLA across all services_

- [ ] Reduce P1 incidents: 3.0/5.0 incidents (@sre-01)
- [ ] Reduce mean time to recovery: 0.0/30.0 minutes (@sre-02)
```

If the agent has no associated objectives, an empty string is returned. This makes it safe to include unconditionally in prompt assembly -- it adds nothing when there are no goals.

## Persistence

All data is stored in a single `objectives.json` file inside the data directory you provide. The file is rewritten on every mutation (create, update). On construction, `GoalManager` reads from this file if it exists, so state survives process restarts.

```
data/goals/
  objectives.json
```

The directory is created automatically (including nested parents) on the first write.
