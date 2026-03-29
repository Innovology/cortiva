"""Tests for consciousness adapters, router, and config integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cortiva.adapters.protocols import ConsciousResponse, Priority
from cortiva.core.consciousness_router import ConsciousnessRouter


# ---------------------------------------------------------------------------
# ConsciousnessRouter
# ---------------------------------------------------------------------------


class MockAdapter:
    """Minimal mock that implements the ConsciousnessAdapter protocol."""

    def __init__(self, name: str = "default") -> None:
        self.name = name

    async def think(self, agent_id, context, prompt, **kwargs):
        return ConsciousResponse(
            content=f"[{self.name}] thought",
            model=self.name,
        )

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(
            content=f"[{self.name}] reflected",
            model=self.name,
        )


class TestConsciousnessRouter:
    @pytest.mark.asyncio
    async def test_routes_to_default_when_no_override(self) -> None:
        default = MockAdapter("default")
        router = ConsciousnessRouter(default=default)

        resp = await router.think("a", "ctx", "prompt")
        assert resp.model == "default"

    @pytest.mark.asyncio
    async def test_routes_to_override_by_call_type(self) -> None:
        default = MockAdapter("default")
        plan_adapter = MockAdapter("planner")
        router = ConsciousnessRouter(
            default=default,
            overrides={"plan": plan_adapter},
        )

        resp = await router.think(
            "a", "ctx", "prompt",
            metadata={"call_type": "plan"},
        )
        assert resp.model == "planner"

    @pytest.mark.asyncio
    async def test_falls_back_to_default_for_unknown_call_type(self) -> None:
        default = MockAdapter("default")
        router = ConsciousnessRouter(
            default=default,
            overrides={"plan": MockAdapter("planner")},
        )

        resp = await router.think(
            "a", "ctx", "prompt",
            metadata={"call_type": "unknown"},
        )
        assert resp.model == "default"

    @pytest.mark.asyncio
    async def test_reflect_uses_reflect_override(self) -> None:
        default = MockAdapter("default")
        reflector = MockAdapter("reflector")
        router = ConsciousnessRouter(
            default=default,
            overrides={"reflect": reflector},
        )

        resp = await router.reflect("a", "ctx", "summary")
        assert resp.model == "reflector"

    @pytest.mark.asyncio
    async def test_reflect_falls_back_to_default(self) -> None:
        default = MockAdapter("default")
        router = ConsciousnessRouter(default=default)

        resp = await router.reflect("a", "ctx", "summary")
        assert resp.model == "default"

    def test_resolve(self) -> None:
        default = MockAdapter("default")
        msg = MockAdapter("msg")
        router = ConsciousnessRouter(default=default, overrides={"message": msg})

        assert router.resolve("message").name == "msg"
        assert router.resolve("plan").name == "default"


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


class TestAdapterRegistry:
    def test_openai_in_registry(self) -> None:
        from cortiva.core.config import _CONSCIOUSNESS_ADAPTERS
        assert "openai" in _CONSCIOUSNESS_ADAPTERS
        assert "openai-compatible" in _CONSCIOUSNESS_ADAPTERS

    def test_google_in_registry(self) -> None:
        from cortiva.core.config import _CONSCIOUSNESS_ADAPTERS
        assert "google" in _CONSCIOUSNESS_ADAPTERS

    def test_anthropic_still_in_registry(self) -> None:
        from cortiva.core.config import _CONSCIOUSNESS_ADAPTERS
        assert "anthropic" in _CONSCIOUSNESS_ADAPTERS


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter unit tests (mocked)
# ---------------------------------------------------------------------------


class TestOpenAICompatibleAdapter:
    def test_init_defaults(self) -> None:
        from cortiva.adapters.consciousness.openai_compat import OpenAICompatibleAdapter
        adapter = OpenAICompatibleAdapter()
        assert adapter.model == "gpt-4o"

    def test_init_custom(self) -> None:
        from cortiva.adapters.consciousness.openai_compat import OpenAICompatibleAdapter
        adapter = OpenAICompatibleAdapter(
            model="gpt-4o-mini",
            base_url="https://api.example.com/v1",
            max_tokens=2048,
        )
        assert adapter.model == "gpt-4o-mini"
        assert adapter._base_url == "https://api.example.com/v1"
        assert adapter.max_tokens == 2048

    @pytest.mark.asyncio
    async def test_think_calls_api(self) -> None:
        from cortiva.adapters.consciousness.openai_compat import OpenAICompatibleAdapter

        adapter = OpenAICompatibleAdapter(api_key="test-key")

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 100
        mock_usage.completion_tokens = 50

        mock_message = MagicMock()
        mock_message.content = "I completed the task."

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        adapter._client = mock_client

        resp = await adapter.think("agent-01", "context", "do something")
        assert resp.content == "I completed the task."
        assert resp.tokens_in == 100
        assert resp.tokens_out == 50


# ---------------------------------------------------------------------------
# Google adapter stub tests
# ---------------------------------------------------------------------------


class TestGoogleAdapter:
    def test_init(self) -> None:
        from cortiva.adapters.consciousness.google import GoogleAdapter
        adapter = GoogleAdapter(model="gemini-2.0-flash")
        assert adapter.model == "gemini-2.0-flash"

    def test_init_params(self) -> None:
        from cortiva.adapters.consciousness.google import GoogleAdapter
        adapter = GoogleAdapter(model="gemini-2.5-pro", api_key="test-key", max_tokens=8192)
        assert adapter.model == "gemini-2.5-pro"
        assert adapter._api_key == "test-key"
        assert adapter.max_tokens == 8192

    def test_get_client_raises_without_sdk(self) -> None:
        from cortiva.adapters.consciousness.google import GoogleAdapter
        adapter = GoogleAdapter()
        # If google-genai is not installed, _get_client should raise ImportError
        # If it IS installed, it should return a client — both are valid
        try:
            client = adapter._get_client()
            assert client is not None
        except ImportError as e:
            assert "google-genai" in str(e)


# ---------------------------------------------------------------------------
# Config integration — build_fabric with different providers
# ---------------------------------------------------------------------------


class TestConfigConsciousnessProviders:
    def test_build_with_openai_provider(self, tmp_path) -> None:
        from cortiva.core.config import build_fabric

        config = {
            "fabric": {"name": "test"},
            "memory": {"adapter": "inmemory"},
            "consciousness": {
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test",
            },
            "agents": {"directory": str(tmp_path / "agents")},
        }

        with patch(
            "cortiva.core.config._import_adapter",
            side_effect=_mock_import_adapter,
        ):
            fabric = build_fabric(config)
        assert fabric.consciousness is not None

    def test_build_with_overrides_creates_router(self, tmp_path) -> None:
        from cortiva.core.config import build_fabric
        from cortiva.core.consciousness_router import ConsciousnessRouter

        config = {
            "fabric": {"name": "test"},
            "memory": {"adapter": "inmemory"},
            "consciousness": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "overrides": {
                    "message": {
                        "provider": "openai",
                        "model": "gpt-4o-mini",
                        "api_key": "sk-test",
                    },
                },
            },
            "agents": {"directory": str(tmp_path / "agents")},
        }

        with patch(
            "cortiva.core.config._import_adapter",
            side_effect=_mock_import_adapter,
        ):
            fabric = build_fabric(config)
        assert isinstance(fabric.consciousness, ConsciousnessRouter)

    def test_build_without_overrides_no_router(self, tmp_path) -> None:
        from cortiva.core.config import build_fabric
        from cortiva.core.consciousness_router import ConsciousnessRouter

        config = {
            "fabric": {"name": "test"},
            "memory": {"adapter": "inmemory"},
            "consciousness": {"provider": "anthropic"},
            "agents": {"directory": str(tmp_path / "agents")},
        }

        with patch(
            "cortiva.core.config._import_adapter",
            side_effect=_mock_import_adapter,
        ):
            fabric = build_fabric(config)
        assert not isinstance(fabric.consciousness, ConsciousnessRouter)


# ---------------------------------------------------------------------------
# Mock helper
# ---------------------------------------------------------------------------


def _mock_import_adapter(registry, name, kind):
    if kind == "memory":
        from cortiva.adapters.memory.inmemory import InMemoryAdapter
        return InMemoryAdapter
    if kind == "consciousness":
        class MockCls:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            async def think(self, **kw):
                return ConsciousResponse(content="ok", model="mock")
            async def reflect(self, **kw):
                return ConsciousResponse(content="ok", model="mock")
        return MockCls
    if kind == "routine":
        from cortiva.adapters.routine.simple import SimpleRoutineAdapter
        return SimpleRoutineAdapter
    class FallbackMock:
        def __init__(self, **kwargs): pass
    return FallbackMock
