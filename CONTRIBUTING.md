# Contributing to Cortiva

Cortiva is an open-source project and we welcome contributions.

## Getting Started

```bash
git clone https://github.com/cortiva-dev/cortiva.git
cd cortiva
pip install -e ".[dev]"
pytest
```

## Architecture

Cortiva uses a pluggable adapter pattern. Every external dependency
sits behind a Protocol interface in `src/cortiva/adapters/protocols.py`.

To add a new memory backend, consciousness provider, or communication
channel, implement the relevant Protocol and register it.

### Directory Structure

```
src/cortiva/
├── __init__.py              # Public API
├── adapters/
│   ├── protocols.py         # Adapter interfaces (Protocols)
│   ├── memory/              # Memory implementations
│   │   ├── engram.py        # Engram adapter
│   │   └── inmemory.py      # In-memory (testing)
│   ├── consciousness/       # LLM thinking layer
│   │   └── anthropic.py     # Claude adapter
│   ├── routine/             # Local model layer
│   └── channel/             # Communication layer
├── core/
│   ├── agent.py             # Agent model and lifecycle
│   └── fabric.py            # Runtime orchestrator
└── cli/
    └── main.py              # CLI entry point
```

## Writing an Adapter

1. Read the Protocol in `adapters/protocols.py`
2. Create your implementation in the appropriate subdirectory
3. Add tests in `tests/`
4. Add any new dependencies as optional in `pyproject.toml`

Example: adding a Redis memory adapter would go in
`src/cortiva/adapters/memory/redis.py` and add `redis` to
`[project.optional-dependencies]`.

## Code Style

- Python 3.11+
- Type hints everywhere
- `ruff` for formatting and linting
- `mypy` for type checking
- `pytest` for tests

## Pull Requests

- One feature per PR
- Tests required for new functionality
- Update README if adding a new adapter
