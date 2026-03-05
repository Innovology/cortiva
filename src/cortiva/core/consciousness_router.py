"""
Consciousness Router — per-call-type backend selection.

Allows agents to use different consciousness backends for different
call types: terminal for planning, API for quick messages, local for
procedural decisions.

Config example (in cortiva.yaml or agent frontmatter):
    consciousness:
      default:
        provider: anthropic
        model: claude-sonnet-4-5
      overrides:
        plan: terminal          # use terminal agent for planning
        reflect: terminal       # use terminal agent for reflection
        message:
          provider: openai
          model: gpt-4o-mini    # cheap model for messages
"""

from __future__ import annotations

from typing import Any

from cortiva.adapters.protocols import ConsciousResponse, ConsciousnessAdapter, Priority


class ConsciousnessRouter:
    """Routes consciousness calls to different backends by call type.

    Falls back to the default adapter for any call type without an override.
    """

    def __init__(
        self,
        default: ConsciousnessAdapter,
        overrides: dict[str, ConsciousnessAdapter] | None = None,
    ) -> None:
        self._default = default
        self._overrides = overrides or {}

    def resolve(self, call_type: str) -> ConsciousnessAdapter:
        """Get the adapter for a given call type."""
        return self._overrides.get(call_type, self._default)

    async def think(
        self,
        agent_id: str,
        context: str,
        prompt: str,
        *,
        priority: Priority = Priority.NORMAL,
        max_tokens: int = 4096,
        metadata: dict[str, Any] | None = None,
    ) -> ConsciousResponse:
        call_type = (metadata or {}).get("call_type", "default")
        adapter = self.resolve(call_type)
        return await adapter.think(
            agent_id=agent_id,
            context=context,
            prompt=prompt,
            priority=priority,
            max_tokens=max_tokens,
            metadata=metadata,
        )

    async def reflect(
        self,
        agent_id: str,
        context: str,
        day_summary: str,
    ) -> ConsciousResponse:
        adapter = self.resolve("reflect")
        return await adapter.reflect(
            agent_id=agent_id,
            context=context,
            day_summary=day_summary,
        )
