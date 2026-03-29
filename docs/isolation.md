# Agent Isolation Guide

Cortiva provides a three-tier isolation system that controls how much agents are separated from each other at runtime. Configure it via the `isolation` section in `cortiva.yaml`.

## Quick Start

Add to your `cortiva.yaml`:

```yaml
isolation:
  tier: soft    # none | soft | os | container
```

That's it. The Fabric picks up the tier at startup and applies the appropriate protections. Omitting the section (or setting `tier: none`) gives you the default behavior with no enforcement.

## Tier Reference

### Tier 1: Soft Isolation

```yaml
isolation:
  tier: soft
```

**What it does:**

- Blocks path traversal — agents can't access files outside their own directory via `../../` patterns
- Validates filenames in `today_path()`, `outbox_path()`, `workspace_path()` — rejects `..`, `/`, `\`
- Wraps the memory adapter in a `GuardedMemoryAdapter` that blocks cross-agent queries
- Locks terminal subprocess working directory to the agent's workspace
- Enforces governance boundaries via keyword matching against `responsibilities.md`

**What it doesn't do:**

- Doesn't filter environment variables (terminal subprocesses inherit the full parent env)
- Doesn't create filesystem-level boundaries (agents run in the same process)
- Doesn't containerise anything

**Good for:** Teams of agents working on the same project where you want to prevent accidental cross-contamination.

### Tier 2: OS Isolation

```yaml
isolation:
  tier: os
  allowed_env:
    - PATH
    - HOME
    - LANG
    - ANTHROPIC_API_KEY
    - OPENAI_API_KEY
```

Includes all Tier 1 protections, plus:

**What it adds:**

- **Environment variable filtering** — only variables listed in `allowed_env` are passed to terminal subprocesses. Defaults include `PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `TZ`, and the standard API key variables.
- **Per-agent TMPDIR** — each agent gets a private `.tmp/` directory inside its workspace. The `TMPDIR`, `TEMP`, and `TMP` environment variables point there.
- **Per-agent IPC socket paths** — socket paths are scoped to `agents/<id>/.cortiva/agent.sock`
- **Agent ID tagging** — `CORTIVA_AGENT_ID` is injected into the subprocess environment

**Good for:** Multi-project setups where agents shouldn't see each other's environment variables or temporary files.

### Tier 3: Container Isolation

```yaml
isolation:
  tier: container
  container:
    runtime: docker       # docker | podman
    cpu_limit: "1.0"      # CPU cores
    memory_limit: "512m"  # Memory limit
    shm_size: "256m"      # Shared memory (increase for browser agents)
    network: "bridge"     # bridge | none | host
    image: "python:3.13-slim"
    browser_endpoint: ""  # WebSocket URL for sidecar browser (optional)
```

Includes all Tier 2 protections, plus:

**What it adds:**

- **Separate container per agent** — each terminal invocation runs inside a container named `cortiva-agent-<id>`
- **Resource limits** — CPU and memory capped per container
- **Network access** — `bridge` (default) allows agents to reach external APIs (Linear, GitHub, Slack, LLM providers); use `none` for air-gapped deployments
- **Restricted volume mounts** — only the agent's directory is mounted (at `/agent` inside the container, read-write)
- **Non-root execution** — containers run as UID 1000:1000
- **Shared memory sizing** — configurable `--shm-size` for agents that drive a browser
- **Browser sidecar support** — `browser_endpoint` injects `BROWSER_WS_ENDPOINT` into the container so agents can drive Chrome via a shared browser service without installing Chromium locally
- **Automatic cleanup** — containers are removed (`--rm`) after each invocation; `cleanup()` force-removes on agent sleep

**Fallback behavior:** If the container runtime (`docker` or `podman`) is not found on PATH, Tier 3 falls back to Tier 2 (OS isolation) with a warning in the logs. This allows development on machines without Docker installed.

**Browser automation:** Agents that need to interact with web UIs (Linear, GitHub, etc.) should use a sidecar browser service rather than installing Chrome in every container. See the browser sidecar section below.

**Good for:** Deployments where terminal subprocesses handle sensitive data and you want hard resource limits and filesystem isolation.

## Browser Sidecar

Agents that need to drive a web browser (e.g., to interact with Linear, GitHub UI, or internal dashboards) should use a **sidecar browser service** rather than installing Chrome in every agent container. This keeps containers lean and shares browser resources.

Run a browser service alongside your Fabric:

```yaml
# docker-compose.yml
services:
  browserless:
    image: browserless/chrome
    ports:
      - "3000:3000"
    environment:
      - MAX_CONCURRENT_SESSIONS=10
    deploy:
      resources:
        limits:
          memory: 2G
```

Then configure agents to connect to it:

```yaml
isolation:
  tier: container
  container:
    network: bridge
    browser_endpoint: "ws://browserless:3000"
```

Cortiva injects `BROWSER_WS_ENDPOINT` into every agent container's environment. Agents (or their terminal tools) use this to connect via Chrome DevTools Protocol or Playwright:

```python
# Inside an agent's terminal session
import playwright.sync_api as pw

browser = pw.chromium.connect_over_cdp(os.environ["BROWSER_WS_ENDPOINT"])
page = browser.new_page()
page.goto("https://linear.app")
```

This pattern means:
- Agent containers stay small (no Chromium installed)
- Browser resources are shared and bounded
- The browser service can be scaled independently
- Agents on `network: bridge` can reach it; agents on `network: none` cannot

## Configuration Reference

Full `isolation` section with all options and defaults:

```yaml
isolation:
  # Isolation tier (required)
  tier: "none"                    # none | soft | os | container

  # Environment variable allowlist (Tier 2+)
  # Variables not in this list are stripped from terminal subprocess env
  allowed_env:
    - PATH
    - HOME
    - USER
    - LANG
    - LC_ALL
    - TZ
    - ANTHROPIC_API_KEY
    - OPENAI_API_KEY
    - GOOGLE_API_KEY

  # Container settings (Tier 3 only)
  container:
    runtime: "docker"             # docker | podman
    cpu_limit: "1.0"              # CPU cores per container
    memory_limit: "512m"          # Memory limit per container
    shm_size: "256m"              # Shared memory size
    network: "bridge"             # bridge | none | host
    image: "python:3.13-slim"     # Base image
    browser_endpoint: ""          # WebSocket URL for sidecar browser
```

## How It Works Internally

### The Enforcer Chain

Each tier is a Python class that inherits from the tier below:

```
NoIsolation          (Tier 0 — pass-through)
  └── SoftIsolation    (Tier 1 — path/memory guards)
        └── OSIsolation    (Tier 2 — env filtering, tmpdir)
              └── ContainerIsolation  (Tier 3 — Docker/Podman)
```

The Fabric calls `isolation.prepare_terminal_env(agent_id, cmd, cwd)` before every terminal adapter invocation. This returns a `SubprocessEnvelope` containing the (possibly wrapped) command, working directory, and environment variables. The terminal adapter uses the envelope to launch the subprocess.

### Memory Guard

When isolation is Tier 1 or above, the memory adapter is automatically wrapped in a `GuardedMemoryAdapter` at config load time. This wrapper intercepts `search()`, `recall()`, and `delete()` calls and verifies the caller's agent ID matches the target agent ID. Writes (`store()`) are always allowed — agents can only write to their own memory.

The guard is transparent to the rest of the system. `ContextBuilder`, `FamiliarityEngine`, and `LivingSummaryRegenerator` all work through the wrapped adapter without modification.

### Governance Enforcement

The `AuthorityValidator` (in `governance.py`) uses keyword-overlap matching to classify proposed actions against the agent's `responsibilities.md`. It checks in order:

1. **Negative authority statements** — `"may NOT merge"` → escalation
2. **Escalation topics** — `"scope changes, new dependencies"` → escalate to target agent
3. **Secondary rules** — `"review technical feasibility"` → requires approval
4. **Primary rules** — `"write tests"` → agent may act unilaterally
5. **Unknown** — no match found

The matching threshold is 0.3 (30% keyword overlap with the rule). This is intentionally low because action descriptions tend to be short while responsibility rules contain qualifying words.

## Docker Templates

Cortiva includes templates for Tier 3 deployments:

- `src/cortiva/templates/Dockerfile.agent` — single-agent container image
- `src/cortiva/templates/docker-compose.agent.yml` — multi-agent compose file with per-agent resource limits

These are starting points. Customise the image, install additional tools your agents need, and adjust resource limits for your workload.

## Limitations

The isolation system constrains terminal subprocesses and memory access patterns. The Fabric orchestrator itself remains a shared trust domain. See [Security Architecture](security.md) for the full trust model analysis and [Agent Autonomy Roadmap](roadmap-agent-autonomy.md) for the planned changes.
