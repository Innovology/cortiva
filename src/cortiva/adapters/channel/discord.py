"""
Discord channel adapter for Cortiva.

Uses ``discord.py`` (``discord.Client``) in a polling model that matches the
pull-based :class:`ChannelAdapter` protocol.  The bot connects on first use
and caches recent messages for ``receive()`` to consume.

Install: pip install 'discord.py>=2.0'
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from cortiva.adapters.protocols import Message


class DiscordChannelAdapter:
    """
    Polling-based Discord adapter.

    * ``send()`` → send message to a channel or DM
    * ``receive()`` → fetch recent messages from subscribed channels
    * ``listen()`` → store channel subscriptions in memory

    Own messages are skipped automatically to prevent loops.
    """

    def __init__(
        self,
        token: str | None = None,
        default_channel: int | None = None,
    ):
        self._token = token or os.environ.get("DISCORD_BOT_TOKEN")
        self._default_channel = default_channel
        self._client: Any = None
        self._bot_user_id: int | None = None

        # agent_id → list of channel IDs (ints)
        self._subscriptions: dict[str, list[int]] = {}
        # channel_id → last-seen message ID (for pagination)
        self._last_message_id: dict[int, int] = {}
        # message IDs sent by this adapter (self-loop prevention)
        self._sent_ids: set[int] = set()

    def _get_client(self) -> Any:
        """Lazy-import and cache the discord.py client."""
        if self._client is None:
            try:
                import discord
            except ImportError:
                raise ImportError(
                    "discord.py is not installed. "
                    "Install it with: pip install 'discord.py>=2.0'"
                )
            if not self._token:
                raise ValueError(
                    "No Discord token provided. "
                    "Set DISCORD_BOT_TOKEN or pass token= to the adapter."
                )
            intents = discord.Intents.default()
            intents.message_content = True
            self._client = discord.Client(intents=intents)
        return self._client

    def _get_cortiva_metadata(self, message: Any) -> dict[str, str | None]:
        """Extract Cortiva routing metadata from message embeds.

        Footer format: ``cortiva:<sender>:<recipient>``
        """
        for embed in getattr(message, "embeds", []):
            footer = getattr(embed, "footer", None)
            text = getattr(footer, "text", None) or ""
            if text.startswith("cortiva:"):
                parts = text.split(":", 3)
                sender = parts[1] if len(parts) >= 2 else None
                if sender is not None:
                    recipient = parts[2] if len(parts) >= 3 else "broadcast"
                    return {"sender": sender, "recipient": recipient}
        return {}

    def _is_addressed_to(
        self,
        agent_id: str,
        text: str,
        cortiva_meta: dict[str, str | None],
    ) -> bool:
        """Decide whether *agent_id* should see this message."""
        sender = cortiva_meta.get("sender")
        if sender is not None and sender == agent_id:
            return False

        recipient = cortiva_meta.get("recipient")
        if recipient and recipient != "broadcast" and recipient != agent_id:
            return False

        mentions = re.findall(r"@([\w-]+)", text)
        if mentions:
            return agent_id in mentions

        return True

    async def send(
        self,
        sender: str,
        recipient: str,
        content: str,
        *,
        channel: str | None = None,
        thread_id: str | None = None,
    ) -> Message:
        """Send a message to a Discord channel."""
        try:
            import discord
        except ImportError:
            raise ImportError(
                "discord.py is not installed. "
                "Install it with: pip install 'discord.py>=2.0'"
            )
        client = self._get_client()

        target_id = int(channel) if channel else self._default_channel
        if target_id is None:
            raise ValueError(
                "No target channel specified. "
                "Pass channel= or set default_channel on the adapter."
            )

        ch = client.get_channel(target_id)
        if ch is None:
            raise ValueError(f"Channel {target_id} not found in client cache.")

        embed = discord.Embed(description="")
        embed.set_footer(text=f"cortiva:{sender}:{recipient}")

        response = await ch.send(content=content, embed=embed)

        self._sent_ids.add(response.id)
        if len(self._sent_ids) > 500:
            excess = len(self._sent_ids) - 500
            it = iter(self._sent_ids)
            for _ in range(excess):
                self._sent_ids.discard(next(it))

        return Message(
            id=str(response.id),
            sender=sender,
            recipient=recipient,
            content=content,
            timestamp=datetime.now(tz=UTC),
            thread_id=str(response.id),
            metadata={"channel": str(target_id)},
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

        for ch_id in channels:
            ch = client.get_channel(ch_id)
            if ch is None:
                continue

            kwargs: dict[str, Any] = {"limit": limit}
            after_id = self._last_message_id.get(ch_id)
            if after_id is not None:
                try:
                    import discord

                    kwargs["after"] = discord.Object(id=after_id)
                except ImportError:
                    pass

            history: list[Any] = []
            async for msg in ch.history(**kwargs):
                history.append(msg)

            for msg in history:
                if msg.id in self._sent_ids:
                    continue
                if self._bot_user_id is not None and msg.author.id == self._bot_user_id:
                    continue

                cortiva_meta = self._get_cortiva_metadata(msg)

                if not cortiva_meta and getattr(msg.author, "bot", False):
                    continue
                if not self._is_addressed_to(agent_id, msg.content, cortiva_meta):
                    continue

                msg_sender = cortiva_meta.get("sender") or str(msg.author.id)

                messages.append(
                    Message(
                        id=str(uuid.uuid4()),
                        sender=msg_sender,
                        recipient=agent_id,
                        content=msg.content,
                        timestamp=msg.created_at.replace(tzinfo=UTC),
                        thread_id=str(msg.id),
                        metadata={"channel": str(ch_id), "discord_msg_id": msg.id},
                    )
                )

                if after_id is None or msg.id > after_id:
                    after_id = msg.id

            if after_id is not None:
                self._last_message_id[ch_id] = after_id

        return messages

    async def listen(
        self,
        agent_id: str,
        channels: list[str],
    ) -> None:
        """Subscribe an agent to one or more Discord channels."""
        existing = self._subscriptions.setdefault(agent_id, [])
        for ch in channels:
            ch_id = int(ch)
            if ch_id not in existing:
                existing.append(ch_id)
