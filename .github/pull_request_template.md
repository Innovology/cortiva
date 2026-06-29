## What

<!-- Brief description of the change. -->

## Why

<!-- Why is this change needed? Link to issue if applicable. -->

Closes #


## Acceptance Criteria
<!--
REQUIRED. List each criterion as a testable checkbox. At least one is required.
CI will fail if this section is empty or placeholder-only.

  ✅ Specific: "[ ] asyncio.run() replaces get_event_loop() in all test helpers"
  ✅ Testable: "[ ] All 3 Python version CI jobs green after this merge"
  ❌ Vague:    "[ ] It works"

Dependabot and bot-authored PRs are exempt.
-->
- [ ] <!-- criterion: what specifically must be true for this PR to be shippable? -->


## Agent Review Prompt
<!--
CODE REVIEW AGENT BLOCK.
Fill this in when AI assisted authoring or will assist review.
Copy the completed block into Claude / your review agent.
Paste the agent's output as a PR comment before requesting human review.
If no AI tooling was used, delete this section.
-->
**Change summary:** <!-- one line: what this PR changes and why -->
**Acceptance criteria to verify:**
<!-- paste your AC list from above so the agent can check each one -->
**Risks:** <!-- mark all that apply: security | data-integrity | backwards-compat | performance -->
**Human review focus:** <!-- what the reviewer should scrutinise after the agent pass -->


## How

<!-- How was this implemented? Any design decisions worth noting? -->

## Checklist

- [ ] Tests added / updated
- [ ] `ruff check .` passes
- [ ] `mypy src/` passes
- [ ] Acceptance Criteria above are specific and testable
- [ ] Agent Review Prompt completed (or section deleted if no AI tooling used)
- [ ] Documentation updated (if applicable)
- [ ] No secrets or credentials in this diff
