"""Admission gate: cap concurrent local-model inferences so a wake-everyone
burst can't OOM/livelock the model. The gate lives in the consciousness
adapter (one per node, shared by all agents)."""
from __future__ import annotations

import asyncio

from cortiva.adapters.consciousness.openai_compat import OpenAICompatibleAdapter


def test_gate_caps_concurrency_at_k():
    a = OpenAICompatibleAdapter(model="m", max_concurrency=2)
    peak = 0

    async def worker():
        nonlocal peak
        async with a._inference_slot():
            peak = max(peak, a._inflight)
            await asyncio.sleep(0.03)

    async def run():
        await asyncio.gather(*[worker() for _ in range(8)])

    asyncio.run(run())
    assert peak == 2, f"in-flight exceeded the cap: peak={peak}"
    assert a._inflight == 0 and a._queued == 0  # fully drained


def test_gate_queues_excess():
    a = OpenAICompatibleAdapter(model="m", max_concurrency=1)
    seen_queue = 0

    async def worker():
        nonlocal seen_queue
        async with a._inference_slot():
            # while we hold the only slot, the others must be queued
            seen_queue = max(seen_queue, a._queued)
            await asyncio.sleep(0.02)

    async def run():
        await asyncio.gather(*[worker() for _ in range(4)])

    asyncio.run(run())
    assert seen_queue >= 1  # excess callers waited behind the cap


def test_gate_disabled_is_unbounded():
    a = OpenAICompatibleAdapter(model="m", max_concurrency=0)

    async def worker():
        async with a._inference_slot():
            await asyncio.sleep(0.005)

    async def run():
        await asyncio.gather(*[worker() for _ in range(6)])

    # No cap, no tracking, no error.
    asyncio.run(run())
    assert a._inflight == 0 and a._queued == 0


def test_perf_snapshot_exposes_gate():
    a = OpenAICompatibleAdapter(model="m", max_concurrency=3)
    snap = a.perf_snapshot()
    assert snap["max_concurrency"] == 3
    assert snap["inflight"] == 0
    assert snap["queued"] == 0
