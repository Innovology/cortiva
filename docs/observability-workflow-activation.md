# Observability Gate — Workflow Activation Required

**Status:** PENDING ACTIVATION
**Date:** 2026-07-14
**Owner:** Anika (Head of SRE)
**Ref:** RB-001 §7

## Background

The observability gate script (`.github/scripts/check-observability.sh`) was deployed in PR #105.
It enforces three gates on every PR:
- **G1** — `## Observability Impact` section required when `src/cortiva/` is touched
- **G2** — `trace_id=` required on every new `FabricEvent()` / `emit_simple()` call
- **G3** — `traceparent` header required on new outbound HTTP calls

The script is live. The workflow that RUNS it is not — blocked by PAT scope.

## Action Required

A repo maintainer with `workflow` scope must create:

**File:** `.github/workflows/observability-lint.yml`

Content:
```yaml
name: Observability Gate

on:
  pull_request:
    types: [opened, edited, synchronize, reopened]

jobs:
  observability-check:
    name: Verify Observability Gate (RB-001 §7)
    runs-on: ubuntu-latest
    if: github.actor != 'dependabot[bot]'
    permissions:
      pull-requests: read
      contents: read

    steps:
      - uses: actions/checkout@v4

      - name: Run observability gate
        env:
          PR_BODY: ${{ github.event.pull_request.body }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
          REPO: ${{ github.repository }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          chmod +x .github/scripts/check-observability.sh
          .github/scripts/check-observability.sh
```

This can be done via GitHub UI, `gh` CLI, or a PAT with `workflow` scope.

## Why it matters

Without this workflow, the gate script runs nowhere. PRs missing trace context will not be caught.
This is a production-blocker equivalent — once Railway services move to production, uncorrelated
traces make incident triage blind. See RB-001 §7 for full gate spec.
