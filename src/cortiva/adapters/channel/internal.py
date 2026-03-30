"""
In-process channel adapter for Cortiva.

Routes messages between agents using ``asyncio.Queue`` — no external
services required.  Ideal for testing, single-process deployments, and
"walk up to the desk" inter-agent communication.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from cortiva.adapters.protocols import Message


class InternalChannelAdapter:
    """
    In-process message bus backed by per-agent ``asyncio.Queue`` instances.

    * ``send()``  — enqueue a message for a specific agent or broadcast to a channel
    * ``receive()`` — drain the agent's queue, optionally filtered by ``since``
    * ``listen()`` — subscribe an agent to named channels (for broadcast)

    Thread-safe: each agent gets its own :class:`asyncio.Queue`, and the
    subscription dict is only mutated under an :class:`asyncio.Lock`.
    """

    def __init__(self) -> None:
        # agent_id → queue of pending messages
        self._queues: dict[str, asyncio.Queue[Message]] = {}
        # channel_name → set of subscribed agent_ids
        self._subscriptions: dict[str, set[str]] = {}
        # Protects _queues and _subscriptions mutations
        self._lock = asyncio.Lock()

    def _get_queue(self, agent_id: str) -> asyncio.Queue[Message]:
        """Return (and lazily create) the queue for *agent_id*."""
        if agent_id not in self._queues:
            self._queues[agent_id] = asyncio.Queue()
        return self._queues[agent_id]

    async def send(
        self,
        sender: str,
        recipient: str,
        content: str,
        *,
        channel: str | None = None,
        thread_id: str | None = None,
    ) -> Message:
        """Send a message to a specific agent or broadcast to a channel."""
        msg = Message(
            id=str(uuid.uuid4()),
            sender=sender,
            recipient=recipient,
            content=content,
            timestamp=datetime.now(tz=UTC),
            thread_id=thread_id,
            metadata={"channel": channel} if channel else {},
        )

        async with self._lock:
            if channel:
                # Broadcast: deliver to every subscriber of this channel
                subscribers = self._subscriptions.get(channel, set())
                for agent_id in subscribers:
                    if agent_id != sender:
                        self._get_queue(agent_id).put_nowait(msg)
            else:
                # Direct message
                self._get_queue(recipient).put_nowait(msg)

        return msg

    async def receive(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[Message]:
        """Drain pending messages for *agent_id*, optionally filtered by *since*."""
        async with self._lock:
            queue = self._get_queue(agent_id)

        messages: list[Message] = []
        requeue: list[Message] = []

        # Drain everything currently in the queue
        while not queue.empty():
            try:
                msg = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if since is not None and msg.timestamp <= since:
                # Too old — skip (don't requeue, these are consumed)
                continue

            if len(messages) < limit:
                messages.append(msg)
            else:
                # Over limit — put back for the next receive() call
                requeue.append(msg)

        for msg in requeue:
            queue.put_nowait(msg)

        return messages

    async def listen(
        self,
        agent_id: str,
        channels: list[str],
    ) -> None:
        """Subscribe *agent_id* to the given broadcast channels."""
        async with self._lock:
            # Ensure the agent's queue exists
            self._get_queue(agent_id)
            for ch in channels:
                self._subscriptions.setdefault(ch, set()).add(agent_id)
