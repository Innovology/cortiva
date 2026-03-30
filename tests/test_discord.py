"""Tests for the Discord channel adapter (fully mocked — no discord.py needed)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cortiva.adapters.channel.discord import DiscordChannelAdapter


def _make_adapter(token: str = "test-token") -> DiscordChannelAdapter:
    """Create an adapter and inject a mocked discord client."""
    adapter = DiscordChannelAdapter(token=token, default_channel=100)
    adapter._client = MagicMock()
    return adapter


def _make_message(
    msg_id: int,
    author_id: int,
    content: str,
    *,
    bot: bool = False,
    embeds: list | None = None,
) -> MagicMock:
    """Build a mock discord.Message."""
    msg = MagicMock()
    msg.id = msg_id
    msg.content = content
    msg.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.author.bot = bot
    msg.embeds = embeds or []
    return msg


def _cortiva_embed(sender: str, recipient: str = "broadcast") -> MagicMock:
    """Build a mock embed with Cortiva footer metadata."""
    embed = MagicMock()
    embed.footer = MagicMock()
    embed.footer.text = f"cortiva:{sender}:{recipient}"
    return embed


class _AsyncHistoryIter:
    """Async iterator wrapper for channel.history() mocking."""

    def __init__(self, messages: list):
        self._messages = messages
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._index]
        self._index += 1
        return msg


def _setup_channel(adapter: DiscordChannelAdapter, channel_id: int, messages: list):
    """Configure the mocked client to return messages for a channel."""
    ch = MagicMock()
    ch.send = AsyncMock()
    ch.history = MagicMock(return_value=_AsyncHistoryIter(messages))
    adapter._client.get_channel = MagicMock(return_value=ch)
    return ch


class TestDiscordSend:
    @pytest.mark.asyncio
    async def test_send_posts_message(self) -> None:
        adapter = _make_adapter()
        ch = _setup_channel(adapter, 100, [])
        response_msg = MagicMock()
        response_msg.id = 9001
        ch.send.return_value = response_msg

        # Mock discord module for Embed
        mock_discord = MagicMock()
        mock_embed_instance = MagicMock()
        mock_discord.Embed.return_value = mock_embed_instance
        with patch.dict("sys.modules", {"discord": mock_discord}):
            msg = await adapter.send("agent-01", "human", "Hello from agent")

        assert msg.id == "9001"
        assert msg.sender == "agent-01"
        assert msg.content == "Hello from agent"
        assert msg.metadata["channel"] == "100"
        ch.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_tracks_id_for_self_loop(self) -> None:
        adapter = _make_adapter()
        ch = _setup_channel(adapter, 100, [])
        response_msg = MagicMock()
        response_msg.id = 9002
        ch.send.return_value = response_msg

        mock_discord = MagicMock()
        mock_discord.Embed.return_value = MagicMock()
        with patch.dict("sys.modules", {"discord": mock_discord}):
            await adapter.send("agent-01", "human", "hi")

        assert 9002 in adapter._sent_ids

    @pytest.mark.asyncio
    async def test_send_to_explicit_channel(self) -> None:
        adapter = _make_adapter()
        ch = _setup_channel(adapter, 200, [])
        response_msg = MagicMock()
        response_msg.id = 9003
        ch.send.return_value = response_msg

        mock_discord = MagicMock()
        mock_discord.Embed.return_value = MagicMock()
        with patch.dict("sys.modules", {"discord": mock_discord}):
            msg = await adapter.send("agent-01", "human", "hi", channel="200")

        assert msg.metadata["channel"] == "200"

    @pytest.mark.asyncio
    async def test_send_raises_without_channel(self) -> None:
        adapter = DiscordChannelAdapter(token="tok")
        adapter._client = MagicMock()

        mock_discord = MagicMock()
        mock_discord.Embed.return_value = MagicMock()
        with patch.dict("sys.modules", {"discord": mock_discord}):
            with pytest.raises(ValueError, match="No target channel"):
                await adapter.send("agent-01", "human", "hi")


class TestDiscordReceive:
    @pytest.mark.asyncio
    async def test_receive_returns_messages(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["100"])

        msgs = [
            _make_message(1, 1001, "hello"),
            _make_message(2, 1002, "world"),
        ]
        _setup_channel(adapter, 100, msgs)

        result = await adapter.receive("agent-01")
        assert len(result) == 2
        assert result[0].content == "hello"
        assert result[1].content == "world"

    @pytest.mark.asyncio
    async def test_receive_skips_own_sent_ids(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["100"])
        adapter._sent_ids.add(1)

        msgs = [_make_message(1, 1001, "echoed back")]
        _setup_channel(adapter, 100, msgs)

        result = await adapter.receive("agent-01")
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_receive_skips_bot_user_id(self) -> None:
        adapter = _make_adapter()
        adapter._bot_user_id = 5000
        await adapter.listen("agent-01", ["100"])

        msgs = [_make_message(1, 5000, "my own msg")]
        _setup_channel(adapter, 100, msgs)

        result = await adapter.receive("agent-01")
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_receive_skips_non_cortiva_bot_messages(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["100"])

        msgs = [
            _make_message(1, 1001, "human msg"),
            _make_message(2, 2001, "bot msg", bot=True),
        ]
        _setup_channel(adapter, 100, msgs)

        result = await adapter.receive("agent-01")
        assert len(result) == 1
        assert result[0].content == "human msg"

    @pytest.mark.asyncio
    async def test_receive_falls_back_to_default_channel(self) -> None:
        adapter = _make_adapter()
        # No listen() call — should use default_channel
        msgs = [_make_message(1, 1001, "fallback")]
        _setup_channel(adapter, 100, msgs)

        result = await adapter.receive("agent-01")
        assert len(result) == 1
        assert result[0].content == "fallback"

    @pytest.mark.asyncio
    async def test_receive_tracks_last_message_id(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["100"])

        msgs = [_make_message(42, 1001, "first")]
        _setup_channel(adapter, 100, msgs)
        await adapter.receive("agent-01")

        assert adapter._last_message_id[100] == 42


class TestDiscordListen:
    @pytest.mark.asyncio
    async def test_listen_adds_channels(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["100", "200"])
        assert adapter._subscriptions["agent-01"] == [100, 200]

    @pytest.mark.asyncio
    async def test_listen_deduplicates(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-01", ["100"])
        await adapter.listen("agent-01", ["100", "200"])
        assert adapter._subscriptions["agent-01"] == [100, 200]


class TestDiscordInit:
    def test_raises_without_token(self) -> None:
        adapter = DiscordChannelAdapter(token=None)
        import os

        os.environ.pop("DISCORD_BOT_TOKEN", None)

        mock_discord = MagicMock()
        with patch.dict("sys.modules", {"discord": mock_discord}):
            with pytest.raises(ValueError, match="No Discord token"):
                adapter._get_client()

    def test_lazy_import_error(self) -> None:
        adapter = DiscordChannelAdapter(token="test-token")
        with patch.dict("sys.modules", {"discord": None}):
            adapter._client = None
            with pytest.raises((ImportError, ModuleNotFoundError)):
                adapter._get_client()


class TestDiscordInterAgentCommunication:
    """Tests for inter-agent messaging via Cortiva embed metadata."""

    @pytest.mark.asyncio
    async def test_agent_a_sends_agent_b_receives(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-b", ["100"])

        msg = _make_message(
            1, 2001, "hello from A", bot=True,
            embeds=[_cortiva_embed("agent-a")],
        )
        _setup_channel(adapter, 100, [msg])

        result = await adapter.receive("agent-b")
        assert len(result) == 1
        assert result[0].sender == "agent-a"
        assert result[0].content == "hello from A"

    @pytest.mark.asyncio
    async def test_agent_does_not_receive_own_messages_metadata(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-a", ["100"])

        msg = _make_message(
            1, 2001, "my own message", bot=True,
            embeds=[_cortiva_embed("agent-a")],
        )
        _setup_channel(adapter, 100, [msg])

        result = await adapter.receive("agent-a")
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_metadata_recipient_routing(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-a", ["100"])
        await adapter.listen("agent-b", ["100"])

        msg = _make_message(
            1, 2001, "private to B", bot=True,
            embeds=[_cortiva_embed("agent-c", recipient="agent-b")],
        )

        _setup_channel(adapter, 100, [msg])
        msgs_a = await adapter.receive("agent-a")

        _setup_channel(adapter, 100, [msg])
        msgs_b = await adapter.receive("agent-b")

        assert len(msgs_a) == 0
        assert len(msgs_b) == 1
        assert msgs_b[0].content == "private to B"

    @pytest.mark.asyncio
    async def test_addressed_message_delivered_to_target(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-b", ["100"])

        msg = _make_message(1, 1001, "hey @agent-b check this")
        _setup_channel(adapter, 100, [msg])

        result = await adapter.receive("agent-b")
        assert len(result) == 1
        assert result[0].content == "hey @agent-b check this"

    @pytest.mark.asyncio
    async def test_addressed_message_not_delivered_to_others(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-c", ["100"])

        msg = _make_message(1, 1001, "hey @agent-b check this")
        _setup_channel(adapter, 100, [msg])

        result = await adapter.receive("agent-c")
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_unaddressed_message_broadcast_to_all(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-a", ["100"])
        await adapter.listen("agent-b", ["100"])

        msg = _make_message(1, 1001, "general announcement")

        _setup_channel(adapter, 100, [msg])
        msgs_a = await adapter.receive("agent-a")

        _setup_channel(adapter, 100, [msg])
        msgs_b = await adapter.receive("agent-b")

        assert len(msgs_a) == 1
        assert len(msgs_b) == 1

    @pytest.mark.asyncio
    async def test_cortiva_bot_message_not_filtered(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-b", ["100"])

        msg = _make_message(
            1, 2001, "cortiva agent msg", bot=True,
            embeds=[_cortiva_embed("agent-a")],
        )
        _setup_channel(adapter, 100, [msg])

        result = await adapter.receive("agent-b")
        assert len(result) == 1
        assert result[0].sender == "agent-a"

    @pytest.mark.asyncio
    async def test_non_cortiva_bot_still_filtered(self) -> None:
        adapter = _make_adapter()
        await adapter.listen("agent-a", ["100"])

        msg = _make_message(1, 9999, "discord reminder", bot=True)
        _setup_channel(adapter, 100, [msg])

        result = await adapter.receive("agent-a")
        assert len(result) == 0
