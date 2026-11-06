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

    def create(self, **kwargs):
        msg = SimpleNamespace(content="x" * 80, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=self.usage)


def _adapter(usage):
    a = OpenAICompatibleAdapter(model="m", max_concurrency=1)
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(usage)))
    a._get_client = lambda agent_id="": client
    return a


def test_missing_usage_is_estimated_not_zero() -> None:
    a = _adapter(None)
    r = asyncio.run(a.think("cto", "c" * 400, "p" * 40))
    assert r.tokens_in == 110  # (400 + 40) // 4
    assert r.tokens_out == 20  # 80 // 4


def test_server_usage_is_preferred_when_present() -> None:
    a = _adapter(SimpleNamespace(prompt_tokens=1234, completion_tokens=56, total_tokens=1290))
    r = asyncio.run(a.think("cto", "c" * 400, "p" * 40))
    assert (r.tokens_in, r.tokens_out) == (1234, 56)
