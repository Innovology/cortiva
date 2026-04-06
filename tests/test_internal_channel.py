"""Tests for InternalChannelAdapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from cortiva.adapters.channel.internal import InternalChannelAdapter
from cortiva.adapters.protocols import ChannelAdapter, Message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter() -> InternalChannelAdapter:
    return InternalChannelAdapter()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_implements_channel_adapter_protocol():
    assert isinstance(InternalChannelAdapter(), ChannelAdapter)


# ---------------------------------------------------------------------------
# Direct messaging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_and_receive_direct(adapter: InternalChannelAdapter):
    msg = await adapter.send("alice", "bob", "hello bob")

    assert isinstance(msg, Message)
    assert msg.sender == "alice"
    assert msg.recipient == "bob"
    assert msg.content == "hello bob"

    received = await adapter.receive("bob")
    assert len(received) == 1
    assert received[0].content == "hello bob"
    assert received[0].sender == "alice"


@pytest.mark.asyncio
async def test_receive_returns_empty_when_no_messages(adapter: InternalChannelAdapter):
    received = await adapter.receive("bob")
    assert received == []


@pytest.mark.asyncio
async def test_receive_drains_queue(adapter: InternalChannelAdapter):
    await adapter.send("alice", "bob", "msg1")
    await adapter.send("alice", "bob", "msg2")

    received = await adapter.receive("bob")
    assert len(received) == 2

    # Second call returns nothing
    received = await adapter.receive("bob")
    assert received == []


@pytest.mark.asyncio
async def test_direct_message_only_reaches_recipient(adapter: InternalChannelAdapter):
    await adapter.send("alice", "bob", "for bob only")

    assert await adapter.receive("alice") == []
    assert await adapter.receive("charlie") == []
    assert len(await adapter.receive("bob")) == 1


# ---------------------------------------------------------------------------
# Broadcast / channel messaging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broadcast_to_channel(adapter: InternalChannelAdapter):
    await adapter.listen("bob", ["general"])
    await adapter.listen("charlie", ["general"])

    await adapter.send("alice", "general", "hi everyone", channel="general")

    bob_msgs = await adapter.receive("bob")
    charlie_msgs = await adapter.receive("charlie")

    assert len(bob_msgs) == 1
    assert bob_msgs[0].content == "hi everyone"
    assert len(charlie_msgs) == 1
    assert charlie_msgs[0].content == "hi everyone"


@pytest.mark.asyncio
async def test_broadcast_excludes_sender(adapter: InternalChannelAdapter):
    await adapter.listen("alice", ["general"])
    await adapter.listen("bob", ["general"])

    await adapter.send("alice", "general", "broadcast", channel="general")

    assert await adapter.receive("alice") == []
    assert len(await adapter.receive("bob")) == 1


@pytest.mark.asyncio
async def test_listen_to_multiple_channels(adapter: InternalChannelAdapter):
    await adapter.listen("bob", ["general", "dev"])

    await adapter.send("alice", "general", "in general", channel="general")
    await adapter.send("alice", "dev", "in dev", channel="dev")

    msgs = await adapter.receive("bob")
    assert len(msgs) == 2
    contents = {m.content for m in msgs}
    assert contents == {"in general", "in dev"}


@pytest.mark.asyncio
async def test_unsubscribed_agent_gets_no_broadcast(adapter: InternalChannelAdapter):
    await adapter.listen("bob", ["general"])

    await adapter.send("alice", "general", "broadcast", channel="general")

    assert await adapter.receive("charlie") == []


# ---------------------------------------------------------------------------
# since filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receive_since_filters_old_messages(adapter: InternalChannelAdapter):
    await adapter.send("alice", "bob", "old message")

    cutoff = datetime.now(tz=UTC)
    # Small delay to ensure the next message is after the cutoff
    await asyncio.sleep(0.01)

    await adapter.send("alice", "bob", "new message")

    received = await adapter.receive("bob", since=cutoff)
    assert len(received) == 1
    assert received[0].content == "new message"


# ---------------------------------------------------------------------------
# limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receive_limit(adapter: InternalChannelAdapter):
    for i in range(5):
        await adapter.send("alice", "bob", f"msg{i}")

    received = await adapter.receive("bob", limit=3)
    assert len(received) == 3

    # Remaining messages are still available
    received = await adapter.receive("bob")
    assert len(received) == 2


# ---------------------------------------------------------------------------
# thread_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_thread_id_preserved(adapter: InternalChannelAdapter):
    await adapter.send("alice", "bob", "threaded", thread_id="t-123")

    received = await adapter.receive("bob")
    assert received[0].thread_id == "t-123"


# ---------------------------------------------------------------------------
# Message fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_message_has_uuid_id(adapter: InternalChannelAdapter):
    msg = await adapter.send("alice", "bob", "hi")
    assert len(msg.id) == 36  # UUID4 format


@pytest.mark.asyncio
async def test_message_timestamp_is_utc(adapter: InternalChannelAdapter):
    msg = await adapter.send("alice", "bob", "hi")
    assert msg.timestamp.tzinfo is not None


@pytest.mark.asyncio
async def test_channel_metadata_set_on_broadcast(adapter: InternalChannelAdapter):
    msg = await adapter.send("alice", "general", "hi", channel="general")
    assert msg.metadata.get("channel") == "general"


@pytest.mark.asyncio
async def test_direct_message_metadata_empty(adapter: InternalChannelAdapter):
    msg = await adapter.send("alice", "bob", "hi")
    assert msg.metadata == {}


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_sends(adapter: InternalChannelAdapter):
    """Multiple agents sending concurrently should not lose messages."""

    async def send_n(sender: str, n: int):
        for i in range(n):
            await adapter.send(sender, "bob", f"{sender}-{i}")

    await asyncio.gather(
        send_n("alice", 50),
        send_n("charlie", 50),
    )

    received = await adapter.receive("bob", limit=200)
    assert len(received) == 100


@pytest.mark.asyncio
async def test_concurrent_broadcast(adapter: InternalChannelAdapter):
    """Broadcast under concurrency delivers to all subscribers."""
    await adapter.listen("bob", ["general"])
    await adapter.listen("charlie", ["general"])

    async def broadcast_n(sender: str, n: int):
        for i in range(n):
            await adapter.send(sender, "general", f"{sender}-{i}", channel="general")

    await asyncio.gather(
        broadcast_n("alice", 20),
        broadcast_n("dave", 20),
    )

    bob_msgs = await adapter.receive("bob")
    charlie_msgs = await adapter.receive("charlie")

    # alice sends 20 (bob+charlie get them), dave sends 20 (bob+charlie get them)
    assert len(bob_msgs) == 40
    assert len(charlie_msgs) == 40


# ---------------------------------------------------------------------------
# Idempotent listen
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_listen_idempotent(adapter: InternalChannelAdapter):
    """Calling listen twice for the same channel does not duplicate messages."""
    await adapter.listen("bob", ["general"])
    await adapter.listen("bob", ["general"])

    await adapter.send("alice", "general", "hi", channel="general")

    received = await adapter.receive("bob")
    assert len(received) == 1
