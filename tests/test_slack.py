"""Tests for the Slack channel adapter (fully mocked — no slack_sdk needed)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cortiva.adapters.channel.slack import SlackChannelAdapter


def _make_adapter(token: str = "xoxb-test") -> SlackChannelAdapter:
    """Create an adapter and inject a mocked AsyncWebClient."""
    adapter = SlackChannelAdapter(token=token, default_channel="C001")
    adapter._client = AsyncMock()
    return adapter


class TestSlackSend:
    @pytest.mark.asyncio
    async def test_send_posts_message(self) -> None:
        adapter = _make_adapter()
        adapter._client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1234567890.123456",
        }

        msg = await adapter.send("agent-01", "human", "Hello from agent")
        adapter._client.chat_postMessage.assert_called_once_with(
            channel="C001",
            text="Hello from agent",
            metadata={
                "event_type": "cortiva_agent_message",
                "event_payload": {
                    "sender_agent_id": "agent-01",
                    "recipient": "human",
                },
            },
        )
        assert msg.id == "1234567890.123456"
        assert msg.sender == "agent-01"
        assert msg.content == "Hello from agent"

    @pytest.mark.asyncio
    async def test_send_with_thread(self) -> None:
        adapter = _make_adapter()
        adapter._client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "111.222",
        }

        msg = await adapter.send(
            "agent-01", "human", "reply", thread_id="000.111"
        )
        adapter._client.chat_postMessage.assert_called_once_with(
            channel="C001",
            text="reply",
            metadata={
                "event_type": "cortiva_agent_message",
                "event_payload": {
                    "sender_agent_id": "agent-01",
                    "recipient": "human",
                },
            },
            thread_ts="000.111",
        )
        assert msg.thread_id == "111.222"

    @pytest.mark.asyncio
    async def test_send_to_explicit_channel(self) -> None:
        adapter = _make_adapter()
        adapter._client.chat_postMessage.return_value = {"ok": True, "ts": "1.1"}

        await adapter.send("agent-01", "human", "hi", channel="C999")
        adapter._client.chat_postMessage.assert_called_once_with(
            channel="C999",
            text="hi",
            metadata={
                "event_type": "cortiva_agent_message",
                "event_payload": {
                    "sender_agent_id": "agent-01",
                    "recipient": "human",
                },
            },
        )


class TestSlackReceive:
    @pytest.mark.asyncio
    async def test_receive_returns_messages(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["C001"])

        adapter._client.conversations_history.return_value = {
            "messages": [
                {"ts": "100.1", "user": "U123", "text": "hello"},
                {"ts": "100.2", "user": "U456", "text": "world"},
            ]
        }

        messages = await adapter.receive("agent-01")
        assert len(messages) == 2
        assert messages[0].content == "hello"
        assert messages[0].sender == "U123"
        assert messages[1].content == "world"

    @pytest.mark.asyncio
    async def test_receive_skips_non_cortiva_bot_messages(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["C001"])

        adapter._client.conversations_history.return_value = {
            "messages": [
                {"ts": "100.1", "user": "U123", "text": "human msg"},
                {"ts": "100.2", "bot_id": "B001", "text": "bot msg"},
                {"ts": "100.3", "subtype": "bot_message", "text": "another bot"},
            ]
        }

        messages = await adapter.receive("agent-01")
        assert len(messages) == 1
        assert messages[0].content == "human msg"

    @pytest.mark.asyncio
    async def test_receive_uses_last_seen_timestamp(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["C001"])

        # First poll
        adapter._client.conversations_history.return_value = {
            "messages": [{"ts": "200.1", "user": "U1", "text": "first"}]
        }
        await adapter.receive("agent-01")

        # Second poll should pass oldest=200.1
        adapter._client.conversations_history.return_value = {"messages": []}
        await adapter.receive("agent-01")

        call_kwargs = adapter._client.conversations_history.call_args_list[-1].kwargs
        assert call_kwargs["oldest"] == "200.1"

    @pytest.mark.asyncio
    async def test_receive_falls_back_to_default_channel(self) -> None:
        adapter = _make_adapter()
        # No explicit listen() — should use default_channel
        adapter._client.conversations_history.return_value = {"messages": []}

        await adapter.receive("agent-01")
        adapter._client.conversations_history.assert_called_once()
        call_kwargs = adapter._client.conversations_history.call_args.kwargs
        assert call_kwargs["channel"] == "C001"


class TestSlackListen:
    @pytest.mark.asyncio
    async def test_listen_adds_channels(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["C001", "C002"])
        assert adapter._subscriptions["agent-01"] == ["C001", "C002"]

    @pytest.mark.asyncio
    async def test_listen_deduplicates(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["C001"])
        await adapter.listen("agent-01", ["C001", "C002"])
        assert adapter._subscriptions["agent-01"] == ["C001", "C002"]


class TestSlackInit:
    def test_raises_without_token(self) -> None:
        adapter = SlackChannelAdapter(token=None)
        import os

        os.environ.pop("SLACK_BOT_TOKEN", None)

        # Mock the slack_sdk import so we get past the ImportError
        mock_client_cls = MagicMock()
        mock_module = MagicMock()
        mock_module.AsyncWebClient = mock_client_cls
        fake_modules = {
            "slack_sdk": MagicMock(),
            "slack_sdk.web": MagicMock(),
            "slack_sdk.web.async_client": mock_module,
        }
        with patch.dict("sys.modules", fake_modules):
            with pytest.raises(ValueError, match="No Slack token"):
                adapter._get_client()

    def test_lazy_import_error(self) -> None:
        adapter = SlackChannelAdapter(token="xoxb-test")
        null_modules = {
            "slack_sdk": None,
            "slack_sdk.web": None,
            "slack_sdk.web.async_client": None,
        }
        with patch.dict("sys.modules", null_modules):
            # Force reimport attempt
            adapter._client = None
            with pytest.raises((ImportError, ModuleNotFoundError)):
                adapter._get_client()


def _cortiva_metadata(sender: str, recipient: str = "broadcast") -> dict:
    """Build Cortiva agent metadata for a Slack message."""
    return {
        "event_type": "cortiva_agent_message",
        "event_payload": {
            "sender_agent_id": sender,
            "recipient": recipient,
        },
    }


class TestSlackInterAgentCommunication:
    """Tests for inter-agent messaging via Cortiva metadata."""

    @pytest.mark.asyncio
    async def test_agent_a_sends_agent_b_receives(self) -> None:
        """Agent-B receives a message sent by Agent-A (via metadata)."""
        adapter = _make_adapter()
        await adapter.listen("agent-b", ["C001"])

        adapter._client.conversations_history.return_value = {
            "messages": [
                {
                    "ts": "300.1",
                    "bot_id": "B001",
                    "text": "hello from A",
                    "metadata": _cortiva_metadata("agent-a"),
                },
            ]
        }

        messages = await adapter.receive("agent-b")
        assert len(messages) == 1
        assert messages[0].sender == "agent-a"
        assert messages[0].content == "hello from A"

    @pytest.mark.asyncio
    async def test_agent_does_not_receive_own_messages_metadata(self) -> None:
        """An agent does not see its own messages (metadata self-suppression)."""
        adapter = _make_adapter()
        await adapter.listen("agent-a", ["C001"])

        adapter._client.conversations_history.return_value = {
            "messages": [
                {
                    "ts": "300.2",
                    "bot_id": "B001",
                    "text": "my own message",
                    "metadata": _cortiva_metadata("agent-a"),
                },
            ]
        }

        messages = await adapter.receive("agent-a")
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_self_loop_suppression_via_sent_ts(self) -> None:
        """Messages with a ts tracked in _sent_ts are skipped."""
        adapter = _make_adapter()
        await adapter.listen("agent-a", ["C001"])
        adapter._sent_ts.add("300.3")

        adapter._client.conversations_history.return_value = {
            "messages": [
                {"ts": "300.3", "user": "U001", "text": "echoed back"},
            ]
        }

        messages = await adapter.receive("agent-a")
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_addressed_message_delivered_to_target(self) -> None:
        """A message mentioning @agent-b is delivered to agent-b."""
        adapter = _make_adapter()
        await adapter.listen("agent-b", ["C001"])

        adapter._client.conversations_history.return_value = {
            "messages": [
                {"ts": "400.1", "user": "U001", "text": "hey @agent-b check this"},
            ]
        }

        messages = await adapter.receive("agent-b")
        assert len(messages) == 1
        assert messages[0].content == "hey @agent-b check this"

    @pytest.mark.asyncio
    async def test_addressed_message_not_delivered_to_others(self) -> None:
        """A message mentioning @agent-b is NOT delivered to agent-c."""
        adapter = _make_adapter()
        await adapter.listen("agent-c", ["C001"])

        adapter._client.conversations_history.return_value = {
            "messages": [
                {"ts": "400.2", "user": "U001", "text": "hey @agent-b check this"},
            ]
        }

        messages = await adapter.receive("agent-c")
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_unaddressed_message_broadcast_to_all(self) -> None:
        """A message with no @mentions is delivered to all subscribers."""
        adapter = _make_adapter()
        await adapter.listen("agent-a", ["C001"])
        await adapter.listen("agent-b", ["C001"])

        adapter._client.conversations_history.return_value = {
            "messages": [
                {"ts": "500.1", "user": "U001", "text": "general announcement"},
            ]
        }

        msgs_a = await adapter.receive("agent-a")
        msgs_b = await adapter.receive("agent-b")
        assert len(msgs_a) == 1
        assert len(msgs_b) == 1

    @pytest.mark.asyncio
    async def test_cortiva_bot_message_not_filtered(self) -> None:
        """A bot message with Cortiva metadata passes through (not filtered)."""
        adapter = _make_adapter()
        await adapter.listen("agent-b", ["C001"])

        adapter._client.conversations_history.return_value = {
            "messages": [
                {
                    "ts": "600.1",
                    "bot_id": "B001",
                    "subtype": "bot_message",
                    "text": "cortiva agent msg",
                    "metadata": _cortiva_metadata("agent-a"),
                },
            ]
        }

        messages = await adapter.receive("agent-b")
        assert len(messages) == 1
        assert messages[0].sender == "agent-a"

    @pytest.mark.asyncio
    async def test_non_cortiva_bot_still_filtered(self) -> None:
        """Bot messages without Cortiva metadata remain filtered."""
        adapter = _make_adapter()
        await adapter.listen("agent-a", ["C001"])

        adapter._client.conversations_history.return_value = {
            "messages": [
                {"ts": "600.2", "bot_id": "B999", "text": "slack reminder"},
            ]
        }

        messages = await adapter.receive("agent-a")
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_metadata_recipient_routing(self) -> None:
        """Metadata recipient field routes to specific agent only."""
        adapter = _make_adapter()
        await adapter.listen("agent-a", ["C001"])
        await adapter.listen("agent-b", ["C001"])

        adapter._client.conversations_history.return_value = {
            "messages": [
                {
                    "ts": "700.1",
                    "bot_id": "B001",
                    "text": "private to B",
                    "metadata": _cortiva_metadata("agent-c", recipient="agent-b"),
                },
            ]
        }

        msgs_a = await adapter.receive("agent-a")
        msgs_b = await adapter.receive("agent-b")
        assert len(msgs_a) == 0
        assert len(msgs_b) == 1
        assert msgs_b[0].content == "private to B"

    @pytest.mark.asyncio
    async def test_send_attaches_metadata(self) -> None:
        """send() includes Cortiva metadata in the Slack API call."""
        adapter = _make_adapter()
        adapter._client.chat_postMessage.return_value = {"ok": True, "ts": "800.1"}

        await adapter.send("agent-a", "agent-b", "hi B")

        call_kwargs = adapter._client.chat_postMessage.call_args.kwargs
        assert call_kwargs["metadata"] == {
            "event_type": "cortiva_agent_message",
            "event_payload": {
                "sender_agent_id": "agent-a",
                "recipient": "agent-b",
            },
        }

    @pytest.mark.asyncio
    async def test_send_tracks_ts_in_sent_ts(self) -> None:
        """send() records the response ts in _sent_ts for self-loop prevention."""
        adapter = _make_adapter()
        adapter._client.chat_postMessage.return_value = {"ok": True, "ts": "900.1"}

        await adapter.send("agent-a", "agent-b", "hello")
        assert "900.1" in adapter._sent_ts
