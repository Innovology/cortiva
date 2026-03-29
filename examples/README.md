# Cortiva Examples

Runnable examples that demonstrate how to use and extend the Cortiva framework.

## Running

All examples can be run from the repository root:

```bash
PYTHONPATH=src python3 examples/<example>.py
```

No API keys are required — examples use mock adapters so you can explore
the framework without any external dependencies.

## Examples

| Example | Description |
|---------|-------------|
| [basic_lifecycle.py](basic_lifecycle.py) | Discover, wake, cycle, and sleep an agent using in-memory adapters and a mock consciousness layer. |
| [custom_adapter.py](custom_adapter.py) | Write a custom `MemoryAdapter` that wraps `InMemoryAdapter` with logging. Shows how to implement the protocol and wire the adapter into a `Fabric`. |
| [isolation_example.py](isolation_example.py) | Demonstrates all three isolation tiers: path validation, memory guards, env filtering, and container command generation. |
| [cortiva-isolated.yaml](cortiva-isolated.yaml) | Sample `cortiva.yaml` with isolation config for all three tiers (commented) and browser sidecar setup. |
