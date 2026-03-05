"""Tests for IPC server/client and CLI daemon communication."""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core.fabric import Fabric
from cortiva.core.ipc import (
    FabricClient,
    FabricServer,
    default_pid_path,
    default_socket_path,
    is_pid_alive,
    read_pid,
    remove_pid,
    write_pid,
)


def _short_sock() -> Path:
    """Return a short socket path that fits within AF_UNIX limits (104 bytes on macOS)."""
    return Path(tempfile.gettempdir()) / f"cv-{uuid.uuid4().hex[:8]}.sock"


# ---------------------------------------------------------------------------
# FabricServer / FabricClient unit tests
# ---------------------------------------------------------------------------


class TestFabricServer:
    @pytest.mark.asyncio
    async def test_start_creates_socket(self) -> None:
        sock = _short_sock()
        server = FabricServer()
        try:
            await server.start(sock)
            assert sock.exists()
        finally:
            await server.stop()
        assert not sock.exists()

    @pytest.mark.asyncio
    async def test_handle_unknown_command(self) -> None:
        server = FabricServer()
        resp = await server.handle_command({"command": "nonexistent"})
        assert resp["ok"] is False
        assert "Unknown" in resp["error"]

    @pytest.mark.asyncio
    async def test_handle_registered_command(self) -> None:
        server = FabricServer()

        async def _echo(**kwargs):
            return {"ok": True, "echo": kwargs}

        server.register("echo", _echo)
        resp = await server.handle_command({"command": "echo", "msg": "hi"})
        assert resp["ok"] is True
        assert resp["echo"]["msg"] == "hi"

    @pytest.mark.asyncio
    async def test_handler_exception_returns_error(self) -> None:
        server = FabricServer()

        async def _fail(**kwargs):
            raise RuntimeError("boom")

        server.register("fail", _fail)
        resp = await server.handle_command({"command": "fail"})
        assert resp["ok"] is False
        assert "boom" in resp["error"]


class TestFabricClientServer:
    """Integration tests: client sends, server responds."""

    @pytest.mark.asyncio
    async def test_roundtrip(self) -> None:
        sock = _short_sock()
        server = FabricServer()

        async def _ping(**_kw):
            return {"ok": True, "pong": True}

        server.register("ping", _ping)
        await server.start(sock)
        try:
            client = FabricClient(sock)
            assert client.is_daemon_running()

            resp = await client.send("ping")
            assert resp["ok"] is True
            assert resp["pong"] is True
        finally:
            await server.stop()
        assert not client.is_daemon_running()

    @pytest.mark.asyncio
    async def test_unknown_command_roundtrip(self) -> None:
        sock = _short_sock()
        server = FabricServer()
        await server.start(sock)
        try:
            client = FabricClient(sock)
            resp = await client.send("nope")
            assert resp["ok"] is False
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_command_with_kwargs(self) -> None:
        sock = _short_sock()
        server = FabricServer()

        async def _greet(name: str = "", **_kw):
            return {"ok": True, "greeting": f"hello {name}"}

        server.register("greet", _greet)
        await server.start(sock)
        try:
            client = FabricClient(sock)
            resp = await client.send("greet", name="cortiva")
            assert resp["greeting"] == "hello cortiva"
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# PID file tests
# ---------------------------------------------------------------------------


class TestPidFile:
    def test_write_and_read(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        write_pid(pid_file)
        pid = read_pid(pid_file)
        assert pid is not None
        import os
        assert pid == os.getpid()

    def test_read_missing(self, tmp_path: Path) -> None:
        assert read_pid(tmp_path / "nope.pid") is None

    def test_remove(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        write_pid(pid_file)
        assert pid_file.exists()
        remove_pid(pid_file)
        assert not pid_file.exists()

    def test_remove_missing_ok(self, tmp_path: Path) -> None:
        remove_pid(tmp_path / "nope.pid")  # no error

    def test_is_pid_alive_self(self) -> None:
        import os
        assert is_pid_alive(os.getpid()) is True

    def test_is_pid_alive_bogus(self) -> None:
        assert is_pid_alive(999999999) is False


# ---------------------------------------------------------------------------
# Fabric IPC integration
# ---------------------------------------------------------------------------


class MockConsciousness:
    async def think(self, agent_id, context, prompt, **kwargs):
        return ConsciousResponse(
            content=(
                "# Plan\n\n"
                "- [ ] Task one\n"
                "- [ ] Task two\n"
            ),
            tokens_in=50, tokens_out=25, model="mock",
        )

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(
            content=f"# {agent_id}\n\nReflected.",
            tokens_in=50, tokens_out=25, model="mock",
        )


class TestFabricIPC:
    def _make_fabric(self, tmp_path: Path) -> Fabric:
        return Fabric(
            agents_dir=tmp_path / "agents",
            memory=InMemoryAdapter(),
            consciousness=MockConsciousness(),
        )

    @pytest.mark.asyncio
    async def test_status_via_ipc(self, tmp_path: Path) -> None:
        sock = _short_sock()
        fabric = self._make_fabric(tmp_path)
        fabric.register_agent("test-01")
        await fabric.start(ipc_socket=sock)
        try:
            resp = await FabricClient(sock).send("status")
            assert resp["ok"] is True
            assert "test-01" in resp["agents"]
            assert resp["running"] is True
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_agent_wake_via_ipc(self, tmp_path: Path) -> None:
        sock = _short_sock()
        fabric = self._make_fabric(tmp_path)
        fabric.register_agent("test-01")
        await fabric.start(ipc_socket=sock)
        try:
            resp = await FabricClient(sock).send("agent.wake", agent_id="test-01")
            assert resp["ok"] is True
            assert resp["state"] == "executing"
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_agent_sleep_via_ipc(self, tmp_path: Path) -> None:
        sock = _short_sock()
        fabric = self._make_fabric(tmp_path)
        fabric.register_agent("test-01")
        await fabric.start(ipc_socket=sock)
        try:
            await FabricClient(sock).send("agent.wake", agent_id="test-01")
            resp = await FabricClient(sock).send("agent.sleep", agent_id="test-01")
            assert resp["ok"] is True
            assert resp["state"] == "sleeping"
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_agent_cycle_via_ipc(self, tmp_path: Path) -> None:
        sock = _short_sock()
        fabric = self._make_fabric(tmp_path)
        fabric.register_agent("test-01")
        await fabric.start(ipc_socket=sock)
        try:
            await FabricClient(sock).send("agent.wake", agent_id="test-01")
            resp = await FabricClient(sock).send("agent.cycle", agent_id="test-01")
            assert resp["ok"] is True
            assert resp["action"] == "executed_task"
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_shutdown_via_ipc(self, tmp_path: Path) -> None:
        sock = _short_sock()
        fabric = self._make_fabric(tmp_path)
        await fabric.start(ipc_socket=sock)
        try:
            resp = await FabricClient(sock).send("shutdown")
            assert resp["ok"] is True
            await asyncio.sleep(0.1)
            assert fabric._running is False
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_wake_unknown_agent_returns_error(self, tmp_path: Path) -> None:
        sock = _short_sock()
        fabric = self._make_fabric(tmp_path)
        await fabric.start(ipc_socket=sock)
        try:
            resp = await FabricClient(sock).send("agent.wake", agent_id="ghost")
            assert resp["ok"] is False
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_budget_via_ipc(self, tmp_path: Path) -> None:
        sock = _short_sock()
        fabric = self._make_fabric(tmp_path)
        await fabric.start(ipc_socket=sock)
        try:
            resp = await FabricClient(sock).send("budget")
            assert resp["ok"] is True
            assert "budget" in resp
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_socket_cleanup_on_stop(self, tmp_path: Path) -> None:
        sock = _short_sock()
        fabric = self._make_fabric(tmp_path)
        await fabric.start(ipc_socket=sock)
        assert sock.exists()
        await fabric.stop()
        assert not sock.exists()


async def client_send(sock: Path, command: str, **kwargs) -> dict:
    """Helper to send a command via a fresh FabricClient."""
    client = FabricClient(sock)
    return await client.send(command, **kwargs)
