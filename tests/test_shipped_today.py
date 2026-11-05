"""An agent's task sessions know what she has already put into the world
today — sent mail and opened PRs — so they build on it instead of shipping
it again (2026-11-05: the CTO opened the same CI fix four times in an hour)."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core import grounding
from cortiva.core.agent import Task
from cortiva.core.fabric import Fabric


class _Model:
    async def think(self, agent_id, context, prompt, **kwargs):
        return ConsciousResponse(content="- [ ] x", tokens_in=1, tokens_out=1, model="m")

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(content="ok", tokens_in=1, tokens_out=1, model="m")


def _fabric(tmp_path: Path):
    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model()
    )
    return fabric, fabric.register_agent("cto")


def _sent(agent, name: str, to: str, subject: str, *, days_ago: int = 0) -> None:
    d = Path(agent.directory) / "outbox" / "email" / "sent"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps({"to": to, "subject": subject, "body": "…"}))
    if days_ago:
        ts = (datetime.now(UTC) - timedelta(days=days_ago)).timestamp()
        os.utime(p, (ts, ts))


def test_nothing_shipped_means_no_block(tmp_path: Path) -> None:
    fabric, agent = _fabric(tmp_path)
    assert fabric._shipped_today_context(agent) == ""


def test_todays_sent_mail_is_listed_and_older_mail_is_not(tmp_path: Path) -> None:
    fabric, agent = _fabric(tmp_path)
    _sent(agent, "a", "alex@innovology.io", "Re: Priority Number 1")
    _sent(agent, "b", "maren@workforce.innovology.io", "MarketMesh halt", days_ago=3)
    block = fabric._shipped_today_context(agent)
    assert "Re: Priority Number 1" in block and "alex@innovology.io" in block
    assert "MarketMesh halt" not in block
    assert "do not send or open these again" in block
    assert "may still have gone through" in block


@pytest.mark.asyncio
async def test_prs_opened_today_reach_both_execution_prompts(tmp_path: Path, monkeypatch) -> None:
    fabric, agent = _fabric(tmp_path)
    (agent.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    calls: list[tuple[str, str]] = []

    def _prs(token, since, timeout=8.0):
        calls.append((token, since))
        return [
            {
                "repo": "Innovology/innovology.io",
                "number": 229,
                "title": "fix(ci): enforce 20-char minimum on AI Delegation description",
                "state": "open",
                "url": "https://github.com/Innovology/innovology.io/pull/229",
            }
        ]

    monkeypatch.setattr(grounding, "prs_opened_since", _prs)
    await fabric._refresh_shipped_today(agent)
    assert calls == [("t", datetime.now(UTC).date().isoformat())]

    task = Task(id="t1", description="Ship the AI delegation CI gate", priority=1)
    ctx = fabric._terminal_context_for_session(agent, task)
    assert "innovology.io#229" in ctx["prompt"] and "pull/229" in ctx["prompt"]
    assert "never make a second copy" in ctx["prompt"]

    # The lookup is cached for a few minutes — one bounded call, not one per task.
    await fabric._refresh_shipped_today(agent)
    assert len(calls) == 1
    fabric._shipped_today[agent.id] = (time.monotonic() - 1000, None)
    await fabric._refresh_shipped_today(agent)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unknown_prs_are_not_presented_as_none(tmp_path: Path, monkeypatch) -> None:
    fabric, agent = _fabric(tmp_path)  # no token → prs_opened_since returns None
    await fabric._refresh_shipped_today(agent)
    assert fabric._shipped_today[agent.id][1] is None
    assert fabric._shipped_today_context(agent) == ""


def test_prs_opened_since_without_a_token_is_unknown() -> None:
    assert grounding.prs_opened_since("", "2026-11-05") is None


def test_prs_opened_since_parses_the_search_result(monkeypatch) -> None:
    def _get(url, token, timeout):
        if url.endswith("/user"):
            return {"login": "samantha-innovology"}
        assert "author%3Asamantha-innovology" in url and "created%3A%3E%3D2026-11-05" in url
        return {
            "items": [
                {
                    "number": 230,
                    "title": "fix(ci): require ≥20-char AI delegation description",
                    "state": "open",
                    "html_url": "https://github.com/Innovology/innovology.io/pull/230",
                    "repository_url": "https://api.github.com/repos/Innovology/innovology.io",
                }
            ]
        }

    monkeypatch.setattr(grounding, "_github_get", _get)
    prs = grounding.prs_opened_since("t", "2026-11-05")
    assert prs == [
        {
            "repo": "Innovology/innovology.io",
            "number": 230,
            "title": "fix(ci): require ≥20-char AI delegation description",
            "state": "open",
            "url": "https://github.com/Innovology/innovology.io/pull/230",
        }
    ]
