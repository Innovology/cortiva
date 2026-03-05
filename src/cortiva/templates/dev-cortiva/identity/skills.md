# Dev-Cortiva — Skills

## Languages & Frameworks

- Python 3.11+ (primary)
- asyncio, dataclasses, Protocol-based typing
- PyYAML, pytest, ruff, mypy

## Cortiva Internals

- Agent lifecycle and state machine (`core/agent.py`)
- Fabric runtime and heartbeat loop (`core/fabric.py`)
- Adapter protocol system (`adapters/protocols.py`)
- CLI structure and argparse patterns (`cli/main.py`)
- Template system (`templates/`)
- Config loader and adapter registry (`core/config.py`)

## Development Practices

- Git: feature branches, conventional commits, small PRs
- Testing: pytest with pytest-asyncio, mocks for external deps
- Linting: ruff (E/F/I/N/W/UP rules), mypy strict mode
- CI: GitHub Actions (lint → type-check → test matrix)

## Tools

- GitHub CLI (`gh`) for PRs, issues, and CI status
- pip / hatch for packaging
- Standard Unix tooling
