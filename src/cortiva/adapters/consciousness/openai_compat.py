"""
OpenAI-compatible consciousness adapter for Cortiva.

Works with any OpenAI-compatible API: OpenAI, Azure OpenAI, Kimi,
Together, Groq, Fireworks, etc.  Set the ``base_url`` to point at
any compatible endpoint.

Install: pip install openai
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any

from cortiva.adapters.protocols import ConsciousResponse, Priority

logger = logging.getLogger(__name__)


class ModelUnavailableError(RuntimeError):
    """Raised fast (without calling the model) while the local-model circuit
    breaker is open — a run of timeouts / connection failures tripped it, so
    agents back off instead of all piling long waits onto a wedged server."""

REFLECTION_SUFFIX_INSTRUCTIONS = """\

After completing the task, you may optionally append a structured reflection \
suffix to your response. Place it after your main response, separated by the \
exact delimiter line shown below. The suffix must be valid JSON.

---REFLECTION---
{
  "outcome": "One-sentence summary of what you accomplished",
  "learned": "Key insight or lesson from this task (stored as a memory)",
  "prediction_error": "What surprised you or differed from expectations",
  "messages": [{"to": "agent-id", "content": "message body"}],
  "escalation": "Issue requiring human or supervisor attention"
}

All fields are optional — include only those that apply. \
Do NOT include the reflection suffix if you have nothing meaningful to report."""


class OpenAICompatibleAdapter:
    """
    Consciousness adapter for any OpenAI-compatible API.

    Parameters
    ----------
    model:
        Model name (e.g. ``gpt-4o``, ``moonshot-v1-8k``).
    api_key:
        API key.  Falls back to ``OPENAI_API_KEY`` env var.
    base_url:
        Base URL for the API.  Defaults to OpenAI's endpoint.
        Set to ``https://api.moonshot.cn/v1`` for Kimi, etc.
    max_tokens:
        Default max completion tokens.
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
        per_agent_keys: dict[str, str] | None = None,
        max_concurrency: int = 0,
        request_timeout: float = 0.0,
        breaker_threshold: int = 0,
        breaker_cooldown: float = 0.0,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self._default_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._base_url = base_url
        self._per_agent_keys = per_agent_keys or {}
        self._clients: dict[str, Any] = {}
        self._default_client: Any = None
        # Admission gate: cap concurrent inference so a wake-everyone burst +
        # the agents' own cognitive loops can't all hit the local model at
        # once (the OOM/livelock failure mode). 0 = unlimited. The semaphore is
        # created lazily in the running loop. inflight/queued surface in
        # perf_snapshot for the contention gauge.
        self._max_concurrency = int(max_concurrency or 0)
        self._sem: asyncio.Semaphore | None = None
        self._inflight = 0
        self._queued = 0
        # Per-request deadline + circuit breaker. A local model can WEDGE: keep
        # its socket open and answer /v1/models while every real generation
        # hangs forever (the "dead model = company idle" incident). Without a
        # per-call deadline a hung request pins a gate slot indefinitely (the
        # openai client's own default timeout is ~600s), so every agent blocks
        # and the node falls to ~0 output. We bound each call; after a run of
        # timeouts/connection failures we OPEN a breaker so agents fail fast
        # instead of all piling long waits onto a known-dead server — which also
        # lets the node's model-watchdog restart it cleanly. The timeout is
        # generous (a 35B at ~40 tok/s can legitimately take >2min on a long
        # generation) so real slow work is never mistaken for a wedge.
        self._request_timeout_s = float(request_timeout or 0) or 180.0
        self._breaker_threshold = int(breaker_threshold or 0) or 4
        self._breaker_cooldown_s = float(breaker_cooldown or 0) or 45.0
        self._breaker_fails = 0
        self._breaker_open_until = 0.0
        # Rolling throughput stats for the model behind this adapter — the
        # "how fast is the local model" signal. EWMA tracks recent speed;
        # totals give a lifetime average. Read via perf_snapshot().
        self._perf_calls = 0
        self._perf_tokens_out_total = 0
        self._perf_tokens_in_total = 0
        self._perf_gen_time_total = 0.0  # seconds, summed latency
        self._perf_ewma_tps = 0.0
        self._perf_last_tps = 0.0

    def _record_perf(self, tokens_out: int, latency_ms: float) -> None:
        """Update rolling throughput stats from one completed call."""
        if latency_ms <= 0 or tokens_out <= 0:
            return
        tps = tokens_out / (latency_ms / 1000.0)
        self._perf_calls += 1
        self._perf_tokens_out_total += tokens_out
        self._perf_gen_time_total += latency_ms / 1000.0
        self._perf_last_tps = tps
        # EWMA (alpha=0.3) so the figure tracks recent speed without jitter.
        if self._perf_ewma_tps <= 0:
            self._perf_ewma_tps = tps
        else:
            self._perf_ewma_tps = 0.3 * tps + 0.7 * self._perf_ewma_tps

    def perf_snapshot(self) -> dict[str, Any]:
        """Throughput stats for the model behind this adapter.

        ``tokens_per_second`` is *effective* generation throughput
        (completion tokens / total call latency, so it includes prompt
        processing — an honest lower bound on raw decode speed).
        """
        avg_tps = (
            self._perf_tokens_out_total / self._perf_gen_time_total
            if self._perf_gen_time_total > 0
            else 0.0
        )
        return {
            "model": self.model,
            "tokens_per_second": round(self._perf_ewma_tps, 1),
            "tokens_per_second_avg": round(avg_tps, 1),
            "tokens_per_second_last": round(self._perf_last_tps, 1),
            "calls": self._perf_calls,
            "tokens_out_total": self._perf_tokens_out_total,
            "tokens_in_total": self._perf_tokens_in_total,
            # Admission gate state — how many inferences are running vs waiting
            # behind the K-at-a-time cap (the contention the gate is absorbing).
            "max_concurrency": self._max_concurrency,
            "inflight": self._inflight,
            "queued": self._queued,
            # Circuit-breaker state — surfaces a wedged local model: open means
            # we're failing fast because recent generations timed out.
            "breaker_open": bool(
                self._breaker_open_until and time.monotonic() < self._breaker_open_until
            ),
            "breaker_fails": self._breaker_fails,
        }

    @contextlib.asynccontextmanager
    async def _inference_slot(self):
        """Admit at most ``max_concurrency`` concurrent inferences.

        Bounds the TOTAL in-flight model calls on this node (every agent shares
        this one adapter), so a wake-everyone burst plus the agents' cognitive
        loops can't slam the local model into an OOM or a livelock. Excess
        callers wait here (counted in ``_queued``) until a slot frees. A cap of
        0 disables the gate entirely (remote APIs that don't need it)."""
        if self._max_concurrency <= 0:
            yield
            return
        if self._sem is None:
            # Created here so it binds to the running event loop.
            self._sem = asyncio.Semaphore(self._max_concurrency)
        self._queued += 1
        try:
            await self._sem.acquire()
        finally:
            self._queued -= 1
        self._inflight += 1
        try:
            yield
        finally:
            self._inflight -= 1
            self._sem.release()

    def _get_client(self, agent_id: str = "") -> Any:
        """Return a client for the given agent.

        Per-agent keys get dedicated clients; all others share the
        default client.
        """
        agent_key = self._per_agent_keys.get(agent_id)
        if agent_key:
            if agent_id not in self._clients:
                try:
                    from openai import OpenAI
                except ImportError:
                    raise ImportError(
                        "openai is not installed. "
                        "Install it with: pip install openai"
                    )
                kwargs: dict[str, Any] = {"api_key": agent_key}
                if self._base_url:
                    kwargs["base_url"] = self._base_url
                self._clients[agent_id] = OpenAI(**kwargs)
            return self._clients[agent_id]

        if self._default_client is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise ImportError(
                    "openai is not installed. "
                    "Install it with: pip install openai"
                )
            kwargs2: dict[str, Any] = {"api_key": self._default_key}
            if self._base_url:
                kwargs2["base_url"] = self._base_url
            self._default_client = OpenAI(**kwargs2)
        return self._default_client

    async def think(
        self,
        agent_id: str,
        context: str,
        prompt: str,
        *,
        priority: Priority = Priority.NORMAL,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ConsciousResponse:
        client = self._get_client(agent_id)

        system_prompt = (
            "You are an autonomous agent in an organisation. "
            "Your identity, skills, responsibilities, and current state "
            "are provided in the context below. Act as this agent — "
            "make decisions, complete tasks, and communicate as them.\n\n"
            f"{context}"
        )

        effective_prompt = prompt
        if metadata and metadata.get("task_execution"):
            effective_prompt = prompt + "\n\n" + REFLECTION_SUFFIX_INSTRUCTIONS

        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": effective_prompt},
            ],
        }
        # Native function-calling: hand the model real tool schemas instead
        # of asking it to hand-write a JSON suffix. Far more reliable for
        # structured actions (the model returns validated tool_calls).
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = "auto"

        # Circuit breaker: if a recent run of calls timed out / failed to
        # connect, the local model is almost certainly wedged — fail fast
        # instead of joining 16 other agents each waiting out the full deadline
        # against a dead server. The node's model-watchdog restarts it; this
        # just stops us hammering it meanwhile. Resets on the first success.
        _now = time.monotonic()
        if self._breaker_open_until and _now < self._breaker_open_until:
            raise ModelUnavailableError(
                "local model circuit breaker open "
                f"({self._breaker_open_until - _now:.0f}s remaining) — "
                f"{self._breaker_fails} consecutive inference failures"
            )

        _start = time.monotonic()
        # The OpenAI client is SYNCHRONOUS — calling it directly here blocked
        # the entire fabric event loop for the whole inference (10-30s on a
        # 35B local model). While agents cycled, the fabric's IPC server
        # (status / agent.wake) never got a turn, so wakes hung and HQ 500'd —
        # the "alive but mute" fabric. Offload to a thread so the loop keeps
        # serving IPC and interleaving other work during inference.
        #
        # The admission gate bounds how many of those threads run at once, so
        # the local model is never asked to batch more sequences than its RAM
        # can hold (the OOM/livelock fix). Waiting happens OUTSIDE the thread,
        # so the loop stays responsive while an agent queues for a slot.
        #
        # The per-request ``timeout`` bounds the HTTP call itself so a wedged
        # server can't pin this gate slot forever; the outer wait_for is a
        # belt-and-braces backstop (a cancelled wait_for can't kill the thread,
        # but the client timeout makes the thread return on its own).
        try:
            async with self._inference_slot():
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        lambda: client.chat.completions.create(
                            timeout=self._request_timeout_s, **create_kwargs
                        )
                    ),
                    timeout=self._request_timeout_s + 15.0,
                )
        except Exception as exc:
            # Count only model-HEALTH failures (timeout / connection) toward the
            # breaker — a 4xx/validation error is a request problem, not a dead
            # server, and must not trip it. Name-based check avoids a hard
            # dependency on openai's exception classes.
            _n = type(exc).__name__
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or any(
                k in _n for k in ("Timeout", "APIConnection", "Connection")
            ):
                self._breaker_fails += 1
                if (
                    self._breaker_fails >= self._breaker_threshold
                    and not (self._breaker_open_until and time.monotonic() < self._breaker_open_until)
                ):
                    self._breaker_open_until = time.monotonic() + self._breaker_cooldown_s
                    logger.error(
                        "Local model circuit breaker OPEN after %d consecutive "
                        "inference failures (%s) — backing off %.0fs; the node "
                        "model-watchdog should restart the server.",
                        self._breaker_fails, _n, self._breaker_cooldown_s,
                    )
            raise
        # Success → the model is answering; clear any failure state.
        if self._breaker_fails or self._breaker_open_until:
            self._breaker_fails = 0
            self._breaker_open_until = 0.0
        latency_ms = (time.monotonic() - _start) * 1000.0

        choice = response.choices[0]
        content = choice.message.content or ""
        usage = response.usage
        if usage and (usage.prompt_tokens or 0) > 0:
            self._perf_tokens_in_total += usage.prompt_tokens
        # MLX's OpenAI-compat server often omits `usage`, which left tok/s
        # reporting 0 (no calls ever recorded). Fall back to estimating
        # completion tokens from the content (~4 chars/token) so the throughput
        # gauge isn't blind — an honest approximation beats a permanent zero.
        completion_toks = (usage.completion_tokens if usage else 0) or 0
        if completion_toks <= 0:
            completion_toks = max(1, len(content) // 4)
        self._record_perf(completion_toks, latency_ms)

        tool_calls: list[dict[str, Any]] = []
        raw_calls = getattr(choice.message, "tool_calls", None) or []
        for tc in raw_calls:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            raw_args = getattr(fn, "arguments", "") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {}
            tool_calls.append({"name": getattr(fn, "name", ""), "arguments": args})

        return ConsciousResponse(
            content=content,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            latency_ms=latency_ms,
            model=self.model,
            metadata={"agent_id": agent_id, "priority": priority.value},
            tool_calls=tool_calls,
        )

    async def reflect(
        self,
        agent_id: str,
        context: str,
        day_summary: str,
    ) -> ConsciousResponse:
        prompt = (
            "Your working day is ending. Here is a summary of what happened:\n\n"
            f"{day_summary}\n\n"
            "Please:\n"
            "1. Rewrite your Living Summary (identity.md) to reflect today's "
            "experiences and any changes to how you see yourself and your role.\n"
            "2. Write a brief journal entry noting what you learned, "
            "what went well, and what you'd do differently.\n\n"
            "Format your response as:\n"
            "## Living Summary\n[updated identity]\n\n"
            "## Journal\n[today's reflection]"
        )

        response = await self.think(
            agent_id=agent_id,
            context=context,
            prompt=prompt,
            priority=Priority.NORMAL,
        )

        content = response.content
        reflection = None

        if "## Journal" in content:
            parts = content.split("## Journal", 1)
            content = parts[0].replace("## Living Summary", "").strip()
            reflection = parts[1].strip()

        return ConsciousResponse(
            content=content,
            reflection=reflection,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            model=response.model,
            metadata=response.metadata,
        )
