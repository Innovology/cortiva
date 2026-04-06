"""
Microsoft Teams channel adapter for Cortiva.

Supports two modes:

1. **Webhook mode** — supply ``webhook_url`` for outbound-only posting via
   Office 365 Incoming Webhook connectors.  ``receive()`` returns an empty
   list (no inbound API).
2. **Graph API mode** — supply ``client_id``, ``client_secret``, and
   ``tenant_id`` to authenticate with Microsoft Graph and both send and
   receive channel messages.

Install (Graph mode): pip install 'msal>=1.20' 'httpx>=0.24'
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

from cortiva.adapters.protocols import Message


class TeamsChannelAdapter:
    """
    Microsoft Teams adapter implementing the :class:`ChannelAdapter` protocol.

    * ``send()`` → webhook POST **or** Graph ``POST .../messages``
    * ``receive()`` → Graph ``GET .../messages`` (Graph mode only)
    * ``listen()`` → stores channel/team subscriptions in memory
    """

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        tenant_id: str | None = None,
        default_team_id: str | None = None,
        default_channel_id: str | None = None,
    ):
        self._webhook_url = webhook_url or os.environ.get("TEAMS_WEBHOOK_URL")
        self._client_id = client_id or os.environ.get("TEAMS_CLIENT_ID")
        self._client_secret = client_secret or os.environ.get("TEAMS_CLIENT_SECRET")
        self._tenant_id = tenant_id or os.environ.get("TEAMS_TENANT_ID")
        self._default_team_id = default_team_id
        self._default_channel_id = default_channel_id

        self._http_client: Any = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

        # agent_id -> list of (team_id, channel_id) tuples
        self._subscriptions: dict[str, list[tuple[str, str]]] = {}
        # (team_id, channel_id) -> last-seen message id
        self._last_seen: dict[tuple[str, str], str] = {}
        # message ids sent by this adapter (self-loop prevention)
        self._sent_ids: set[str] = set()

    @property
    def _use_graph(self) -> bool:
        return bool(self._client_id and self._client_secret and self._tenant_id)

    def _get_http_client(self) -> Any:
        """Lazy-import and cache an httpx async client."""
        if self._http_client is None:
            try:
                import httpx
            except ImportError:
                raise ImportError(
                    "httpx is not installed. Install it with: pip install 'httpx>=0.24'"
                )
            self._http_client = httpx.AsyncClient()
        return self._http_client

    async def _ensure_token(self) -> str:
        """Obtain or refresh an OAuth2 token via MSAL client credentials flow."""
        import time

        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        try:
            import msal
        except ImportError:
            raise ImportError(
                "msal is not installed. Install it with: pip install 'msal>=1.20'"
            )

        app = msal.ConfidentialClientApplication(
            self._client_id,
            authority=f"https://login.microsoftonline.com/{self._tenant_id}",
            client_credential=self._client_secret,
        )
        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"],
        )
        if "access_token" not in result:
            raise RuntimeError(
                f"Failed to acquire Graph API token: {result.get('error_description', result)}"
            )
        self._access_token = result["access_token"]
        self._token_expires_at = time.time() + result.get("expires_in", 3600) - 60
        return self._access_token

    async def _graph_headers(self) -> dict[str, str]:
        token = await self._ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def send(
        self,
        sender: str,
        recipient: str,
        content: str,
        *,
        channel: str | None = None,
        thread_id: str | None = None,
    ) -> Message:
        """Send a message to a Teams channel via webhook or Graph API."""
        client = self._get_http_client()

        if self._use_graph:
            return await self._send_graph(
                client, sender, recipient, content, channel=channel, thread_id=thread_id,
            )

        url = self._webhook_url
        if not url:
            raise ValueError(
                "No webhook URL or Graph API credentials provided. "
                "Set TEAMS_WEBHOOK_URL or supply client_id/client_secret/tenant_id."
            )

        payload: dict[str, Any] = {"text": content}
        resp = await client.post(url, json=payload)
        resp.raise_for_status()

        msg_id = str(uuid.uuid4())
        self._sent_ids.add(msg_id)
        self._trim_sent_ids()

        return Message(
            id=msg_id,
            sender=sender,
            recipient=recipient,
            content=content,
            timestamp=datetime.now(tz=UTC),
            thread_id=thread_id,
            metadata={"mode": "webhook"},
        )

    async def _send_graph(
        self,
        client: Any,
        sender: str,
        recipient: str,
        content: str,
        *,
        channel: str | None = None,
        thread_id: str | None = None,
    ) -> Message:
        """Send via Microsoft Graph API."""
        team_id, channel_id = self._resolve_channel(channel)
        headers = await self._graph_headers()
        now = datetime.now(tz=UTC)

        body: dict[str, Any] = {
            "body": {
                "contentType": "text",
                "content": content,
            },
        }

        if thread_id:
            url = (
                f"https://graph.microsoft.com/v1.0/teams/{team_id}"
                f"/channels/{channel_id}/messages/{thread_id}/replies"
            )
        else:
            url = (
                f"https://graph.microsoft.com/v1.0/teams/{team_id}"
                f"/channels/{channel_id}/messages"
            )

        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        msg_id = data.get("id", str(uuid.uuid4()))
        self._sent_ids.add(msg_id)
        self._trim_sent_ids()

        return Message(
            id=msg_id,
            sender=sender,
            recipient=recipient,
            content=content,
            timestamp=now,
            thread_id=thread_id or msg_id,
            metadata={"mode": "graph", "team_id": team_id, "channel_id": channel_id},
        )

    async def receive(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[Message]:
        """Poll subscribed Teams channels for new messages (Graph mode only)."""
        if not self._use_graph:
            return []

        client = self._get_http_client()
        headers = await self._graph_headers()
        subs = self._subscriptions.get(agent_id, [])
        if not subs and self._default_team_id and self._default_channel_id:
            subs = [(self._default_team_id, self._default_channel_id)]

        messages: list[Message] = []

        for team_id, channel_id in subs:
            url = (
                f"https://graph.microsoft.com/v1.0/teams/{team_id}"
                f"/channels/{channel_id}/messages?$top={limit}"
            )
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            last_seen = self._last_seen.get((team_id, channel_id))
            newest_id: str | None = None

            for item in data.get("value", []):
                msg_id = item.get("id", "")

                # Skip messages we sent
                if msg_id in self._sent_ids:
                    continue

                # Skip already-seen messages
                if last_seen and msg_id <= last_seen:
                    continue

                created = item.get("createdDateTime", "")
                try:
                    ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    ts = datetime.now(tz=UTC)

                if since and ts < since:
                    continue

                sender_name = (
                    item.get("from", {}).get("user", {}).get("displayName", "unknown")
                )
                body_content = item.get("body", {}).get("content", "")

                messages.append(
                    Message(
                        id=str(uuid.uuid4()),
                        sender=sender_name,
                        recipient=agent_id,
                        content=body_content,
                        timestamp=ts,
                        thread_id=msg_id,
                        metadata={
                            "team_id": team_id,
                            "channel_id": channel_id,
                            "graph_msg_id": msg_id,
                        },
                    )
                )

                if newest_id is None or msg_id > newest_id:
                    newest_id = msg_id

            if newest_id:
                self._last_seen[(team_id, channel_id)] = newest_id

        return messages[:limit]

    async def listen(
        self,
        agent_id: str,
        channels: list[str],
    ) -> None:
        """Subscribe an agent to Teams channels.

        Each entry in *channels* should be ``"team_id:channel_id"``.
        """
        existing = self._subscriptions.setdefault(agent_id, [])
        for ch in channels:
            parts = ch.split(":", 1)
            if len(parts) != 2:
                raise ValueError(
                    f"Expected 'team_id:channel_id' format, got: {ch!r}"
                )
            pair = (parts[0], parts[1])
            if pair not in existing:
                existing.append(pair)

    def _resolve_channel(self, channel: str | None) -> tuple[str, str]:
        """Parse a ``team_id:channel_id`` string or fall back to defaults."""
        if channel and ":" in channel:
            parts = channel.split(":", 1)
            return (parts[0], parts[1])
        if self._default_team_id and self._default_channel_id:
            return (self._default_team_id, self._default_channel_id)
        raise ValueError(
            "No channel specified and no defaults configured. "
            "Pass channel='team_id:channel_id' or set default_team_id/default_channel_id."
        )

    def _trim_sent_ids(self) -> None:
        if len(self._sent_ids) > 500:
            excess = len(self._sent_ids) - 500
            it = iter(self._sent_ids)
            for _ in range(excess):
                self._sent_ids.discard(next(it))
