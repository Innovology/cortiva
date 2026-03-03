# Dev-Cortiva — Procedures

## Starting a Task

1. Read the backlog item or issue fully.
2. Check out a new branch: `feature/<short-description>`.
3. Read existing code in the affected area before writing anything.
4. Implement the change with tests.
5. Run `pytest` and `ruff check .` locally.
6. Commit with a descriptive message.
7. Open a PR and request review from QA-Cortiva.

## Handling a Bug Report

1. Reproduce the bug with a failing test.
2. Fix the root cause (not the symptom).
3. Verify the fix doesn't break other tests.
4. Commit the test and fix together.

## Responding to PR Feedback

1. Read all comments before making changes.
2. Address each comment explicitly.
3. Push updates as new commits (don't force-push during review).
4. Re-request review when ready.

## Code Conventions

- Line length: 100 characters.
- Imports: sorted by ruff (isort-compatible).
- Type hints on all public functions.
- Async-first for adapter methods.
- Lazy imports for optional dependencies.
