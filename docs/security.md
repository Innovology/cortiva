# Security Architecture

This document describes Cortiva's trust model, isolation boundaries, and known limitations. Read this before deploying agents that handle sensitive data.

## Trust Model

Cortiva uses a **centralised orchestrator** model. The Fabric process manages all agents within a single deployment. Understanding what the Fabric can and cannot access is essential to assessing your security posture.

### What the Fabric Has Access To

The Fabric is currently a **god object** — it directly handles:

- **Agent identity files** (soul, skills, responsibilities, procedures)
- **Agent memory** via the shared memory adapter
- **LLM context assembly** — the Fabric builds every prompt that goes to the consciousness adapter
- **LLM API credentials** — shared across all agents in the deployment
- **Agent journal entries** and workspace files
- **Inter-agent message routing** via the channel adapter

This means all agents within a single Fabric deployment share a **single trust domain**. The Fabric can read any agent's data at any time as part of normal operations.

### What Isolation Protects

The three-tier isolation system (`isolation.tier` in cortiva.yaml) constrains **terminal subprocesses** (Claude Code, Codex, Aider) and **memory access patterns**, not the orchestration layer itself.

| Boundary | Protected | Not Protected |
|----------|-----------|---------------|
| Agent filesystem | Terminal subprocess can't traverse to other agents (Tier 1+) | Fabric reads all agent directories directly |
| Memory store | Cross-agent queries blocked via GuardedMemoryAdapter (Tier 1+) | Fabric has unrestricted access to the inner adapter |
| Environment variables | Filtered to allowlist for subprocesses (Tier 2+) | Fabric process has full env access |
| Container boundary | Terminal runs in Docker/Podman with resource limits (Tier 3) | Fabric runs on the host, not in a container |
| API credentials | Shared across all agents | No per-agent credential isolation |

### Implications for Multi-Tenant Deployments

**Agents within a single Fabric share a trust domain.** This is fine for:

- Multiple agents working on the same project (dev, QA, PM)
- Agents within the same organisation and compliance boundary
- Development and testing environments

This is **not sufficient** for:

- Agents handling data from different regulatory regimes (e.g., healthcare + banking)
- Agents belonging to different organisations
- Any scenario where one agent's data must be cryptographically inaccessible to another

For cross-regulatory workloads, run **separate Fabric instances** with separate configurations, credentials, and memory stores.

## Isolation Tiers

### Tier 0: None (default)

No enforcement. All agents share a process, filesystem, memory, and credentials. This is backward-compatible with pre-isolation Cortiva.

### Tier 1: Soft

Process-level enforcement within the existing architecture:

- **Path traversal prevention** — `Path.resolve()` checks prevent agents from accessing files outside their directory via `../../` patterns
- **Filename validation** — `today_path()`, `outbox_path()`, and `workspace_path()` reject filenames containing path separators or `..`
- **Memory guard** — `GuardedMemoryAdapter` wraps the memory adapter and blocks cross-agent queries
- **Terminal cwd enforcement** — terminal subprocesses are locked to the agent's workspace directory
- **Governance enforcement** — `AuthorityValidator` classifies actions against the agent's responsibility boundaries using keyword-overlap matching

### Tier 2: OS

All Tier 1 protections plus OS-level subprocess isolation:

- **Environment variable filtering** — only allowlisted variables are passed to terminal subprocesses
- **Per-agent TMPDIR** — each agent gets a private temporary directory inside its workspace
- **Per-agent IPC socket paths** — socket paths are scoped per agent
- **Agent ID tagging** — `CORTIVA_AGENT_ID` is set in the subprocess environment for audit purposes

### Tier 3: Container

All Tier 2 protections plus Docker/Podman container isolation:

- **Separate container per agent** — each terminal invocation runs inside `cortiva-agent-<id>`
- **CPU and memory limits** — configurable per deployment
- **Network isolation** — `--network=none` by default
- **Restricted volume mounts** — only the agent's own directory is mounted
- **Non-root execution** — containers run as UID 1000

## Known Limitations

### The Fabric is the Trust Boundary

The isolation system constrains terminal subprocesses and memory access patterns. It does not isolate the Fabric orchestrator itself. The Fabric:

1. Reads every agent's identity files to build LLM context
2. Queries every agent's memories via the memory adapter (bypassing the guard)
3. Sends all LLM requests using shared API credentials
4. Assembles prompts that may contain sensitive agent-specific data

Until the Fabric is decomposed (see [Agent Autonomy Roadmap](roadmap-agent-autonomy.md)), all agents within a deployment are in the same trust domain.

### Budget Enforcement is Fabric-Side

The consciousness budget manager runs in the Fabric process. A compromised or misconfigured Fabric can:

- Reset budget counters mid-day
- Raise budget limits without agent consent
- Suppress sleep signals, allowing agents to work indefinitely
- Override priority preemption rules

Budget enforcement is currently advisory, not cryptographic. For hard budget limits, use API-level spend caps on your LLM provider or deploy a budget proxy adapter (see roadmap).

### Shared API Credentials

All agents in a Fabric deployment share the same LLM API key. This means:

- All agents' prompts (including sensitive context) are billed to the same account
- A compromised agent's prompts are indistinguishable from a legitimate agent's at the API level
- There is no per-agent audit trail at the API provider

### Container Escape Risk

Tier 3 uses standard Docker/Podman namespaces. Container escape vulnerabilities (which occur periodically — e.g., CVE-2024-21626) would grant access to the host and all agent data. For higher assurance, consider gVisor or Kata Containers as the runtime.

### No Encryption at Rest

Agent workspaces (identity files, journals, workspace) are stored as plaintext on the host filesystem. Sensitive data in agent memories or journals is not encrypted.

### No Tamper-Evident Audit Trail

The event bus logs to a JSON Lines file with no integrity chain. There is no signing, append-only storage guarantee, or external SIEM forwarding. The Fabric can retroactively modify audit logs.

## Recommendations by Deployment Scenario

### Single-Project Team (Low Sensitivity)

```yaml
isolation:
  tier: soft
```

Adequate for dev/QA/PM agents working on the same codebase. Prevents accidental cross-contamination. All agents share credentials and trust the Fabric.

### Multi-Project, Single Organisation

```yaml
isolation:
  tier: os
```

Adds environment filtering and per-agent tmpdir. Suitable when agents work on different projects within the same organisation and compliance boundary.

### Sensitive Workloads

```yaml
isolation:
  tier: container
  container:
    runtime: docker
    cpu_limit: "1.0"
    memory_limit: "512m"
    network: "none"
```

Adds container boundaries. Suitable when terminal subprocesses handle sensitive data and you want resource limits. Remember: the Fabric still has full access.

### Cross-Regulatory Workloads

**Run separate Fabric instances.** Do not put healthcare and banking agents in the same deployment. Each Fabric should have:

- Its own `cortiva.yaml` with its own API keys
- Its own memory store (separate database/namespace)
- Its own agent directory
- Its own process and user account

This is the only currently supported way to achieve true inter-agent isolation at the orchestration level.

## Future Architecture

The [Agent Autonomy Roadmap](roadmap-agent-autonomy.md) describes the planned architectural changes that will close the gaps documented above:

- **Agent-owned cognitive loop** — context assembly and LLM calls move inside the agent boundary
- **Budget proxy adapter** — hard budget enforcement via an external service
- **Per-agent credentials** — separate API keys per agent, managed by a secret store
- **Agent-side schedule enforcement** — agents verify lifecycle commands against their own contract
- **Internal channel adapter** — structured inter-agent communication within a Fabric without shared memory
