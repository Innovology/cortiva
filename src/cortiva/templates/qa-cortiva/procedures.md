# QA-Cortiva — Procedures

## Reviewing a PR

1. Read the linked issue or backlog item to understand intent.
2. Read the full diff, file by file.
3. Check for:
   - Correctness: does the code do what the spec says?
   - Tests: is every new code path covered?
   - Edge cases: what happens with empty input, large input, concurrent access?
   - Security: no injection, no secrets in code, no unsafe deserialization.
   - Conventions: ruff-clean, type-annotated, line length ≤ 100.
4. Run `pytest` and `ruff check .` locally.
5. Leave comments with specific line references.
6. Approve or request changes with a summary.

## Filing a Bug

1. Title: short, descriptive, includes the affected component.
2. Steps to reproduce: minimal, deterministic.
3. Expected vs actual behaviour.
4. Environment: Python version, OS, relevant config.
5. Severity assessment: critical / high / medium / low.

## Validating a Feature

1. Read the spec from PM-Cortiva.
2. List acceptance criteria.
3. Test each criterion manually or with automated tests.
4. Confirm documentation is updated if needed.
5. Report pass/fail to PM-Cortiva.
