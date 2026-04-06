# Writing Custom Adapters

Every external dependency in Cortiva sits behind a Protocol interface. Memory systems, LLM providers, communication channels, terminal agents, and subconscious routers are all adapters. You can swap any of them without changing your agents or the framework.

This guide explains how the adapter system works and how to write your own.

## Architecture

Cortiva's adapter system has three key properties:

1. **Protocol interfaces** -- Each adapter type is defined as a Python `Protocol` (from `typing`). No base classes, no inheritance. If your class has the right methods with the right signatures, it is a valid adapter.

2. **Lazy imports** -- Adapter classes are never imported at framework startup. The config system maps adapter names to `(module_path, class_name)` pairs and imports them only when needed. This means optional dependencies (like `slack-sdk` or `discord.py`) do not need to be installed unless you actually use that adapter.

3. **Config registry** -- Each adapter type has a registry dict in `cortiva/core/config.py` that maps string names to module/class pairs. The `cortiva.yaml` file references adapters by name; the framework looks them up and instantiates them at startup.

## The Five Adapter Types

### Memory Adapter

Stores and retrieves agent memories. Every agent accumulates experience over time, and the memory adapter is the persistence layer.

```python
class MemoryAdapter(Protocol):
    async def store(self, agent_id: str, content: str, *, tags=None, importance=5.0, metadata=None) -> MemoryRecord: ...
    async def search(self, agent_id: str, query: str, *, limit=10, min_importance=0.0, tags=None) -> list[MemoryRecord]: ...
    async def recall(self, agent_id: str, *, limit=20, min_importance=0.0) -> list[MemoryRecord]: ...
    async def delete(self, agent_id: str, memory_id: str) -> bool: ...
```

Built-in implementations: `InMemoryAdapter` (dict-backed, no persistence), `EngramMemoryAdapter`, `Neo4jMemoryAdapter`.

There is also a `GraphMemoryAdapter` extension that adds `create_edge()`, `find_clusters()`, `traverse()`, and `get_edges()` for graph-based memory operations.

**Config registry key:** `_MEMORY_ADAPTERS`

### Consciousness Adapter

The expensive thinking layer -- LLM API calls for decisions, planning, and reflection.

```python
class ConsciousnessAdapter(Protocol):
    async def think(self, agent_id: str, context: str, prompt: str, *, priority=Priority.NORMAL, max_tokens=4096, metadata=None) -> ConsciousResponse: ...
    async def reflect(self, agent_id: str, context: str, day_summary: str) -> ConsciousResponse: ...
```

Built-in implementations: `AnthropicConsciousnessAdapter`, `OpenAICompatibleAdapter`, `GoogleAdapter`.

**Config registry key:** `_CONSCIOUSNESS_ADAPTERS`

### Routine Adapter

The cheap subconscious layer -- a local model that monitors, computes, and routes. It decides whether a task can be handled procedurally or needs escalation to the conscious layer.

```python
class RoutineAdapter(Protocol):
    async def assess(self, agent_id: str, task_description: str, procedural_index: str, familiarity: FamiliaritySignal) -> dict[str, Any]: ...
    async def compile_context(self, agent_id: str, identity: str, memories: list[MemoryRecord], familiarity: FamiliaritySignal, task: str, additional=None) -> str: ...
```

Built-in implementations: `SimpleRoutineAdapter`, `OllamaRoutineAdapter`.

**Config registry key:** `_ROUTINE_ADAPTERS`

### Channel Adapter

Communication between agents and between agents and humans. See the [Channel Adapters](channels.md) guide for full details.

```python
class ChannelAdapter(Protocol):
    async def send(self, sender: str, recipient: str, content: str, *, channel=None, thread_id=None) -> Message: ...
    async def receive(self, agent_id: str, *, since=None, limit=50) -> list[Message]: ...
    async def listen(self, agent_id: str, channels: list[str]) -> None: ...
```

Built-in implementations: `SlackChannelAdapter`, `DiscordChannelAdapter`, `TeamsChannelAdapter`, `InternalChannelAdapter`.

**Config registry key:** `_CHANNEL_ADAPTERS`

### Terminal Agent Adapter

CLI-based AI tools that can read/write files and run commands. These let Cortiva agents delegate coding work to tools like Claude Code, Codex, or Aider.

```python
class TerminalAgentAdapter(Protocol):
    async def invoke(self, prompt: str, cwd: Path, *, output_format="json", allowed_tools=None, max_turns=None, env=None) -> AgentResponse: ...
    async def is_available(self) -> bool: ...
    async def capabilities(self) -> ToolCapabilities: ...
```

Built-in implementations: `ClaudeCodeAdapter`, `CodexAdapter`, `AiderAdapter`.

**Config registry key:** `_TERMINAL_ADAPTERS`

## Writing a Custom Adapter: Step by Step

This example walks through building a custom memory adapter backed by Redis.

### 1. Create the Adapter Class

Create a new file at `src/cortiva/adapters/memory/redis.py`:

```python
"""Redis-backed memory adapter for Cortiva."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, UTC
from typing import Any

from cortiva.adapters.protocols import MemoryRecord


class RedisMemoryAdapter:
    """Stores agent memories in Redis with JSON serialization."""

    def __init__(self, url: str = "redis://localhost:6379", prefix: str = "cortiva:mem"):
        self._url = url
        self._prefix = prefix
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-import and cache the Redis client."""
        if self._client is None:
            try:
                import redis.asyncio as redis
            except ImportError:
                raise ImportError(
                    "redis is not installed. "
                    "Install it with: pip install 'redis>=5.0'"
                )
            self._client = redis.from_url(self._url)
        return self._client

    async def store(
        self,
        agent_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 5.0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        client = self._get_client()
        record = MemoryRecord(
            id=str(uuid.uuid4()),
            content=content,
            agent_id=agent_id,
            tags=tags or [],
            importance=importance,
            metadata=metadata or {},
        )
        key = f"{self._prefix}:{agent_id}:{record.id}"
        await client.set(key, json.dumps({
            "id": record.id,
            "content": record.content,
            "agent_id": record.agent_id,
            "tags": record.tags,
            "importance": record.importance,
            "created_at": record.created_at.isoformat(),
            "metadata": record.metadata,
        }))
        return record

    async def search(
        self,
        agent_id: str,
        query: str,
        *,
        limit: int = 10,
        min_importance: float = 0.0,
        tags: list[str] | None = None,
    ) -> list[MemoryRecord]:
        # Implementation: scan keys, deserialize, filter, return
        ...

    async def recall(
        self,
        agent_id: str,
        *,
        limit: int = 20,
        min_importance: float = 0.0,
    ) -> list[MemoryRecord]:
        # Implementation: scan keys, sort by importance, return top N
        ...

    async def delete(self, agent_id: str, memory_id: str) -> bool:
        client = self._get_client()
        key = f"{self._prefix}:{agent_id}:{memory_id}"
        return await client.delete(key) > 0
```

Key patterns to follow:

- **No `Protocol` inheritance needed.** The class just needs to have methods matching the protocol signatures. Python's `runtime_checkable` Protocol handles structural subtyping.
- **Lazy imports.** Import the optional dependency (`redis`) inside a method, not at module level. Wrap it in a try/except with a clear error message.
- **Cache the client.** Create the connection or client object once on first use and reuse it.
- **Constructor takes config kwargs.** These come directly from the `config:` section of `cortiva.yaml`.

### 2. Register It in the Config Registry

Open `src/cortiva/core/config.py` and add your adapter to the appropriate registry dict:

```python
_MEMORY_ADAPTERS: dict[str, tuple[str, str]] = {
    "inmemory": ("cortiva.adapters.memory.inmemory", "InMemoryAdapter"),
    "engram": ("cortiva.adapters.memory.engram", "EngramMemoryAdapter"),
    "neo4j": ("cortiva.adapters.memory.neo4j", "Neo4jMemoryAdapter"),
    "redis": ("cortiva.adapters.memory.redis", "RedisMemoryAdapter"),  # new
}
```

The registry maps a config name (the string you use in `cortiva.yaml`) to a `(module_path, class_name)` tuple. The framework calls `importlib.import_module(module_path)` and then `getattr(mod, class_name)` at startup -- only when that adapter is actually selected.

### 3. Use It in cortiva.yaml

```yaml
memory:
  adapter: redis
  config:
    url: "redis://localhost:6379"
    prefix: "myproject:mem"
```

Everything under `config:` is passed as keyword arguments to the adapter constructor. So the above config calls `RedisMemoryAdapter(url="redis://localhost:6379", prefix="myproject:mem")`.

### 4. Verify Protocol Compliance

You can verify your adapter matches the protocol at runtime:

```python
from cortiva.adapters.protocols import MemoryAdapter
from cortiva.adapters.memory.redis import RedisMemoryAdapter

assert isinstance(RedisMemoryAdapter(), MemoryAdapter)
```

This works because `MemoryAdapter` is decorated with `@runtime_checkable`. If your class is missing a method or has a wrong signature, the `isinstance` check will fail.

## Testing Patterns

### Mocking Adapters

Since adapters are protocol-based, you can mock them without any special framework support:

```python
from unittest.mock import AsyncMock
from cortiva.adapters.protocols import MemoryRecord

mock_memory = AsyncMock()
mock_memory.store.return_value = MemoryRecord(
    id="test-id",
    content="test content",
    agent_id="agent-1",
)
mock_memory.search.return_value = []

# Pass mock_memory anywhere a MemoryAdapter is expected
```

### Testing Async Methods

All adapter methods are async. Use `pytest-asyncio` for testing:

```python
import pytest
from cortiva.adapters.memory.redis import RedisMemoryAdapter

@pytest.mark.asyncio
async def test_store_and_recall():
    adapter = RedisMemoryAdapter(url="redis://localhost:6379")
    record = await adapter.store("agent-1", "learned something")
    assert record.agent_id == "agent-1"
    assert record.content == "learned something"

    results = await adapter.recall("agent-1", limit=5)
    assert any(r.id == record.id for r in results)
```

### Using InternalChannelAdapter for Tests

The internal channel adapter is purpose-built for testing. No external services needed:

```python
import pytest
from cortiva.adapters.channel.internal import InternalChannelAdapter

@pytest.mark.asyncio
async def test_agent_communication():
    channel = InternalChannelAdapter()
    await channel.listen("alice", ["#general"])
    await channel.listen("bob", ["#general"])

    await channel.send("alice", "broadcast", "hello team", channel="#general")

    messages = await channel.receive("bob")
    assert len(messages) == 1
    assert messages[0].content == "hello team"
    assert messages[0].sender == "alice"
```

## Best Practices

**Lazy-import dependencies.** Never import optional packages at module level. Use a `_get_client()` pattern that imports and caches on first use. This keeps `import cortiva` fast and avoids forcing users to install packages they do not need.

**Accept config via constructor kwargs.** The config system passes everything under `config:` in `cortiva.yaml` directly to the constructor. Use keyword arguments with sensible defaults.

**Support environment variable fallbacks.** For secrets (API keys, tokens), check `os.environ` as a fallback. Users should not have to put secrets in config files.

```python
def __init__(self, token: str | None = None):
    self._token = token or os.environ.get("MY_SERVICE_TOKEN")
```

**Implement self-loop prevention.** Channel adapters should track messages they have sent and skip them in `receive()`. Without this, agents can enter infinite message loops.

**Return proper data types.** Always return `MemoryRecord`, `ConsciousResponse`, `Message`, or `AgentResponse` as defined in `cortiva.adapters.protocols`. Do not return raw dicts or strings.

**Handle connection failures gracefully.** External services go down. Raise clear exceptions with actionable messages rather than letting low-level errors propagate.

**Keep adapters stateless where possible.** Connection caching and cursor tracking (like last-seen message timestamps) are fine. Avoid storing business logic state in the adapter.
