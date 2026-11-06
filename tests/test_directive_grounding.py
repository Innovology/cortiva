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
    assert calls == [1, 1]
    assert "Now readable" in fabric._directive_salience_context(agent)

    # Read and still open → re-checked each wake (so closure is noticed);
    # read and closed → never asked about again.
    await fabric._ground_directives(agent)
    assert calls == [1, 1, 1]
    saved = json.loads((agent.directory / "directives.json").read_text())
    saved[0]["references"][0]["state"] = "closed"
    (agent.directory / "directives.json").write_text(json.dumps(saved))
    await fabric._ground_directives(agent)
    assert calls == [1, 1, 1]


@pytest.mark.asyncio
async def test_withdrawn_commitment_does_not_close_a_directive(tmp_path: Path) -> None:
    # Only delivery closes a directive; a withdrawal (which a classifier can
    # emit on its reading of a reply) must leave it open.
    from cortiva.core import commitments as cm

    fabric, agent = _fabric_with_directive(tmp_path)
    cid = fabric._open_directives(agent)[0]["commitment_id"]
    cm.update(agent.directory, commitment_id=cid, withdrawn=True)
    assert len(fabric._open_directives(agent)) == 1

    # Delivery with a real artifact (evidence off, the pure path) does close it.
    fabric2, agent2 = _fabric_with_directive(tmp_path / "two")
    cid2 = fabric2._open_directives(agent2)[0]["commitment_id"]
    cm.update(
        agent2.directory,
        commitment_id=cid2,
        delivered=True,
        artifact="https://github.com/Innovology/innovology.io/pull/231",
    )
    assert fabric2._open_directives(agent2) == []


@pytest.mark.asyncio
async def test_a_reply_to_the_originator_does_not_close_a_directive(tmp_path: Path) -> None:
    # 2026-11-05: the CEO's commitment for "Priority Number 1" was delivered
    # by an email back to the founder asking him to paste the issue, and the
    # directive was pruned as handled. Asking a question back is not the work.
    from cortiva.core import commitments as cm

    fabric, agent = _fabric_with_directive(tmp_path)
    cid = fabric._open_directives(agent)[0]["commitment_id"]
    cm.update(
        agent.directory,
        commitment_id=cid,
        delivered=True,
        artifact="ceo/outbox/email/sent/b06ed3ab-founder-reply-priority-number-1.json",
    )
    assert len(fabric._open_directives(agent)) == 1

    # ...and a delivery with no artifact at all is only ever a reply.
    fabric2, agent2 = _fabric_with_directive(tmp_path / "two")
    cid2 = fabric2._open_directives(agent2)[0]["commitment_id"]
    cm.update(agent2.directory, commitment_id=cid2, delivered=True)
    assert len(fabric2._open_directives(agent2)) == 1

    # The commitment names the work, not the mail.
    c = next(x for x in cm.load(agent.directory) if x.id == cid)
    assert URL in c.what and "a reply is not delivery" in c.what


@pytest.mark.asyncio
async def test_a_directive_that_asks_for_a_message_is_delivered_by_one(tmp_path: Path) -> None:
    from cortiva.core import commitments as cm

    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model()
    )
    agent = fabric.register_agent("ceo")
    fabric._record_directives(
        agent,
        [{"from": "alex@innovology.io", "subject": "W45 brief", "text": "Send me the W45 brief."}],
        {"alex@innovology.io": "the founder"},
    )
    cid = fabric._open_directives(agent)[0]["commitment_id"]
    cm.update(
        agent.directory,
        commitment_id=cid,
        delivered=True,
        artifact="ceo/outbox/email/sent/w45-brief.json",
    )
    assert fabric._open_directives(agent) == []


@pytest.mark.asyncio
async def test_a_directive_about_github_work_closes_when_the_issue_does(
    tmp_path: Path, monkeypatch
) -> None:
    # The world decides: while #228 is open the directive is open, whatever
    # the linked commitment says; once it is closed, the directive is handled
    # with no bookkeeping at all.
    from cortiva.core import commitments as cm

    state = {"value": "open"}
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {
            **ref,
            "status": "read",
            "title": "Lead form + lead pipeline",
            "state": state["value"],
            "excerpt": "A lead form on the site and a pipeline screen.",
            "kind": "issue",
        },
    )
    fabric, agent = _fabric_with_directive(tmp_path)
    (agent.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    await fabric._ground_directives(agent)
    cid = fabric._open_directives(agent)[0]["commitment_id"]
    cm.update(
        agent.directory,
        commitment_id=cid,
        delivered=True,
        artifact="https://github.com/Innovology/innovology.io/pull/231",
    )
    assert len(fabric._open_directives(agent)) == 1, "issue still open → directive open"

    # Open references are re-checked on later wakes; a blip never voids them.
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {**ref, "status": "unreadable", "reason": "blip"},
    )
    await fabric._ground_directives(agent)
    saved = json.loads((agent.directory / "directives.json").read_text())
    assert saved[0]["references"][0]["status"] == "read"

    state["value"] = "closed"
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {
            **ref,
            "status": "read",
            "state": "closed",
            "kind": "issue",
        },
    )
    await fabric._ground_directives(agent)
    assert fabric._open_directives(agent) == []


@pytest.mark.asyncio
async def test_execution_context_carries_the_grounded_references(
    tmp_path: Path, monkeypatch
) -> None:
    # The executing session must see the same ⛔/📎 lines as the planner.
    fabric, agent = _fabric_with_directive(tmp_path)
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {**ref, "status": "unreadable", "reason": "no creds"},
    )
    await fabric._ground_directives(agent)
    block = fabric._directive_references_context(agent)
    assert (
        "actually point at" in block
        and "could **NOT** read" in block
        and "treat it as void" in block
    )

    fabric2, agent2 = _fabric_with_directive(tmp_path / "none")
    assert fabric2._directive_references_context(agent2) != ""  # pending refs still listed


@pytest.mark.asyncio
async def test_a_reference_she_cannot_read_is_read_with_a_reports_access(
    tmp_path: Path, monkeypatch
) -> None:
    """2026-11-06 18:42: the CEO, with no GitHub credentials, emailed the founder
    to paste issue #228; the founder: 'ask Samantha and find some way to
    believe her'. The way to believe her is GitHub read through her access —
    the source stays the system, and the agent is told whose access it was."""
    fabric, agent = _fabric_with_directive(tmp_path)
    cto = fabric.register_agent("cto")
    (cto.directory / "credentials.json").write_text('{"GH_TOKEN": "cto-token"}')

    class _Org:
        def subordinates_of(self, aid):
            return ["cto"] if aid == "ceo" else []

        def manager_of(self, aid):
            return "ceo" if aid == "cto" else None

    fabric.org = _Org()

    def _read(ref, token, timeout=8.0):
        if token == "cto-token":
            return {
                **ref,
                "status": "read",
                "title": "Homeschool retirement",
                "state": "open",
                "kind": "issue",
                "excerpt": "Retire the homeschool product.",
            }
        return {**ref, "status": "unreadable", "reason": "this agent has no GitHub credentials"}

    monkeypatch.setattr(grounding, "read_github_ref", _read)
    await fabric._ground_directives(agent)
    saved = json.loads((agent.directory / "directives.json").read_text())
    ref = saved[0]["references"][0]
    assert ref["status"] == "read" and ref["read_via"] == "cto"
    block = fabric._directive_salience_context(agent)
    assert "Homeschool retirement" in block
    assert "read with cto's access" in block
