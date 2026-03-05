"""
IPC for Cortiva — Unix domain socket server and client.

The fabric daemon runs a :class:`FabricServer` that accepts JSON commands
over a Unix socket.  CLI commands use :class:`FabricClient` to send
requests and receive JSON responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger("cortiva.ipc")

# Maximum message size (1 MB)
_MAX_MSG = 1_048_576
_NEWLINE = b"\n"


def default_socket_path() -> Path:
    """Return the default socket path for the current working directory."""
    return Path.cwd() / ".cortiva" / "fabric.sock"


def default_pid_path() -> Path:
    """Return the default PID file path for the current working directory."""
    return Path.cwd() / ".cortiva" / "fabric.pid"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class FabricServer:
    """Unix domain socket server running inside the fabric daemon.

    Commands are newline-delimited JSON objects.  Responses are single
    JSON objects followed by a newline.
    """

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._socket_path: Path | None = None
        self._handlers: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {}

    def register(self, command: str, handler: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        """Register a handler for *command*."""
        self._handlers[command] = handler

    async def start(self, socket_path: Path | None = None) -> None:
        """Start listening on the Unix socket."""
        path = socket_path or default_socket_path()
        self._socket_path = path
        path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket
        if path.exists():
            path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(path)
        )
        # Make socket readable/writable by owner only
        path.chmod(0o600)
        logger.info("IPC server listening on %s", path)

    async def stop(self) -> None:
        """Stop the server and clean up the socket file."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._socket_path and self._socket_path.exists():
            self._socket_path.unlink(missing_ok=True)
            logger.info("IPC socket removed: %s", self._socket_path)

    async def handle_command(self, command: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a command dict to the registered handler."""
        cmd = command.get("command", "")
        handler = self._handlers.get(cmd)
        if handler is None:
            return {"ok": False, "error": f"Unknown command: {cmd}"}
        try:
            return await handler(**{k: v for k, v in command.items() if k != "command"})
        except Exception as exc:
            logger.error("Handler error for %s: %s", cmd, exc, exc_info=True)
            return {"ok": False, "error": str(exc)}

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection."""
        try:
            data = await reader.readuntil(_NEWLINE)
            if len(data) > _MAX_MSG:
                resp = {"ok": False, "error": "Message too large"}
            else:
                try:
                    command = json.loads(data)
                except json.JSONDecodeError:
                    resp = {"ok": False, "error": "Invalid JSON"}
                else:
                    resp = await self.handle_command(command)
            writer.write(json.dumps(resp).encode() + _NEWLINE)
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        except Exception as exc:
            logger.error("Connection error: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class FabricClient:
    """Client used by CLI to send commands to the running daemon."""

    def __init__(self, socket_path: Path | None = None) -> None:
        self._socket_path = socket_path or default_socket_path()

    def is_daemon_running(self) -> bool:
        """Check whether the daemon socket exists."""
        return self._socket_path.exists()

    async def send(self, command: str, **kwargs: Any) -> dict[str, Any]:
        """Send a command and return the response dict."""
        payload = {"command": command, **kwargs}
        reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
        try:
            writer.write(json.dumps(payload).encode() + _NEWLINE)
            await writer.drain()
            data = await reader.readuntil(_NEWLINE)
            return json.loads(data)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def send_sync(self, command: str, **kwargs: Any) -> dict[str, Any]:
        """Synchronous wrapper around :meth:`send` for CLI use."""
        return asyncio.run(self.send(command, **kwargs))


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def write_pid(path: Path | None = None) -> None:
    """Write the current process PID to a file."""
    p = path or default_pid_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()))


def read_pid(path: Path | None = None) -> int | None:
    """Read a PID from file.  Returns None if missing or invalid."""
    p = path or default_pid_path()
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def remove_pid(path: Path | None = None) -> None:
    """Remove the PID file."""
    p = path or default_pid_path()
    p.unlink(missing_ok=True)


def is_pid_alive(pid: int) -> bool:
    """Check whether a process with *pid* is running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
