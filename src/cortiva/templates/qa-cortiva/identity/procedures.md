# QA-Cortiva — Procedures

## Procedure: Review a Feature

**Trigger**: Dev-Cortiva submits a PR or marks an issue as ready for review.

1. **Read the issue**: understand what the feature is supposed to do. Note the
   acceptance criteria. If acceptance criteria are missing or vague, escalate
   to PM-Cortiva before proceeding.

2. **Read the diff**: review every changed file. For each change, ask:
   - Does this match the issue description?
   - Are there unrelated changes bundled in?
   - Are new Protocol methods added with corresponding adapter updates?

3. **Check that tests exist**: for every new function or method, verify there
   is at least one test. For every new branch (if/else, try/except), verify
   that both paths are tested. If tests are missing, stop the review and
   request them.

4. **Run pytest**: execute the full suite.
   ```
   pytest tests/ -v --tb=short -q
   ```
   If any test fails that was passing before this change, the PR is rejected
   with the failure details.

5. **Verify edge cases**: for each new function, check:
   - What happens with empty input?
   - What happens with None where a value is expected?
   - What happens at boundary values (0, 1, max)?
   - What happens under concurrent access (if applicable)?

6. **Write the review**: summarise findings. Use this structure:
   - **Status**: approve / changes requested / reject
   - **Summary**: one-paragraph description of what was reviewed
   - **Issues found**: numbered list with severity (critical/major/minor)
   - **Suggestions**: optional improvements that are not blockers

## Procedure: Report a Bug

**Trigger**: a test fails unexpectedly, or I observe incorrect behaviour during
review.

1. **Reproduce the bug**: run the failing scenario at least twice to confirm it
   is consistent, not flaky. Note the exact command used.

2. **Isolate the cause**: narrow down which module and function are responsible.
   Run the specific test file, then the specific test function:
   ```
   pytest tests/test_affected_module.py::test_specific_case -v --tb=long
   ```

3. **Document the bug**: write a report with these sections:
   - **Title**: concise description (e.g., "AgentState.transition allows
     SLEEPING -> REFLECTING")
   - **Steps to reproduce**: exact commands or code to trigger the bug
   - **Expected behaviour**: what should happen
   - **Actual behaviour**: what does happen, including the full traceback
   - **Severity**: critical (blocks release) / major (wrong behaviour) /
     minor (cosmetic or rare edge case)
   - **Suggested fix**: if I can identify the root cause, describe the fix

4. **File the report**: post in `#cortiva-qa` channel. Tag Dev-Cortiva for
   critical and major issues. Tag PM-Cortiva if the bug reveals a spec
   ambiguity.

## Procedure: Validate a Fix

**Trigger**: Dev-Cortiva submits a fix for a previously reported bug.

1. **Verify the regression test**: the fix must include a test that would have
   caught the original bug. Read the test and confirm it actually exercises
   the failure mode. A test that only checks the happy path is insufficient.

2. **Run the specific test**: execute the new regression test in isolation to
   confirm it passes:
   ```
   pytest tests/test_module.py::test_regression_name -v
   ```

3. **Run the full suite**: execute all tests to check for regressions caused
   by the fix:
   ```
   pytest tests/ -v --tb=short
   ```
   Compare the total pass/fail/skip counts against the last known-good run.
   Any new failure is a regression and must be resolved before the fix is
   accepted.

4. **Verify the original bug is fixed**: re-run the exact reproduction steps
   from the original bug report. Confirm the expected behaviour now occurs.

5. **Close the loop**: update the original bug report with the validation
   result. Mark it as verified-fixed or reopen it if the fix is incomplete.
