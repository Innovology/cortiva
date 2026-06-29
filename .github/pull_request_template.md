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

## Agent Block *(delete for human-authored PRs)*

**Prompt hash:** <!-- sha256 of the task prompt that generated this PR -->
**Model:** <!-- e.g. claude-sonnet-4-6 -->
**Verification:** <!-- how the agent confirmed the change is correct -->

## ADR / RFC Reference

<!-- Use [[ADR-XXXX-slug]], a bare ADR-/RFC- identifier, a full wiki URL,
     or "N/A — <reason>" for implementation-only changes. -->
