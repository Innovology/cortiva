"""
Slack channel adapter for Cortiva.

Uses ``slack_sdk.web.async_client.AsyncWebClient`` in a polling model that
matches the pull-based :class:`ChannelAdapter` protocol.  No Bolt, no HTTP
server — the fabric's heartbeat drives message retrieval.

Install: pip install 'slack-sdk>=3.0'
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

from cortiva.adapters.protocols import Message


class SlackChannelAdapter:
    """
    Polling-based Slack adapter.

    * ``send()`` → ``chat_postMessage``
    * ``receive()`` → ``conversations_history`` (new messages since last check)
    * ``listen()`` → stores channel subscriptions in memory

    Bot messages are skipped automatically to prevent loops.
    """

    def __init__(
        self,
        token: str | None = None,
        default_channel: str | None = None,
    ):
        self._token = token or os.environ.get("SLACK_BOT_TOKEN")
        self._default_channel = default_channel
        self._client: Any = None

        # agent_id → list of channel IDs
        self._subscriptions: dict[str, list[str]] = {}
        # channel_id → last-seen message timestamp (for pagination)
        self._last_ts: dict[str, str] = {}

    def _get_client(self) -> Any:
        """Lazy-import and cache the async Slack client."""
        if self._client is None:
            try:
                from slack_sdk.web.async_client import AsyncWebClient
            except ImportError:
                raise ImportError(
                    "slack-sdk is not installed. "
                    "Install it with: pip install 'slack-sdk>=3.0'"
                )
            if not self._token:
                raise ValueError(
                    "No Slack token provided. Set SLACK_BOT_TOKEN or pass token= to the adapter."
                )
            self._client = AsyncWebClient(token=self._token)
        return self._client

    async def send(
        self,
        sender: str,
        recipient: str,
        content: str,
        *,
        channel: str | None = None,
        thread_id: str | None = None,
    ) -> Message:
        """Post a message to a Slack channel."""
        client = self._get_client()
        target = channel or self._default_channel or recipient

        kwargs: dict[str, Any] = {
            "channel": target,
            "text": content,
        }
        if thread_id:
            kwargs["thread_ts"] = thread_id

        response = await client.chat_postMessage(**kwargs)

        return Message(
            id=response["ts"],
            sender=sender,
            recipient=recipient,
            content=content,
            timestamp=datetime.now(tz=UTC),
            thread_id=response.get("ts"),
            metadata={"channel": target},
        )

    async def receive(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[Message]:
        """Poll subscribed channels for new messages."""
        client = self._get_client()
        channels = self._subscriptions.get(agent_id, [])
        if not channels and self._default_channel:
            channels = [self._default_channel]

        messages: list[Message] = []

        for ch in channels:
            kwargs: dict[str, Any] = {
                "channel": ch,
                "limit": limit,
            }
            oldest = self._last_ts.get(ch)
            if oldest:
                kwargs["oldest"] = oldest

            response = await client.conversations_history(**kwargs)

            for msg in response.get("messages", []):
                # Skip bot messages to avoid loops
                if msg.get("bot_id") or msg.get("subtype") == "bot_message":
                    continue

                ts = msg["ts"]
                messages.append(
                    Message(
                        id=str(uuid.uuid4()),
                        sender=msg.get("user", "unknown"),
                        recipient=agent_id,
                        content=msg.get("text", ""),
                        timestamp=datetime.fromtimestamp(float(ts), tz=UTC),
                        thread_id=msg.get("thread_ts"),
                        metadata={"channel": ch, "slack_ts": ts},
                    )
                )

                # Track the latest timestamp we've seen for this channel
                if not oldest or ts > oldest:
                    oldest = ts

            if oldest:
                self._last_ts[ch] = oldest

        return messages

    async def listen(
        self,
        agent_id: str,
        channels: list[str],
    ) -> None:
        """Subscribe an agent to one or more Slack channels."""
        existing = self._subscriptions.setdefault(agent_id, [])
        for ch in channels:
            if ch not in existing:
                existing.append(ch)
