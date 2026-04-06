# Security Architecture

This document describes Cortiva's security model, the controls available at each layer, and how to deploy securely for production workloads. Read this before deploying agents that handle sensitive data.

## Security Model Overview

Cortiva has **seven security layers** that work together:

```
┌─────────────────────────────────────────────────┐
│  Layer 7: Data Boundary Enforcement             │
│  LLM endpoint allow/deny, telemetry separation  │
├─────────────────────────────────────────────────┤
│  Layer 6: Encryption at Rest                    │
│  AES-256-GCM for identity, journals, workspace  │
├─────────────────────────────────────────────────┤
│  Layer 5: Credential Delegation                 │
│  Azure Key Vault, Managed Identity, per-agent   │
├─────────────────────────────────────────────────┤
│  Layer 4: Execution Policies                    │
│  Tool permissions, action approvals, filesystem │
├─────────────────────────────────────────────────┤
│  Layer 3: Resource Guards                       │
│  Cycle timeout, disk quota, budget limits       │
├─────────────────────────────────────────────────┤
│  Layer 2: Agent Isolation (Tiers 0-3)           │
│  Path validation, memory guards, containers     │
├─────────────────────────────────────────────────┤
│  Layer 1: Tamper-Evident Audit Log              │
│  SHA-256 hash-chained, daily rotation           │
└─────────────────────────────────────────────────┘
```

## Trust Model

Cortiva uses a **centralised orchestrator** model. The Fabric process manages all agents within a single deployment.

### Trust Domains

All agents within a single Fabric share a **trust domain**. The Fabric reads agent identity files, queries memories, and sends LLM requests on behalf of agents. Isolation controls constrain terminal subprocesses and memory access patterns, but the Fabric orchestrator has unrestricted access by design.

For workloads requiring separate trust domains, deploy separate Fabric instances.

### Per-Agent Credential Isolation

Despite the shared trust domain, agents have **separate credentials** when credential delegation is configured. Agent A's Azure DevOps PAT is not available to Agent B, even though both run in the same Fabric. This is enforced by the `CredentialProvider` which resolves secrets per-agent from the configured secret store.

## Layer 1: Tamper-Evident Audit Log

Every significant action is logged to a SHA-256 hash-chained audit trail.

- Each entry includes the hash of the previous entry
- Tampering with any entry breaks the chain from that point forward
- Daily file rotation: `audit-YYYY-MM-DD.jsonl`
- Verification via `AuditLog.verify(date)` detects modifications
- Events logged: lifecycle transitions, task execution, policy decisions, hook arrivals, delegation, approvals, resource violations

```yaml
# No config needed — audit logging is always on when configured
# Place audit files in a customer-controlled location
```

**What it doesn't do**: The audit log is append-only but not externally signed. A compromised Fabric process could append false entries (but not modify existing ones without breaking the chain). For higher assurance, forward audit events to an external SIEM.

## Layer 2: Agent Isolation

Four tiers of isolation, each inheriting all protections from lower tiers.

### Tier 0: None

No enforcement. All agents share a process, filesystem, and memory.

### Tier 1: Soft

- Path traversal prevention (agents can't access other agents' directories)
- Filename validation (rejects `..`, `/`, `\` in path helpers)
- Cross-agent memory blocking via `GuardedMemoryAdapter`
- Terminal subprocess cwd locked to agent workspace
- Governance enforcement (authority boundaries from responsibilities.md)

### Tier 2: OS

All Tier 1 protections plus:

- Environment variable filtering via allowlist
- Per-agent TMPDIR
- Per-agent IPC socket paths
- Agent ID tagging in subprocess environment

### Tier 3: Container

All Tier 2 protections plus:

- Separate Docker/Podman container per agent terminal invocation
- CPU and memory limits per container
- Network mode control (`bridge` for API access, `none` for air-gapped)
- Shared memory sizing (`--shm-size` for browser-driving agents)
- Volume mounts restricted to agent directory only
- Non-root execution (UID 1000)
- Browser sidecar support (`BROWSER_WS_ENDPOINT` injection)

```yaml
isolation:
  tier: container
  container:
    runtime: docker
    cpu_limit: "1.0"
    memory_limit: "512m"
    shm_size: "256m"
    network: bridge
    image: python:3.13-slim
    browser_endpoint: "ws://browserless:3000"
```

## Layer 3: Resource Guards

Prevents any agent from starving others or overwhelming the host. Works at **all tiers**, not just container mode.

| Control | Default | Description |
|---------|---------|-------------|
| `cycle_timeout_s` | 120 | Max wall-clock seconds per cycle. Cancels stuck LLM calls. |
| `max_consciousness_calls_per_cycle` | 5 | Prevents runaway LLM loops. |
| `max_disk_mb` | 500 | Disk quota per agent. Checked before each cycle. |
| `max_cycles_per_heartbeat` | 1 | Prevents any agent from hogging the heartbeat. |
| `max_hours_per_day` | 12 | Forces agents to stop after this many hours. |

Agents that exceed limits are suspended. Per-agent overrides allow different limits for different roles.

```yaml
resource_limits:
  defaults:
    cycle_timeout_s: 120
    max_disk_mb: 500
    max_hours_per_day: 10
  dev-cortiva:
    cycle_timeout_s: 300
    max_disk_mb: 2000
```

## Layer 4: Execution Policies

Declarative YAML policies controlling what each agent can do.

### Tool Permissions

```yaml
policies:
  defaults:
    tools:
      allowed: [Read, Write, Edit, Glob, Grep]
      denied: [Bash]
  dev-cortiva:
    tools:
      denied: []  # dev gets Bash access
```

### Action Approvals

```yaml
policies:
  defaults:
    execution:
      auto_approve:
        - "write tests*"
        - "create branch*"
      require_approval:
        - "merge*"
        - "deploy*"
      deny:
        - "drop database*"
        - "rm -rf*"
```

Tasks matching `require_approval` enter an approval queue. The designated approver (from org reporting lines) is notified. Tasks matching `deny` are rejected immediately.

### Filesystem Restrictions

```yaml
policies:
  defaults:
    filesystem:
      workspace_only: true
      denied_paths: ["/etc*", "/var*"]
      allowed_paths: ["/shared/data*"]
```

## Layer 5: Credential Delegation

Agents access customer resources without Cortiva holding credentials.

### Azure Key Vault (Recommended for Production)

The node uses Azure Managed Identity to authenticate to the customer's Key Vault. No secrets are stored on disk or in environment variables.

```yaml
credentials:
  provider: azure-keyvault
  key_vault_url: https://contoso.vault.azure.net
  agents:
    dev-cortiva:
      AZURE_DEVOPS_PAT: secret/devops-pat
      GITHUB_TOKEN: secret/github-token
    pm-cortiva:
      LINEAR_API_KEY: secret/linear-key
```

### Azure Managed Identity

For direct Azure API access (Azure DevOps, Graph API, etc.), agents can request tokens via Managed Identity without any stored credentials.

### Environment Variables (Development Only)

```yaml
credentials:
  provider: env
  agents:
    dev-cortiva:
      GITHUB_TOKEN: GITHUB_TOKEN  # reads from env
```

## Layer 6: Encryption at Rest

Agent identity files, journals, memories, and workspace data can be encrypted at rest using AES-256-GCM.

### Azure Key Vault Key

```yaml
encryption:
  enabled: true
  provider: azure-keyvault
  key_vault_url: https://contoso.vault.azure.net
  key_name: cortiva-encryption-key
```

The encryption key is stored in the customer's Azure Key Vault. The node accesses it via Managed Identity. No key material is stored on the node.

### Local Key File (Development)

```yaml
encryption:
  enabled: true
  provider: local
```

A 256-bit key is auto-generated at `{agents_dir}/.cortiva/encryption.key` with permissions `0600`.

### What Gets Encrypted

- Agent identity files (soul.md, skills.md, procedures.md, etc.)
- Journal entries
- Workspace files
- Task queue and exception data

### What Doesn't Get Encrypted

- The `cortiva.yaml` config file itself (contains no agent data)
- The audit log (must be readable for verification)
- IPC socket communication (local-only, protected by file permissions)

## Layer 7: Data Boundary Enforcement

Controls where agent data can flow, critical for data residency compliance.

### LLM Endpoint Restrictions

```yaml
data_boundary:
  region: "UK South"
  allowed_llm_endpoints:
    - "https://contoso-openai.openai.azure.com"
  denied_llm_endpoints:
    - "https://api.openai.com"
    - "https://api.anthropic.com"
```

All LLM calls are validated against these lists before execution. Denied calls are blocked with a logged warning.

### Telemetry Separation

Agent activity data (audit logs, task details, performance metrics) stays with the customer. Only safe platform fields go to Cortiva HQ.

```yaml
data_boundary:
  telemetry:
    customer_sink: azure-monitor
    platform_sink: "https://telemetry.cortivahq.com"
    platform_fields: [agent_count, uptime, version, heartbeat_interval]
```

The `filter_platform_telemetry()` method strips all agent-specific data before transmission. Only the fields listed in `platform_fields` pass through.

## Secure Deployment Guide

### Development (Local)

Minimal security — suitable for experimentation.

```yaml
isolation:
  tier: soft
```

### Single-Organisation Production

For teams running agents on their own infrastructure.

```yaml
isolation:
  tier: container
  container:
    network: bridge
    cpu_limit: "1.0"
    memory_limit: "512m"

encryption:
  enabled: true
  provider: local

resource_limits:
  defaults:
    cycle_timeout_s: 120
    max_disk_mb: 500
    max_hours_per_day: 10

policies:
  defaults:
    tools:
      denied: [Bash]
    execution:
      deny: ["drop database*", "rm -rf*"]
    filesystem:
      workspace_only: true
```

### Customer-Deployed Node (Cortiva HQ Managed)

For SMEs whose node is deployed into their Azure tenant by Cortiva HQ.

```yaml
isolation:
  tier: container
  container:
    runtime: docker
    network: bridge

encryption:
  enabled: true
  provider: azure-keyvault
  key_vault_url: https://contoso.vault.azure.net

credentials:
  provider: azure-keyvault
  key_vault_url: https://contoso.vault.azure.net
  agents:
    dev-cortiva:
      AZURE_DEVOPS_PAT: secret/devops-pat
    pm-cortiva:
      LINEAR_API_KEY: secret/linear-key

data_boundary:
  region: "UK South"
  allowed_llm_endpoints:
    - "https://contoso-openai.openai.azure.com"
  denied_llm_endpoints:
    - "https://api.openai.com"
    - "https://api.anthropic.com"
  telemetry:
    customer_sink: azure-monitor
    platform_sink: "https://telemetry.cortivahq.com"
    platform_fields: [agent_count, uptime, version]

resource_limits:
  defaults:
    cycle_timeout_s: 120
    max_disk_mb: 500
    max_hours_per_day: 8

policies:
  defaults:
    tools:
      allowed: [Read, Write, Edit, Glob, Grep]
      denied: [Bash]
    execution:
      require_approval: ["deploy*", "merge to main*"]
      deny: ["drop*", "delete production*"]
    filesystem:
      workspace_only: true

hooks:
  routes:
    - source: pagerduty
      events: ["incident.trigger"]
      agent: dev-cortiva
      priority: critical
      wake_if_sleeping: true
```

### What the Customer's Security Team Can Verify

| Audit Question | How to Verify |
|---------------|---------------|
| Is data encrypted at rest? | Check `encryption.enabled: true` and Key Vault config |
| Where do LLM calls go? | Check `data_boundary.allowed_llm_endpoints` |
| Does Cortiva HQ see our data? | Check `data_boundary.telemetry.platform_fields` — only agent_count/uptime/version |
| How are secrets managed? | Check `credentials.provider: azure-keyvault` — Managed Identity, no local secrets |
| What can agents do? | Check `policies` — tool restrictions, action approvals, filesystem limits |
| Can agents run indefinitely? | Check `resource_limits` — cycle timeout, max hours, disk quota |
| Is there an audit trail? | Verify audit log chain: `AuditLog.verify(date)` returns `(True, None)` |
| Can agents access each other's data? | Check `isolation.tier` — Tier 1+ blocks cross-agent access |

## Known Limitations

### The Fabric is the Trust Boundary

The Fabric process has unrestricted access to all agents within its deployment. Isolation controls constrain terminal subprocesses and memory access, not the orchestrator itself. This means:

- All agents in a Fabric share a trust domain
- A compromised Fabric process has access to all agent data
- The Fabric assembles every LLM prompt, seeing all agent context

For cross-trust-domain workloads (e.g., different customers, different regulatory regimes), deploy separate Fabric instances.

### Budget Enforcement is Fabric-Side

The resource guard and consciousness budget run in the Fabric process. They can be bypassed by a compromised Fabric, though the audit log would record anomalies. For hard budget limits, use API-level spend caps on your LLM provider.

### Container Escape Risk

Tier 3 uses standard Docker namespaces. Container escape vulnerabilities (which occur periodically) would grant access to the host. For higher assurance, use gVisor or Kata Containers as the runtime.

### XOR Fallback Without Cryptography Library

If the `cryptography` Python library is not installed, encryption falls back to XOR obfuscation, which is **not secure**. Always install the `cryptography` library for production deployments: `pip install cortiva[encryption]`.

## Reporting Security Issues

See [SECURITY.md](https://github.com/Innovology/cortiva/blob/main/SECURITY.md) for responsible disclosure instructions. Do not open public issues for security vulnerabilities.
