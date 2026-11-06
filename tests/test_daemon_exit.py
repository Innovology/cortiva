"""A stopped fabric must exit even while executor threads are stuck in long calls."""

from __future__ import annotations

import asyncio
import os
import threading
import time

from cortiva.cli import main as cli_main


def test_exit_daemon_does_not_wait_for_stuck_executor_threads(monkeypatch):
    exited: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: exited.append(code))
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def _stuck():
        started.set()
        time.sleep(30)  # a model HTTP call that will not return for a long time

    fut = loop.run_in_executor(None, _stuck)
    loop.run_until_complete(asyncio.sleep(0))
    assert started.wait(5)

    t0 = time.monotonic()
    cli_main._exit_daemon(loop, executor_timeout_s=0.5)

    assert time.monotonic() - t0 < 5
    assert exited == [0]
    assert not fut.done()  # the stuck thread is still running; we left anyway
