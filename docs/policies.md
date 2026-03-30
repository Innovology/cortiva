# Execution Policies

Execution policies control what agents are allowed to do at runtime. They define tool permissions, action approvals, and filesystem restrictions. Policies are enforced by the Fabric before any action reaches the terminal adapter or consciousness layer.

## Concepts

### Tool Permissions

Tool permissions control which tools an agent can invoke. By default, all tools available to an agent (from installed skills and the terminal adapter) are permitted. Policies can restrict this.

```yaml
policies:
  tool_permissions:
    dev-cortiva:
      allow:
        - web_search
        - file_read
        - file_write
        - shell_exec
      deny:
        - deploy_production
        - database_drop
    qa-cortiva:
      allow:
        - web_search
        - file_read
        - shell_exec
      deny:
        - file_write     # QA reads code, does not modify it
```

Rules:

- If `allow` is specified, only listed tools are permitted. All others are denied.
- If only `deny` is specified, all tools except listed ones are permitted.
- If both are specified, `deny` takes precedence over `allow`.
- If neither is specified, all tools are permitted (default).

### Action Approvals

Some actions require approval before execution. This works with the governance system described in [Isolation](isolation.md) -- when the `AuthorityValidator` classifies an action as `secondary`, an approval request is generated.

Policies define who can approve and how:

```yaml
policies:
  approvals:
    dev-cortiva:
      approver: pm-cortiva
      auto_approve:
        - priority: low
          max_cost: 10       # consciousness calls
      require_approval:
        - tool: deploy_staging
        - tool: shell_exec
          args_match: "rm -rf*"
```

The approval workflow:

1. Agent proposes an action that requires approval.
2. The Fabric creates an approval request and sends it to the configured approver via the channel adapter.
3. The approver (another agent or a human) reviews and approves or rejects.
4. If approved, the action proceeds. If rejected, the agent receives the rejection reason and replans.

Auto-approve rules allow low-risk actions to proceed without human intervention. The example above auto-approves actions with `low` priority that cost fewer than 10 consciousness calls.

### Filesystem Restrictions

Filesystem policies extend the isolation system with path-level rules:

```yaml
policies:
  filesystem:
    dev-cortiva:
      writable:
        - "agents/dev-cortiva/**"
        - "src/**"
        - "tests/**"
      readable:
        - "**"               # Can read everything
      denied:
        - ".env"
        - "secrets/**"
        - "agents/*/identity/contract.yaml"
    qa-cortiva:
      writable:
        - "agents/qa-cortiva/**"
        - "tests/**"
      readable:
        - "src/**"
        - "tests/**"
      denied:
        - ".env"
        - "secrets/**"
```

Rules use glob patterns. The evaluation order is:

1. Check `denied` -- if the path matches, block the operation.
2. For write operations, check `writable` -- if the path does not match, block.
3. For read operations, check `readable` -- if the path does not match, block.
4. If no filesystem policy is configured, fall back to the isolation tier's default behavior.

## Config Schema

The full `policies` section in `cortiva.yaml`:

```yaml
policies:
  tool_permissions:
    <agent-id>:
      allow:
        - <tool-name>
      deny:
        - <tool-name>

  approvals:
    <agent-id>:
      approver: <agent-id>     # Who approves this agent's actions
      auto_approve:
        - priority: <low|normal|high>
          max_cost: <int>       # Max consciousness calls
      require_approval:
        - tool: <tool-name>
          args_match: <glob>    # Optional pattern for tool arguments

  filesystem:
    <agent-id>:
      writable:
        - <glob-pattern>
      readable:
        - <glob-pattern>
      denied:
        - <glob-pattern>
```

## How Policies Are Enforced

Policies are loaded at Fabric startup and checked at two points:

1. **Before tool invocation**: When the cognitive loop or terminal adapter is about to invoke a tool, the Fabric checks `tool_permissions` and `filesystem` policies. If the tool or path is denied, the invocation is blocked and the agent receives an error.

2. **Before action execution**: When the `AuthorityValidator` classifies an action as `secondary`, the Fabric checks `approvals` policies. If an approver is configured, the approval workflow begins. If no approver is configured, secondary actions are blocked by default.

Policy violations are logged as events (`policy.tool_denied`, `policy.path_denied`, `policy.approval_required`) and appear in `cortiva watch` output.

## Approval Workflow

The full approval lifecycle:

```
Agent proposes action
    |
    v
AuthorityValidator classifies as SECONDARY
    |
    v
Policy engine checks auto_approve rules
    |
    +-- Matches auto_approve --> Action proceeds
    |
    +-- No match --> Approval request created
                        |
                        v
                    Request sent to approver via channel
                        |
                        +-- Approved --> Action proceeds
                        |
                        +-- Rejected --> Agent receives reason, replans
                        |
                        +-- Timeout --> Action blocked, logged
```

Approval requests expire after a configurable timeout (default: 30 minutes). Expired requests are treated as rejections.

## Interaction with Isolation

Policies layer on top of the isolation tier. Isolation provides the enforcement mechanism (path traversal prevention, memory guards, container boundaries). Policies provide the rules (which paths, which tools, which approvals).

- **Tier 0 (none)**: Policies are advisory. Tool permission checks run but filesystem restrictions rely on the agent's cooperation.
- **Tier 1+ (soft and above)**: Filesystem restrictions are enforced by the path validation in `SoftIsolation`. Policies add granular per-agent path rules on top of the base isolation checks.
- **Tier 3 (container)**: Filesystem restrictions are enforced at the container level (volume mounts). Policies provide additional in-process checks before the container is invoked.

## Events

| Event Type | When |
|------------|------|
| `policy.tool_denied` | Agent attempted to use a denied tool. |
| `policy.path_denied` | Agent attempted to access a denied path. |
| `policy.approval_required` | Action requires approval. Request sent to approver. |
| `policy.approved` | Approver approved the action. |
| `policy.rejected` | Approver rejected the action. |
| `policy.timeout` | Approval request expired. |
