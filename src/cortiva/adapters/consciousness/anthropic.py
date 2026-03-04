"""
Anthropic consciousness adapter for Cortiva.

Uses the Anthropic API (Claude) as the conscious thinking layer.
Every call is a moment of conscious thought — expensive, deliberate,
and reserved for when real thinking is needed.

Install: pip install anthropic
"""

from __future__ import annotations

import os
from typing import Any

from cortiva.adapters.protocols import ConsciousResponse, Priority

REFLECTION_SUFFIX_INSTRUCTIONS = """\

After completing the task, you may optionally append a structured reflection \
suffix to your response. Place it after your main response, separated by the \
exact delimiter line shown below. The suffix must be valid JSON.

---REFLECTION---
{
  "outcome": "One-sentence summary of what you accomplished",
  "learned": "Key insight or lesson from this task (stored as a memory)",
  "prediction_error": "What surprised you or differed from expectations",
  "procedure_update": "New or revised procedure step to add to your procedures",
  "messages": [{"to": "agent-id", "content": "message body"}],
  "escalation": "Issue requiring human or supervisor attention"
}

All fields are optional — include only those that apply. \
Do NOT include the reflection suffix if you have nothing meaningful to report."""


class AnthropicConsciousnessAdapter:
    """
    Consciousness adapter backed by Anthropic's Claude API.

    Each invocation is a fresh context window. The agent's identity
    and state are provided in the context parameter, assembled by
    the subconscious layer.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        max_tokens: int = 4096,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self._client = None
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self._api_key)
            except ImportError:
                raise ImportError(
                    "anthropic is not installed. "
                    "Install it with: pip install anthropic"
                )
        return self._client

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
        client = self._get_client()

        system_prompt = (
            "You are an autonomous agent in an organisation. "
            "Your identity, skills, responsibilities, and current state "
            "are provided in the context below. Act as this agent — "
            "make decisions, complete tasks, and communicate as them.\n\n"
            f"{context}"
        )

        effective_prompt = prompt
        if metadata and metadata.get("task_execution"):
            effective_prompt = prompt + "\n\n" + REFLECTION_SUFFIX_INSTRUCTIONS

        message = client.messages.create(
            model=self.model,
            max_tokens=max_tokens or self.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": effective_prompt}],
        )

        content = ""
        for block in message.content:
            if hasattr(block, "text"):
                content += block.text

        return ConsciousResponse(
            content=content,
            tokens_in=message.usage.input_tokens,
            tokens_out=message.usage.output_tokens,
            model=self.model,
            metadata={"agent_id": agent_id, "priority": priority.value},
        )

    async def reflect(
        self,
        agent_id: str,
        context: str,
        day_summary: str,
    ) -> ConsciousResponse:
        prompt = (
            "Your working day is ending. Here is a summary of what happened:\n\n"
            f"{day_summary}\n\n"
            "Please:\n"
            "1. Rewrite your Living Summary (identity.md) to reflect today's "
            "experiences and any changes to how you see yourself and your role.\n"
            "2. Write a brief journal entry noting what you learned, "
            "what went well, and what you'd do differently.\n\n"
            "Format your response as:\n"
            "## Living Summary\n[updated identity]\n\n"
            "## Journal\n[today's reflection]"
        )

        response = await self.think(
            agent_id=agent_id,
            context=context,
            prompt=prompt,
            priority=Priority.NORMAL,
        )

        # Split response into identity update and journal entry
        content = response.content
        reflection = None

        if "## Journal" in content:
            parts = content.split("## Journal", 1)
            content = parts[0].replace("## Living Summary", "").strip()
            reflection = parts[1].strip()

        return ConsciousResponse(
            content=content,
            reflection=reflection,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            model=response.model,
            metadata=response.metadata,
        )
