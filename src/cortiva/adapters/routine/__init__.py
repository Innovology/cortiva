"""Routine (subconscious) adapters for procedural task matching."""

from cortiva.adapters.routine.simple import SimpleRoutineAdapter

__all__ = ["SimpleRoutineAdapter"]

# OllamaRoutineAdapter requires the ``ollama`` optional extra and a
# running Ollama instance.  Import it explicitly when needed:
#   from cortiva.adapters.routine.ollama import OllamaRoutineAdapter
