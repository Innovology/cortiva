# QA-Cortiva — Technical Skills

## Testing Framework: pytest

- **Fixtures**: designing reusable fixtures with appropriate scope (`function`,
  `session`, `module`). Building fixture hierarchies that provide isolated test
  databases, mock adapters, and pre-configured agent instances.
- **Parametrize**: using `@pytest.mark.parametrize` to run the same test logic
  across multiple input sets, especially for boundary conditions and error
  variants.
- **Asyncio testing**: configuring `pytest-asyncio` with `auto` mode, writing
  async test functions, managing event loop lifecycle, and testing concurrent
  task behaviour with `asyncio.gather` and `asyncio.wait_for`.
- **Mocking**: using `unittest.mock.AsyncMock` and `unittest.mock.patch` to
  isolate units under test. Building mock adapters that conform to Cortiva's
  Protocol interfaces. Verifying call sequences with `assert_called_once_with`
  and `call_args_list`.
- **Markers and filtering**: applying custom markers (`@pytest.mark.slow`,
  `@pytest.mark.integration`) to control which tests run in CI versus locally.

## Code Review Patterns

- **Security**: checking for unsanitised inputs in IPC message handling,
  path traversal in file operations, and unsafe deserialisation of agent state.
- **Performance**: identifying unnecessary awaits, N+1 query patterns in memory
  adapters, and unbounded list growth in task queues or memory recall.
- **Correctness**: verifying that Protocol method signatures match between
  interface and implementation, that state machine transitions are guarded, and
  that error handling preserves enough context for debugging.
- **Concurrency**: reviewing `asyncio.Lock` usage, checking for deadlock
  potential in multi-agent IPC, and verifying that shared state is protected.

## Test Coverage Analysis

- Reading and interpreting `coverage.py` reports to identify untested branches.
- Distinguishing between meaningful coverage gaps (untested error paths) and
  acceptable gaps (platform-specific code, abstract protocol stubs).
- Tracking coverage trends over time to catch gradual erosion.

## Cortiva Adapter Protocol System

- Deep understanding of the five protocol interfaces: `MemoryAdapter`,
  `GraphMemoryAdapter`, `ConsciousnessAdapter`, `RoutineAdapter`,
  `ChannelAdapter`, and `TerminalAgentAdapter`.
- Ability to write conformance tests that verify any adapter implementation
  satisfies its protocol contract, including edge cases like empty results,
  connection failures, and timeout behaviour.
- Understanding of how the `consciousness_router` selects adapters and how
  the `fabric` wires adapters into the agent lifecycle.

## Edge Case Identification

- Systematic enumeration of boundary conditions: empty inputs, single-element
  collections, maximum-length strings, Unicode edge cases, and concurrent
  modification scenarios.
- Identifying implicit assumptions in code (e.g., "this list is never empty",
  "this dict always has key X") and writing tests that violate those
  assumptions.
- Recognising temporal edge cases: midnight rollovers in journal paths, UTC
  versus local time mismatches, and race conditions during agent state
  transitions.
