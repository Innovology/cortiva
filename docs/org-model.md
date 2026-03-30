# Org Model

Cortiva models agent teams as an organisation with departments, reporting lines, and roles. The org model defines who reports to whom, which agents belong to which department, and how authority flows through the hierarchy.

## Concepts

### Departments

A department groups agents that share a function. Each department has a head (the manager) and one or more members. Departments are defined in `cortiva.yaml`:

```yaml
org:
  departments:
    engineering:
      head: pm-cortiva
      members:
        - dev-cortiva
        - qa-cortiva
    operations:
      head: ops-lead
      members:
        - infra-01
        - monitoring-01
```

The department name is a free-form string. Use whatever maps to your team structure.

### Roles

Every agent has a role that determines its authority level within the organisation:

| Role | Description |
|------|-------------|
| `individual` | Default. Executes assigned work. Cannot delegate. |
| `lead` | Can delegate work to members of their department. |
| `head` | Department head. Approves secondary actions. Can delegate across the department. |
| `director` | Cross-department authority. Can delegate to any agent. |

Roles are assigned per agent in the org config:

```yaml
org:
  roles:
    pm-cortiva: head
    dev-cortiva: individual
    qa-cortiva: individual
    ops-lead: lead
```

Agents without an explicit role default to `individual`.

### Reporting Lines

Reporting lines are inferred from the department structure. An agent's manager is the head of their department. If no department is configured, the agent has no manager and operates independently.

The reporting line determines:

- Who receives escalation messages from the agent
- Who can approve secondary actions (see [Policies](policies.md))
- Who can delegate work to the agent (see [Delegation](delegation.md))

## Config Schema

The full `org` section in `cortiva.yaml`:

```yaml
org:
  departments:
    <department-name>:
      head: <agent-id>        # Required. Must be a registered agent.
      members:                 # Required. List of agent IDs.
        - <agent-id>
        - <agent-id>

  roles:
    <agent-id>: <role>         # individual | lead | head | director
```

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `departments` | object | No | Map of department name to department config. |
| `departments.<name>.head` | string | Yes | Agent ID of the department head. |
| `departments.<name>.members` | list | Yes | Agent IDs of department members. |
| `roles` | object | No | Map of agent ID to role. Defaults to `individual`. |

## How Org Context Is Injected

When the Fabric builds context for an agent's conscious layer, it includes org metadata:

- The agent's department and role
- Its manager (department head)
- Its peers (other department members)
- Its direct reports (if the agent is a head or lead)

This context is injected into the planning prompt so the agent understands its position in the organisation. An agent that knows it reports to `pm-cortiva` will naturally escalate blockers to that agent rather than attempting to resolve them independently.

Example context block injected into a planning prompt:

```
## Organisation
Department: engineering
Role: individual
Manager: pm-cortiva
Peers: qa-cortiva
```

## How Delegation Authority Works

The org model defines who can delegate to whom. The rules are:

1. A `head` can delegate to any member of their department.
2. A `lead` can delegate to members of their department.
3. A `director` can delegate to any agent.
4. An `individual` cannot delegate.

Delegation authority is checked before any assignment is created. If an agent attempts to delegate work to someone outside their authority, the assignment is rejected and an escalation event is emitted.

See [Delegation](delegation.md) for the full delegation workflow.

## Example

A typical three-agent bootstrap team:

```yaml
org:
  departments:
    product:
      head: pm-cortiva
      members:
        - dev-cortiva
        - qa-cortiva

  roles:
    pm-cortiva: head
    dev-cortiva: individual
    qa-cortiva: individual
```

In this setup:

- `pm-cortiva` is the department head. It can delegate work to `dev-cortiva` and `qa-cortiva`, approve their secondary actions, and receive their escalations.
- `dev-cortiva` and `qa-cortiva` are peers. They cannot delegate to each other but can communicate via channels.
- If `dev-cortiva` encounters a scope change (an escalation topic in its `responsibilities.md`), it escalates to `pm-cortiva`.
