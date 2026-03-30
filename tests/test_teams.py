"""Tests for the Microsoft Teams channel adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cortiva.adapters.channel.teams import TeamsChannelAdapter
from cortiva.adapters.protocols import ChannelAdapter, Message


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_teams_adapter_satisfies_protocol():
    adapter = TeamsChannelAdapter(webhook_url="https://example.com/webhook")
    assert isinstance(adapter, ChannelAdapter)


# ---------------------------------------------------------------------------
# Constructor / mode detection
# ---------------------------------------------------------------------------


def test_webhook_mode_detected():
    adapter = TeamsChannelAdapter(webhook_url="https://example.com/webhook")
    assert not adapter._use_graph


def test_graph_mode_detected():
    adapter = TeamsChannelAdapter(
        client_id="cid", client_secret="csec", tenant_id="tid"
    )
    assert adapter._use_graph


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://env.example.com/webhook")
    adapter = TeamsChannelAdapter()
    assert adapter._webhook_url == "https://env.example.com/webhook"


# ---------------------------------------------------------------------------
# Webhook send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_webhook():
    adapter = TeamsChannelAdapter(webhook_url="https://example.com/webhook")

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    adapter._http_client = mock_client

    msg = await adapter.send("agent-a", "agent-b", "hello teams")

    mock_client.post.assert_awaited_once_with(
        "https://example.com/webhook", json={"text": "hello teams"}
    )
    assert isinstance(msg, Message)
    assert msg.sender == "agent-a"
    assert msg.recipient == "agent-b"
    assert msg.content == "hello teams"
    assert msg.metadata["mode"] == "webhook"


@pytest.mark.asyncio
async def test_send_webhook_raises_without_url():
    adapter = TeamsChannelAdapter()
    adapter._http_client = AsyncMock()

    with pytest.raises(ValueError, match="No webhook URL"):
        await adapter.send("a", "b", "text")


# ---------------------------------------------------------------------------
# Graph API send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_graph():
    adapter = TeamsChannelAdapter(
        client_id="cid",
        client_secret="csec",
        tenant_id="tid",
        default_team_id="team1",
        default_channel_id="chan1",
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={"id": "msg-123"})

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    adapter._http_client = mock_client
    adapter._access_token = "fake-token"
    adapter._token_expires_at = 9999999999.0

    msg = await adapter.send("agent-a", "agent-b", "graph hello")

    assert mock_client.post.await_count == 1
    call_args = mock_client.post.call_args
    assert "/teams/team1/channels/chan1/messages" in call_args[0][0]
    assert msg.id == "msg-123"
    assert msg.metadata["mode"] == "graph"


@pytest.mark.asyncio
async def test_send_graph_with_thread():
    adapter = TeamsChannelAdapter(
        client_id="cid",
        client_secret="csec",
        tenant_id="tid",
        default_team_id="team1",
        default_channel_id="chan1",
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={"id": "reply-1"})

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    adapter._http_client = mock_client
    adapter._access_token = "fake-token"
    adapter._token_expires_at = 9999999999.0

    msg = await adapter.send(
        "agent-a", "agent-b", "reply", thread_id="parent-msg-id"
    )

    call_url = mock_client.post.call_args[0][0]
    assert "/messages/parent-msg-id/replies" in call_url


# ---------------------------------------------------------------------------
# Graph API receive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receive_graph():
    adapter = TeamsChannelAdapter(
        client_id="cid",
        client_secret="csec",
        tenant_id="tid",
        default_team_id="team1",
        default_channel_id="chan1",
    )
    adapter._access_token = "fake-token"
    adapter._token_expires_at = 9999999999.0

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={
        "value": [
            {
                "id": "msg-001",
                "createdDateTime": "2026-01-15T10:00:00Z",
                "from": {"user": {"displayName": "Alice"}},
                "body": {"content": "Hi there"},
            },
            {
                "id": "msg-002",
                "createdDateTime": "2026-01-15T10:01:00Z",
                "from": {"user": {"displayName": "Bob"}},
                "body": {"content": "Hello"},
            },
        ]
    })

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    adapter._http_client = mock_client

    # Subscribe to the default channel
    await adapter.listen("agent-x", ["team1:chan1"])

    msgs = await adapter.receive("agent-x")

    assert len(msgs) == 2
    assert msgs[0].sender == "Alice"
    assert msgs[0].content == "Hi there"
    assert msgs[1].sender == "Bob"


@pytest.mark.asyncio
async def test_receive_skips_sent_messages():
    adapter = TeamsChannelAdapter(
        client_id="cid",
        client_secret="csec",
        tenant_id="tid",
        default_team_id="team1",
        default_channel_id="chan1",
    )
    adapter._access_token = "fake-token"
    adapter._token_expires_at = 9999999999.0
    adapter._sent_ids.add("msg-001")

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={
        "value": [
            {
                "id": "msg-001",
                "createdDateTime": "2026-01-15T10:00:00Z",
                "from": {"user": {"displayName": "Self"}},
                "body": {"content": "my own msg"},
            },
        ]
    })

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    adapter._http_client = mock_client

    await adapter.listen("agent-x", ["team1:chan1"])
    msgs = await adapter.receive("agent-x")
    assert len(msgs) == 0


@pytest.mark.asyncio
async def test_receive_webhook_mode_returns_empty():
    adapter = TeamsChannelAdapter(webhook_url="https://example.com/webhook")
    msgs = await adapter.receive("agent-x")
    assert msgs == []


# ---------------------------------------------------------------------------
# listen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listen_adds_subscriptions():
    adapter = TeamsChannelAdapter(webhook_url="https://example.com/webhook")
    await adapter.listen("agent-a", ["team1:chan1", "team2:chan2"])
    assert adapter._subscriptions["agent-a"] == [("team1", "chan1"), ("team2", "chan2")]


@pytest.mark.asyncio
async def test_listen_no_duplicates():
    adapter = TeamsChannelAdapter(webhook_url="https://example.com/webhook")
    await adapter.listen("agent-a", ["team1:chan1"])
    await adapter.listen("agent-a", ["team1:chan1", "team1:chan2"])
    assert adapter._subscriptions["agent-a"] == [("team1", "chan1"), ("team1", "chan2")]


@pytest.mark.asyncio
async def test_listen_invalid_format():
    adapter = TeamsChannelAdapter(webhook_url="https://example.com/webhook")
    with pytest.raises(ValueError, match="team_id:channel_id"):
        await adapter.listen("agent-a", ["bad-format"])


# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_token_calls_msal():
    adapter = TeamsChannelAdapter(
        client_id="cid", client_secret="csec", tenant_id="tid"
    )

    mock_app = MagicMock()
    mock_app.acquire_token_for_client.return_value = {
        "access_token": "tok-123",
        "expires_in": 3600,
    }

    import sys

    fake_msal = MagicMock()
    fake_msal.ConfidentialClientApplication.return_value = mock_app
    sys.modules["msal"] = fake_msal

    try:
        token = await adapter._ensure_token()
        assert token == "tok-123"
        assert adapter._access_token == "tok-123"
    finally:
        del sys.modules["msal"]


# ---------------------------------------------------------------------------
# resolve_channel
# ---------------------------------------------------------------------------


def test_resolve_channel_explicit():
    adapter = TeamsChannelAdapter(webhook_url="https://example.com/webhook")
    assert adapter._resolve_channel("t1:c1") == ("t1", "c1")


def test_resolve_channel_defaults():
    adapter = TeamsChannelAdapter(
        webhook_url="https://example.com/webhook",
        default_team_id="dt",
        default_channel_id="dc",
    )
    assert adapter._resolve_channel(None) == ("dt", "dc")


def test_resolve_channel_raises_without_defaults():
    adapter = TeamsChannelAdapter(webhook_url="https://example.com/webhook")
    with pytest.raises(ValueError, match="No channel specified"):
        adapter._resolve_channel(None)


# ---------------------------------------------------------------------------
# httpx lazy import
# ---------------------------------------------------------------------------


def test_get_http_client_raises_without_httpx():
    adapter = TeamsChannelAdapter(webhook_url="https://example.com/webhook")
    import sys

    # Temporarily remove httpx from modules if present
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = None  # type: ignore[assignment]
    try:
        with pytest.raises(ImportError, match="httpx is not installed"):
            adapter._get_http_client()
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            del sys.modules["httpx"]
