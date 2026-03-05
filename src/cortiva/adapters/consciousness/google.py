"""
Google Gemini consciousness adapter (stub).

The Gemini API differs enough from OpenAI-compatible APIs to warrant
its own adapter.  This stub defines the interface; implementation
requires ``google-generativeai``.
"""

from __future__ import annotations

from typing import Any

from cortiva.adapters.protocols import ConsciousResponse, Priority


class GoogleAdapter:
    """Placeholder for Google Gemini API integration."""

    def __init__(
        self,
        model: str = "gemini-2.0-flash",
        api_key: str | None = None,
        max_tokens: int = 4096,
    ):
        self.model = model
        self._api_key = api_key
        self.max_tokens = max_tokens

    async def think(
        self,
        agent_id: str,
        context: str,
        prompt: str,
        *,
        priority: Priority = Priority.NORMAL,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConsciousResponse:
        raise NotImplementedError("GoogleAdapter is not yet implemented")

    async def reflect(
        self,
        agent_id: str,
        context: str,
        day_summary: str,
    ) -> ConsciousResponse:
        raise NotImplementedError("GoogleAdapter is not yet implemented")
