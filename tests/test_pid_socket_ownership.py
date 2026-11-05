"""A fabric exiting late must not remove its successor's pid file or socket."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from cortiva.core.ipc import FabricServer, read_pid, remove_pid, write_pid


def test_remove_pid_leaves_a_successors_pid_file(tmp_path: Path) -> None:
    p = tmp_path / "fabric.pid"
    write_pid(p)
    assert read_pid(p) == os.getpid()
    p.write_text(str(os.getpid() + 1))  # a successor took over the file
    remove_pid(p)
    assert p.exists()
    remove_pid(p, pid=os.getpid() + 1)  # the owner may remove it
    assert not p.exists()


def test_stop_leaves_a_successors_socket(tmp_path: Path) -> None:
    sock = tmp_path / "fabric.sock"

    async def run() -> None:
        old = FabricServer()
        await old.start(sock)
        new = FabricServer()
        await new.start(sock)  # successor replaces the socket file at the same path
        assert sock.exists()
        await old.stop()
        assert sock.exists(), "late-exiting fabric removed its successor's socket"
        await new.stop()
        assert not sock.exists()

    asyncio.run(run())
