"""Opt-in JSONL log of every local-model call — what each agent asked and got back."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cortiva.adapters.consciousness.openai_compat import OpenAICompatibleAdapter
from cortiva.core.config import _build_consciousness_adapter


class _Completions:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def create(self, **kwargs: Any) -> Any:
        if self.fail:
            raise TimeoutError("model wedged")
        msg = SimpleNamespace(content="- [ ] Ship the lead form", tool_calls=None)
        usage = SimpleNamespace(prompt_tokens=120, completion_tokens=8)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def _adapter(tmp_path: Path, *, fail: bool = False, log: bool = True) -> OpenAICompatibleAdapter:
    a = OpenAICompatibleAdapter(
        model="m", prompt_log=str(tmp_path / "inference.jsonl") if log else None
    )
    a._default_client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(fail)))
    return a


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (tmp_path / "inference.jsonl").read_text().splitlines()]


def test_successful_call_is_logged(tmp_path: Path) -> None:
    a = _adapter(tmp_path)
    resp = asyncio.run(
        a.think("ceo", "IDENTITY: Maren", "Plan your day", metadata={"call_type": "plan_daily"})
    )

    [r] = _records(tmp_path)
    assert (r["agent"], r["call_type"], r["prompt"]) == ("ceo", "plan_daily", "Plan your day")
    assert r["reply"] == "- [ ] Ship the lead form"
    assert r["tokens_in"] == 120 and r["context_chars"] > 0
    assert resp.metadata["agent_id"] == "ceo"


def test_failed_call_is_logged_and_still_raises(tmp_path: Path) -> None:
    a = _adapter(tmp_path, fail=True)
    with pytest.raises(TimeoutError):
        asyncio.run(a.think("cto", "ctx", "Plan your day"))

    [r] = _records(tmp_path)
    assert r["agent"] == "cto" and "TimeoutError" in r["error"]


def test_log_is_off_by_default(tmp_path: Path) -> None:
    a = _adapter(tmp_path, log=False)
    asyncio.run(a.think("ceo", "ctx", "Plan"))
    assert not (tmp_path / "inference.jsonl").exists()


def test_config_wires_prompt_log(tmp_path: Path) -> None:
    con = _build_consciousness_adapter(
        {
            "provider": "openai-compatible",
            "base_url": "http://127.0.0.1:9100/v1",
            "model": "m",
            "prompt_log": str(tmp_path / "x.jsonl"),
        }
    )
    assert con._prompt_log == tmp_path / "x.jsonl"
