## What

<!-- Required. One sentence on what changed. -->

## Why

<!-- Required. Why is this needed? Link the issue. -->

Closes #

## Acceptance Criteria

<!-- Required. CI fails if:
       · this section is missing
       · no items are checked
       · an unchecked item is not written as "N/A — <reason>"
       · a checked item has no verification note after the "—"
     Format for verified items:  - [x] Label — what you verified and how
     Format for N/A items:       - [ ] Label — N/A — reason this doesn't apply -->

- [ ] Tests — <!-- e.g. "pytest 47/47 pass", "mypy clean" -->
- [ ] Exercised locally or in staging — <!-- describe the run, or N/A — reason -->
- [ ] Adjacent paths spot-checked — <!-- what you checked for regressions, or N/A — reason -->

## How

<!-- How was this implemented? Any design decisions worth noting? -->

## ADR / RFC Reference

<!--
REQUIRED. Choose one:
  1. Link to an Architecture Decision Record or RFC that covers this change:
       ADR-0023  or  [[ADR-0023-slug]]  or  full wiki URL
  2. No architectural decision needed — state why:
       N/A — <reason e.g. "dependency version bump", "typo fix", "test-only change">

Leaving this blank or with the placeholder text will fail the ADR lint CI check.
-->

**ADR/RFC:** <!-- fill in -->

## AI Delegation

<!--
REQUIRED. Choose one:
  1. Describe what was delegated to AI: "Claude drafted the test suite; I reviewed and adjusted 3 assertions."
  2. Nothing delegated: "N/A — written by hand; no AI assistance used."

Leaving blank or with the placeholder fails the AI Delegation lint check.
-->

**AI Delegation:** <!-- fill in -->

**Prompt owner:** <!-- @handle of the squad tech lead accountable for prompt quality -->

## Agent Review Prompt *(required when AI Delegation is not N/A — delete for human-authored PRs)*

<!-- Fill before requesting human review. CI will fail if this block is missing or placeholder. -->
<!-- Schema: schemas/agent-review-prompt.schema.json -->

**Change summary:** <!-- One-line description of what changed and why — min 20 chars, be specific -->
**Risks:** <!-- security | money-path | data-integrity | backwards-compat | performance | none — <reason> -->
**Human review focus:** <!-- What the reviewer should scrutinise after the agent pass — min 20 chars -->

## Checklist

- [ ] Tests added / updated
- [ ] `ruff check .` passes
- [ ] `mypy src/` passes
- [ ] Documentation updated (if applicable)
- [ ] ADR/RFC reference filled above
- [ ] AI Delegation field filled above
- [ ] No secrets or credentials in this diff
