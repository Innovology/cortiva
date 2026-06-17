"""Tests for consciousness adapters: per-agent keys, reflection, and reflect()."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cortiva.adapters.protocols import ConsciousResponse, Priority


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------


class TestAnthropicAdapter:
    def _make_adapter(self, **kwargs):
        from cortiva.adapters.consciousness.anthropic import AnthropicConsciousnessAdapter

        return AnthropicConsciousnessAdapter(**kwargs)

    def test_get_client_per_agent_key(self) -> None:
        adapter = self._make_adapter(
            per_agent_keys={"agent-1": "sk-agent1-key"},
        )
        mock_client = MagicMock()
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            client = adapter._get_client("agent-1")
            mock_anthropic.Anthropic.assert_called_once_with(api_key="sk-agent1-key")
            assert client is mock_client

    def test_get_client_per_agent_key_cached(self) -> None:
        adapter = self._make_adapter(
            per_agent_keys={"agent-1": "sk-agent1-key"},
        )
        mock_client = MagicMock()
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            c1 = adapter._get_client("agent-1")
            c2 = adapter._get_client("agent-1")
            mock_anthropic.Anthropic.assert_called_once()
            assert c1 is c2

    def test_get_client_default_fallback(self) -> None:
        adapter = self._make_adapter(
            api_key="sk-default",
            per_agent_keys={"agent-1": "sk-agent1"},
        )
        mock_client = MagicMock()
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            client = adapter._get_client("agent-2")
            mock_anthropic.Anthropic.assert_called_once_with(api_key="sk-default")

    @pytest.mark.asyncio
    async def test_think_with_task_execution_metadata(self) -> None:
        from cortiva.adapters.consciousness.anthropic import REFLECTION_SUFFIX_INSTRUCTIONS

        adapter = self._make_adapter(api_key="sk-test")

        mock_block = MagicMock()
        mock_block.text = "response text"
        mock_message = MagicMock()
        mock_message.content = [mock_block]
        mock_message.usage.input_tokens = 50
        mock_message.usage.output_tokens = 25

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message
        adapter._default_client = mock_client

        resp = await adapter.think(
            "agent-1", "context", "do task",
            metadata={"task_execution": True},
        )

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs["messages"]
        assert REFLECTION_SUFFIX_INSTRUCTIONS in messages[0]["content"]
        assert resp.content == "response text"

    @pytest.mark.asyncio
    async def test_think_without_task_execution(self) -> None:
        from cortiva.adapters.consciousness.anthropic import REFLECTION_SUFFIX_INSTRUCTIONS

        adapter = self._make_adapter(api_key="sk-test")

        mock_block = MagicMock()
        mock_block.text = "response"
        mock_message = MagicMock()
        mock_message.content = [mock_block]
        mock_message.usage.input_tokens = 10
        mock_message.usage.output_tokens = 5

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message
        adapter._default_client = mock_client

        await adapter.think("agent-1", "context", "prompt")

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs["messages"]
        assert REFLECTION_SUFFIX_INSTRUCTIONS not in messages[0]["content"]

    @pytest.mark.asyncio
    async def test_reflect_splits_content(self) -> None:
        adapter = self._make_adapter(api_key="sk-test")

        mock_block = MagicMock()
        mock_block.text = (
            "## Living Summary\nI am a test agent.\n\n"
            "## Journal\nToday I learned something."
        )
        mock_message = MagicMock()
        mock_message.content = [mock_block]
        mock_message.usage.input_tokens = 100
        mock_message.usage.output_tokens = 50

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message
        adapter._default_client = mock_client

        resp = await adapter.reflect("agent-1", "context", "day summary")

        assert "I am a test agent." in resp.content
        assert resp.reflection == "Today I learned something."

    @pytest.mark.asyncio
    async def test_reflect_no_journal_section(self) -> None:
        adapter = self._make_adapter(api_key="sk-test")

        mock_block = MagicMock()
        mock_block.text = "Just a plain response without journal section."
        mock_message = MagicMock()
        mock_message.content = [mock_block]
        mock_message.usage.input_tokens = 10
        mock_message.usage.output_tokens = 5

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message
        adapter._default_client = mock_client

        resp = await adapter.reflect("agent-1", "context", "day summary")

        assert "plain response" in resp.content
        assert resp.reflection is None


# ---------------------------------------------------------------------------
# Google adapter
# ---------------------------------------------------------------------------


class TestGoogleAdapterExtended:
    def _make_adapter(self, **kwargs):
        from cortiva.adapters.consciousness.google import GoogleAdapter

        return GoogleAdapter(**kwargs)

    def test_get_client_per_agent_key(self) -> None:
        adapter = self._make_adapter(
            per_agent_keys={"agent-1": "google-key-1"},
        )
        mock_client = MagicMock()
        mock_genai = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_google = MagicMock()
        mock_google.genai = mock_genai

        with patch.dict("sys.modules", {"google": mock_google, "google.genai": mock_genai}):
            client = adapter._get_client("agent-1")
            mock_genai.Client.assert_called_once_with(api_key="google-key-1")
            assert client is mock_client

    def test_get_client_default_fallback(self) -> None:
        adapter = self._make_adapter(
            api_key="google-default",
            per_agent_keys={"agent-1": "google-agent1"},
        )
        mock_client = MagicMock()
        adapter._default_client = mock_client

        assert adapter._get_client("agent-2") is mock_client

    @pytest.fixture
    def google_mocks(self):
        """Set up sys.modules mocks for google.genai.types."""
        mock_types = MagicMock()
        mock_genai = MagicMock()
        mock_genai.types = mock_types
        mock_google = MagicMock()
        mock_google.genai = mock_genai

        modules = {
            "google": mock_google,
            "google.genai": mock_genai,
            "google.genai.types": mock_types,
        }
        with patch.dict("sys.modules", modules):
            yield mock_types

    @pytest.mark.asyncio
    async def test_think_with_task_execution(self, google_mocks) -> None:
        from cortiva.adapters.consciousness.google import REFLECTION_SUFFIX_INSTRUCTIONS

        adapter = self._make_adapter(api_key="test-key")

        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 40
        mock_usage.candidates_token_count = 20

        mock_response = MagicMock()
        mock_response.text = "completed"
        mock_response.usage_metadata = mock_usage

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        adapter._default_client = mock_client

        resp = await adapter.think(
            "agent-1", "ctx", "prompt",
            metadata={"task_execution": True},
        )

        call_args = mock_client.models.generate_content.call_args
        assert REFLECTION_SUFFIX_INSTRUCTIONS in call_args.kwargs["contents"]

    @pytest.mark.asyncio
    async def test_reflect_splits_content(self, google_mocks) -> None:
        adapter = self._make_adapter(api_key="test-key")

        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 50
        mock_usage.candidates_token_count = 30

        mock_response = MagicMock()
        mock_response.text = (
            "## Living Summary\nUpdated identity.\n\n"
            "## Journal\nReflection entry."
        )
        mock_response.usage_metadata = mock_usage

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        adapter._default_client = mock_client

        resp = await adapter.reflect("agent-1", "ctx", "day summary")

        assert "Updated identity." in resp.content
        assert resp.reflection == "Reflection entry."


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter
# ---------------------------------------------------------------------------


class TestOpenAICompatAdapterExtended:
    def _make_adapter(self, **kwargs):
        from cortiva.adapters.consciousness.openai_compat import OpenAICompatibleAdapter

        return OpenAICompatibleAdapter(**kwargs)

    def test_get_client_per_agent_key(self) -> None:
        adapter = self._make_adapter(
            per_agent_keys={"agent-1": "sk-agent1"},
            base_url="https://api.example.com/v1",
        )
        mock_client = MagicMock()
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            client = adapter._get_client("agent-1")
            mock_openai.OpenAI.assert_called_once_with(
                api_key="sk-agent1",
                base_url="https://api.example.com/v1",
            )
            assert client is mock_client

    def test_get_client_per_agent_key_no_base_url(self) -> None:
        adapter = self._make_adapter(
            per_agent_keys={"agent-1": "sk-agent1"},
        )
        mock_client = MagicMock()
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            client = adapter._get_client("agent-1")
            mock_openai.OpenAI.assert_called_once_with(api_key="sk-agent1")

    def test_get_client_default_with_base_url(self) -> None:
        adapter = self._make_adapter(
            api_key="sk-default",
            base_url="https://custom.api/v1",
        )
        mock_client = MagicMock()
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            client = adapter._get_client("any-agent")
            mock_openai.OpenAI.assert_called_once_with(
                api_key="sk-default",
                base_url="https://custom.api/v1",
            )

    @pytest.mark.asyncio
    async def test_think_with_task_execution(self) -> None:
        from cortiva.adapters.consciousness.openai_compat import REFLECTION_SUFFIX_INSTRUCTIONS

        adapter = self._make_adapter(api_key="sk-test")

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 100
        mock_usage.completion_tokens = 50

        mock_message = MagicMock()
        mock_message.content = "done"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        adapter._default_client = mock_client

        resp = await adapter.think(
            "agent-1", "ctx", "prompt",
            metadata={"task_execution": True},
        )

        call_kwargs = mock_client.chat.completions.create.call_args
        messages = call_kwargs.kwargs["messages"]
        user_msg = messages[1]["content"]
        assert REFLECTION_SUFFIX_INSTRUCTIONS in user_msg

    @pytest.mark.asyncio
    async def test_reflect_splits_content(self) -> None:
        adapter = self._make_adapter(api_key="sk-test")

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 80
        mock_usage.completion_tokens = 40

        mock_message = MagicMock()
        mock_message.content = (
            "## Living Summary\nNew identity.\n\n"
            "## Journal\nLearned a lot."
        )

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        adapter._default_client = mock_client

        resp = await adapter.reflect("agent-1", "ctx", "summary")

        assert "New identity." in resp.content
        assert resp.reflection == "Learned a lot."

    @pytest.mark.asyncio
    async def test_reflect_no_journal(self) -> None:
        adapter = self._make_adapter(api_key="sk-test")

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5

        mock_message = MagicMock()
        mock_message.content = "plain response"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        adapter._default_client = mock_client

        resp = await adapter.reflect("agent-1", "ctx", "summary")

        assert resp.reflection is None

    @pytest.mark.asyncio
    async def test_think_metadata_in_response(self) -> None:
        adapter = self._make_adapter(api_key="sk-test")

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5

        mock_message = MagicMock()
        mock_message.content = "ok"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        adapter._default_client = mock_client

        resp = await adapter.think(
            "agent-1", "ctx", "prompt",
            priority=Priority.HIGH,
        )

        assert resp.metadata["agent_id"] == "agent-1"
        assert resp.metadata["priority"] == "high"


# ---------------------------------------------------------------------------
# Circuit breaker — a WEDGED local model must not pin gate slots forever, and
# after a run of timeouts agents fail fast instead of all blocking on a dead
# server (the "dead model = company idle" incident).
# ---------------------------------------------------------------------------


class TestOpenAICompatCircuitBreaker:
    def _adapter(self, **kw):
        from cortiva.adapters.consciousness.openai_compat import OpenAICompatibleAdapter

        return OpenAICompatibleAdapter(api_key="sk-test", **kw)

    def _ok_response(self):
        usage = MagicMock(); usage.prompt_tokens = 5; usage.completion_tokens = 3
        msg = MagicMock(); msg.content = "ok"; msg.tool_calls = None
        ch = MagicMock(); ch.message = msg
        resp = MagicMock(); resp.choices = [ch]; resp.usage = usage
        return resp

    @pytest.mark.asyncio
    async def test_breaker_trips_after_threshold_then_fails_fast(self) -> None:
        from cortiva.adapters.consciousness.openai_compat import ModelUnavailableError

        adapter = self._adapter(breaker_threshold=3, breaker_cooldown=999.0)
        client = MagicMock()
        client.chat.completions.create.side_effect = TimeoutError("hung")
        adapter._default_client = client

        for _ in range(3):  # three health failures trip the breaker
            with pytest.raises(TimeoutError):
                await adapter.think("a", "c", "p")
        assert adapter.perf_snapshot()["breaker_open"] is True

        # Next call must fail FAST without touching the client.
        client.chat.completions.create.reset_mock()
        with pytest.raises(ModelUnavailableError):
            await adapter.think("a", "c", "p")
        client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_breaker_resets_on_success(self) -> None:
        adapter = self._adapter(breaker_threshold=5)
        client = MagicMock()
        client.chat.completions.create.side_effect = TimeoutError("x")
        adapter._default_client = client
        with pytest.raises(TimeoutError):
            await adapter.think("a", "c", "p")
        assert adapter._breaker_fails == 1

        client.chat.completions.create.side_effect = None
        client.chat.completions.create.return_value = self._ok_response()
        await adapter.think("a", "c", "p")
        assert adapter._breaker_fails == 0
        assert adapter.perf_snapshot()["breaker_open"] is False

    @pytest.mark.asyncio
    async def test_non_health_error_does_not_trip_breaker(self) -> None:
        # A 4xx / validation error is a request problem, not a dead server.
        adapter = self._adapter(breaker_threshold=1)
        client = MagicMock()
        client.chat.completions.create.side_effect = ValueError("bad request")
        adapter._default_client = client
        with pytest.raises(ValueError):
            await adapter.think("a", "c", "p")
        assert adapter._breaker_fails == 0
        assert adapter.perf_snapshot()["breaker_open"] is False

    @pytest.mark.asyncio
    async def test_per_request_timeout_is_passed_to_client(self) -> None:
        adapter = self._adapter(request_timeout=42.0)
        client = MagicMock()
        client.chat.completions.create.return_value = self._ok_response()
        adapter._default_client = client
        await adapter.think("a", "c", "p")
        assert client.chat.completions.create.call_args.kwargs["timeout"] == 42.0
