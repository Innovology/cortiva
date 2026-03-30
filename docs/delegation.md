# Delegation

Delegation is how managers assign work to agents. A manager creates an assignment, the target agent receives it, executes it, and reflects on the outcome. The reflection suffix protocol ensures the manager gets structured feedback.

## How It Works

### Creating an Assignment

A manager (any agent with `lead`, `head`, or `director` role) creates an assignment through the delegation system. Assignments can be created programmatically by the manager's cognitive loop or via the CLI.

An assignment contains:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Auto-generated unique identifier. |
| `from_agent` | string | Agent ID of the delegator. |
| `to_agent` | string | Agent ID of the assignee. |
| `description` | string | What needs to be done. |
| `priority` | string | `low`, `normal`, `high`, `urgent`. Default: `normal`. |
| `deadline` | string | Optional. ISO 8601 timestamp. |
| `context` | string | Optional. Additional context or references. |
| `status` | string | `pending`, `accepted`, `in_progress`, `completed`, `rejected`. |

### Receiving an Assignment

When an assignment is created, it appears in the target agent's task queue (`today/task_queue.json`). The next time the agent replans, it incorporates the assignment into its plan based on priority.

An agent can accept or reject an assignment:

- **Accept**: The assignment moves to `accepted`, then `in_progress` when execution begins.
- **Reject**: The assignment moves to `rejected` with a reason. The delegator is notified via the channel adapter and can reassign or escalate.

Agents reject assignments when:

- The work falls outside their `responsibilities.md` boundaries
- They are over capacity (budget exhausted, too many pending tasks)
- The deadline is not achievable given their current workload

### Completing an Assignment

When an agent finishes an assignment, it writes a reflection suffix (see below) and marks the assignment as `completed`. The delegator receives a completion notification via the channel adapter.

## The Reflection Suffix Protocol

Every completed assignment includes a structured reflection appended to the agent's normal task reflection. This gives the delegator machine-readable feedback on the outcome.

The suffix format:

```
## Assignment Reflection
- **Assignment**: <assignment-id>
- **Delegated by**: <from-agent>
- **Outcome**: completed | partial | blocked
- **Summary**: One-line summary of what was done.
- **Blockers**: None | Description of what blocked progress.
- **Time spent**: Estimated consciousness calls used.
- **Learnings**: What the agent learned from this task.
```

This suffix is:

1. Written to the agent's journal as part of the daily reflection.
2. Stored in the agent's memory for familiarity matching on future similar tasks.
3. Sent to the delegator as part of the completion notification.

The delegator's cognitive loop reads these reflections to assess whether assignments are being completed effectively, which informs future delegation decisions.

## Authority Checks

Before an assignment is created, the delegation system verifies:

1. **Role check**: The delegator has a role that permits delegation (`lead`, `head`, or `director`).
2. **Scope check**: The delegator has authority over the target agent (same department for `lead`/`head`, any agent for `director`).
3. **Boundary check**: The assignment description is checked against the target agent's `responsibilities.md` to ensure it falls within their primary or secondary authority.

If any check fails, the assignment is rejected before delivery. The delegator receives an error with the specific check that failed.

## CLI Commands

```
cortiva delegate <from> <to> "<description>"       Create an assignment
cortiva delegate <from> <to> "<description>" --priority high
cortiva delegate <from> <to> "<description>" --deadline 2025-06-01T17:00:00
cortiva assignments <agent-id>                      List assignments for an agent
cortiva assignments <agent-id> --status pending     Filter by status
cortiva assignment <assignment-id>                  Show assignment details
```

Examples:

```bash
# PM assigns a feature to the dev agent
cortiva delegate pm-cortiva dev-cortiva "Implement the user settings page" --priority high

# Check what dev-cortiva is working on
cortiva assignments dev-cortiva --status in_progress

# View a specific assignment
cortiva assignment asg-3f8a2b
```

## Events

The delegation system emits events to the event bus:

| Event Type | When |
|------------|------|
| `delegation.created` | Assignment created and delivered to target agent. |
| `delegation.accepted` | Target agent accepted the assignment. |
| `delegation.rejected` | Target agent rejected the assignment. |
| `delegation.started` | Target agent began working on the assignment. |
| `delegation.completed` | Target agent completed the assignment. |
| `delegation.authority_denied` | Delegation authority check failed. |

Subscribe to these events via `cortiva watch --filter delegation.*` or the portal WebSocket API.
