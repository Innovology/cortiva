"""
In-process channel adapter for Cortiva.

Routes messages between agents using ``asyncio.Queue`` — no external
services required.  Ideal for testing, single-process deployments, and
"walk up to the desk" inter-agent communication.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cortiva.adapters.protocols import Message

logger = logging.getLogger("cortiva.channel.internal")


class InternalChannelAdapter:
    """
    In-process message bus backed by per-agent ``asyncio.Queue`` instances.

    * ``send()``  — enqueue a message for a specific agent or broadcast to a channel
    * ``receive()`` — drain the agent's queue, optionally filtered by ``since``
    * ``listen()`` — subscribe an agent to named channels (for broadcast)

    Thread-safe: each agent gets its own :class:`asyncio.Queue`, and the
    subscription dict is only mutated under an :class:`asyncio.Lock`.

    **Durability.** With ``persist_dir`` set, every direct message is also
    appended to ``<persist_dir>/<recipient>.jsonl`` and removed only when
    the recipient actually receives it. This is what lets a CEO→CFO
    message survive a fabric restart — without it the in-memory queue is
    lost on every bounce, and unread inter-agent messages vanished
    silently (2026-06-07). Broadcasts stay in-memory (ephemeral by
    nature).
    """

    def __init__(self, persist_dir: str | Path | None = None) -> None:
        # agent_id → queue of pending messages
        self._queues: dict[str, asyncio.Queue[Message]] = {}
        # channel_name → set of subscribed agent_ids
        self._subscriptions: dict[str, set[str]] = {}
        # Protects _queues and _subscriptions mutations
        self._lock = asyncio.Lock()
        self._persist_dir = Path(persist_dir) if persist_dir else None
        if self._persist_dir:
            self._persist_dir.mkdir(parents=True, exist_ok=True)

    def _get_queue(self, agent_id: str) -> asyncio.Queue[Message]:
        """Return (and lazily create) the queue for *agent_id*."""
        if agent_id not in self._queues:
            self._queues[agent_id] = asyncio.Queue()
        return self._queues[agent_id]

    # --- Durable inbox (disk) ------------------------------------------

    def _inbox_path(self, agent_id: str) -> Path | None:
        if not self._persist_dir:
            return None
        # agent_id is an internal slug (cpo, cfo…) — safe as a filename,
        # but guard against path tricks regardless.
        safe = agent_id.replace("/", "_").replace("..", "_")
        return self._persist_dir / f"{safe}.jsonl"

    def _persist_append(self, recipient: str, msg: Message) -> None:
        path = self._inbox_path(recipient)
        if path is None:
            return
        try:
            row = {
                "id": msg.id,
                "sender": msg.sender,
                "recipient": msg.recipient,
                "content": msg.content,
                "timestamp": msg.timestamp.isoformat(),
                "thread_id": msg.thread_id,
                "metadata": msg.metadata,
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            logger.warning("Could not persist message to %s", path, exc_info=True)

    def _load_persisted(self, agent_id: str) -> list[Message]:
        path = self._inbox_path(agent_id)
        if path is None or not path.exists():
            return []
        out: list[Message] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                out.append(
                    Message(
                        id=d["id"],
                        sender=d["sender"],
                        recipient=d["recipient"],
                        content=d["content"],
                        timestamp=datetime.fromisoformat(d["timestamp"]),
                        thread_id=d.get("thread_id"),
                        metadata=d.get("metadata") or {},
                    )
                )
        except (OSError, json.JSONDecodeError, KeyError):
            logger.warning("Corrupt inbox %s — skipping", path, exc_info=True)
        return out

    def _rewrite_persisted(self, agent_id: str, remaining: list[Message]) -> None:
        path = self._inbox_path(agent_id)
        if path is None:
            return
        try:
            if not remaining:
                path.unlink(missing_ok=True)
                return
            lines = [
                json.dumps(
                    {
                        "id": m.id,
                        "sender": m.sender,
                        "recipient": m.recipient,
                        "content": m.content,
                        "timestamp": m.timestamp.isoformat(),
                        "thread_id": m.thread_id,
                        "metadata": m.metadata,
                    }
                )
                for m in remaining
            ]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            logger.warning("Could not rewrite inbox %s", path, exc_info=True)

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
                # Direct message — in-memory for the live process, and
                # to disk so it survives a restart until received.
                self._get_queue(recipient).put_nowait(msg)
                self._persist_append(recipient, msg)

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

            # Seed the in-memory queue from disk on first receive after a
            # restart: any persisted message not already queued (by id)
            # is re-enqueued so it's delivered exactly once.
            if self._persist_dir is not None:
                queued_ids = {m.id for m in queue._queue}  # type: ignore[attr-defined]
                for m in self._load_persisted(agent_id):
                    if m.id not in queued_ids:
                        queue.put_nowait(m)

            messages: list[Message] = []
            requeue: list[Message] = []

            # Drain everything currently in the queue
            while not queue.empty():
                try:
                    msg = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if since is not None and msg.timestamp <= since:
                    # Too old — consumed, not requeued
                    continue

                if len(messages) < limit:
                    messages.append(msg)
                else:
                    # Over limit — put back for the next receive() call
                    requeue.append(msg)

            for msg in requeue:
                queue.put_nowait(msg)

            # Disk now holds only what's still pending (the requeued
            # overflow). Delivered messages are removed.
            if self._persist_dir is not None:
                self._rewrite_persisted(agent_id, requeue)

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
