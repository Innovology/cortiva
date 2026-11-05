"""Directives are grounded in what they point at — an unreadable link is
surfaced as unreadable, never filled in from memory (innovology.io#228,
2026-11-05: the CEO had no GitHub credentials and invented the issue's content)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortiva.core import grounding
from cortiva.core.grounding import extract_github_refs, read_github_ref

URL = "https://github.com/Innovology/innovology.io/issues/228"


def test_extract_refs_dedups_and_parses():
    text = f"Please focus on {URL} and also {URL} plus https://github.com/a/b/pull/7"
    refs = extract_github_refs(text)
    assert [r["url"] for r in refs] == [URL, "https://github.com/a/b/pull/7"]
    assert refs[0]["owner"] == "Innovology" and refs[0]["repo"] == "innovology.io"
    assert refs[0]["number"] == 228 and refs[0]["status"] == "pending"


def test_no_token_is_unreadable_with_reason():
    out = read_github_ref(extract_github_refs(URL)[0], token="")
    assert out["status"] == "unreadable" and "no GitHub credentials" in out["reason"]


def test_read_ref_uses_token_and_keeps_title_and_body(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {
                    "title": "Lead form + lead management pipeline",
                    "state": "open",
                    "body": "## Why\nx" * 2,
                }
            ).encode()

    seen = {}

    def _open(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        return _Resp()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    out = read_github_ref(extract_github_refs(URL)[0], token="ghp_x")
    assert out["status"] == "read" and out["title"].startswith("Lead form")
    assert out["kind"] == "issue" and "Why" in out["excerpt"]
    assert seen["auth"] == "Bearer ghp_x" and seen["url"].endswith(
        "/repos/Innovology/innovology.io/issues/228"
    )


def test_http_404_is_unreadable(monkeypatch):
    import urllib.request

    def _open(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    out = read_github_ref(extract_github_refs(URL)[0], token="ghp_x")
    assert out["status"] == "unreadable" and "404" in out["reason"]


# --- fabric path -----------------------------------------------------------

from cortiva.adapters.memory.inmemory import InMemoryAdapter  # noqa: E402
from cortiva.adapters.protocols import ConsciousResponse  # noqa: E402
from cortiva.core.fabric import Fabric  # noqa: E402


class _Model:
    async def think(self, agent_id, context, prompt, **kwargs):
        return ConsciousResponse(content="- [ ] x", tokens_in=1, tokens_out=1, model="m")

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(content="ok", tokens_in=1, tokens_out=1, model="m")


def _fabric_with_directive(tmp_path: Path) -> tuple[Fabric, object]:
    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model()
    )
    agent = fabric.register_agent("ceo")
    fabric._record_directives(
        agent,
        [
            {
                "from": "alex@innovology.io",
                "subject": "Priority Number 1",
                "text": f"Please focus all attention on {URL}",
            }
        ],
        {"alex@innovology.io": "the founder"},
    )
    return fabric, agent


@pytest.mark.asyncio
async def test_unreadable_reference_is_surfaced_not_guessed(tmp_path: Path, monkeypatch) -> None:
    fabric, agent = _fabric_with_directive(tmp_path)
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {
            **ref,
            "status": "unreadable",
            "reason": "this agent has no GitHub credentials",
        },
    )
    await fabric._ground_directives(agent)
    block = fabric._directive_salience_context(agent)

    assert "could **NOT** read this" in block and "no GitHub credentials" in block
    assert "Do not guess" in block and "alex@innovology.io" in block
    assert "treat it as void" in block and "do not mark it delivered" in block
    saved = json.loads((agent.directory / "directives.json").read_text())
    assert saved[0]["references"][0]["status"] == "unreadable"


@pytest.mark.asyncio
async def test_readable_reference_puts_the_real_content_in_front_of_the_agent(
    tmp_path: Path, monkeypatch
) -> None:
    fabric, agent = _fabric_with_directive(tmp_path)
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {
            **ref,
            "status": "read",
            "title": "Lead form + lead management pipeline",
            "state": "open",
            "kind": "issue",
            "excerpt": "A lead form on innovology.io and a pipeline screen.",
        },
    )
    await fabric._ground_directives(agent)
    block = fabric._directive_salience_context(agent)

    assert "Lead form + lead management pipeline" in block and "pipeline screen" in block
    assert "NOT" not in block.split("📎")[1][:200]


@pytest.mark.asyncio
async def test_unreadable_references_are_retried_next_wake(tmp_path: Path, monkeypatch) -> None:
    fabric, agent = _fabric_with_directive(tmp_path)
    calls = []

    def _reader(ref, token, timeout=8.0):
        calls.append(1)
        if len(calls) == 1:
            return {**ref, "status": "unreadable", "reason": "HTTP 401"}
        return {
            **ref,
            "status": "read",
            "title": "Now readable",
            "state": "open",
            "kind": "issue",
            "excerpt": "",
        }

    monkeypatch.setattr(grounding, "read_github_ref", _reader)
    await fabric._ground_directives(agent)
    await fabric._ground_directives(agent)
    await fabric._ground_directives(agent)  # read → no further calls

    assert calls == [1, 1]
    assert "Now readable" in fabric._directive_salience_context(agent)


@pytest.mark.asyncio
async def test_withdrawn_commitment_does_not_close_a_directive(tmp_path: Path) -> None:
    # Only delivery closes a directive; a withdrawal (which a classifier can
    # emit on its reading of a reply) must leave it open.
    from cortiva.core import commitments as cm

    fabric, agent = _fabric_with_directive(tmp_path)
    cid = fabric._open_directives(agent)[0]["commitment_id"]
    cm.update(agent.directory, commitment_id=cid, withdrawn=True)
    assert len(fabric._open_directives(agent)) == 1

    # Delivery (with evidence off, the pure path) does close it.
    fabric2, agent2 = _fabric_with_directive(tmp_path / "two")
    cid2 = fabric2._open_directives(agent2)[0]["commitment_id"]
    cm.update(agent2.directory, commitment_id=cid2, delivered=True)
    assert fabric2._open_directives(agent2) == []
