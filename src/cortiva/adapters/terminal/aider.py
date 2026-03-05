"""Aider terminal agent adapter (stub)."""

from __future__ import annotations

from pathlib import Path

from cortiva.adapters.protocols import AgentResponse, ToolCapabilities


class AiderAdapter:
    """Placeholder for Aider CLI integration."""

    async def invoke(
        self,
        prompt: str,
        cwd: Path,
        *,
        output_format: str = "json",
        allowed_tools: list[str] | None = None,
        max_turns: int | None = None,
    ) -> AgentResponse:
        raise NotImplementedError("AiderAdapter is not yet implemented")

    async def is_available(self) -> bool:
        return False

    async def capabilities(self) -> ToolCapabilities:
        return ToolCapabilities()
