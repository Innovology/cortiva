"""When the OpenAI-compatible server omits ``usage`` (mlx_lm.server does),
the adapter estimates both sides at ~4 chars/token instead of reporting 0 —
the fabric's model meter accounts what each ask carried."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cortiva.adapters.consciousness.openai_compat import OpenAICompatibleAdapter


class _Completions:
    def __init__(self, usage):
        self.usage = usage
        self.sent_chars = 0

    def create(self, **kwargs):
        self.sent_chars = sum(len(str(m.get("content") or "")) for m in kwargs["messages"])
        msg = SimpleNamespace(content="x" * 80, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=self.usage)


def _adapter(usage):
    a = OpenAICompatibleAdapter(model="m", max_concurrency=1)
    completions = _Completions(usage)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    a._get_client = lambda agent_id="": client
    return a, completions


def test_missing_usage_is_estimated_not_zero() -> None:
    a, completions = _adapter(None)
    r = asyncio.run(a.think("cto", "c" * 400, "p" * 40))
    # ~4 chars/token over everything the request carried (the adapter adds
    # its own system framing on top of context + prompt).
    assert completions.sent_chars >= 440
    assert r.tokens_in == completions.sent_chars // 4
    assert r.tokens_out == 20  # 80 // 4


def test_server_usage_is_preferred_when_present() -> None:
    a, _ = _adapter(SimpleNamespace(prompt_tokens=1234, completion_tokens=56, total_tokens=1290))
    r = asyncio.run(a.think("cto", "c" * 400, "p" * 40))
    assert (r.tokens_in, r.tokens_out) == (1234, 56)
