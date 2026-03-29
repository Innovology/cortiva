# Cortiva Examples

Runnable examples that demonstrate how to extend and use the Cortiva framework.

All examples can be run from the repository root with:

```bash
PYTHONPATH=src python3 examples/<example>.py
```

## Examples

| File | Description |
|------|-------------|
| `custom_adapter.py` | Write a custom `MemoryAdapter` that wraps `InMemoryAdapter` with logging. Shows how to implement the protocol and wire the adapter into a `Fabric`. |
