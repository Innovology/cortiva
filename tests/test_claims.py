"""What a colleague states about a pull request is read back from GitHub,
put to them when false, and kept against them. One person told their manager
a pull request was "reviewed, approved and merged" when it was none of the
three (Mini-2, 2026-11-18)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core import claims, ledger
from cortiva.core.agent import TaskQueue
from cortiva.core.fabric import Fabric

_OPEN = {
    "repo": "o/r",
    "number": 345,
    "state": "open",
    "merged": False,
    "reviewers": [],
    "decisions": {},
    "failing": [],
    "running": False,
}


def test_statements_are_read_out_of_a_message_and_requests_are_not() -> None:
    said = claims.extract("Approved: PR #345\n\nPR #345 is reviewed, approved and merged.")
    assert sorted(k for _, _, k in said) == ["approved", "merged", "reviewed"]
    assert claims.extract("Please approve PR #346 once CI is green.") == []
    assert claims.extract("PR #350 needs to be merged before the numbers read.") == []
    assert claims.extract("Is #349 merged?") == []
    assert claims.extract("PR #346 is not approved yet.") == []
    assert claims.extract("o/r#349 merged, checks all green.") == [
        ("o/r", 349, "merged"),
        ("o/r", 349, "green"),
    ]


def test_what_was_stated_is_held_against_what_github_shows() -> None:
    assert not claims.holds("merged", _OPEN)
    assert not claims.holds("approved", _OPEN)
    assert not claims.holds("reviewed", _OPEN)
    assert claims.holds("green", _OPEN)
    assert claims.shows(_OPEN) == "open, no approval, no review"
    done = {**_OPEN, "merged": True, "state": "closed"}
    assert all(claims.holds(k, done) for k in claims.SAID)
    approved = {**_OPEN, "reviewers": ["yuki"], "decisions": {"yuki": ("APPROVED", "t")}}
    assert claims.holds("approved", approved) and not claims.holds("merged", approved)


def test_each_persons_record_counts_what_was_checked_and_what_was_false(tmp_path: Path) -> None:
    for kind, true in (("merged", False), ("approved", False), ("green", True)):
        claims.record(
            tmp_path, by="cto", to="ceo", ref="o/r#345", kind=kind, true=true, shown="open"
        )
    row = claims.standing(tmp_path)["cto"]
    assert (row["checked"], row["false"]) == (3, 2) and row["latest"]["kind"] == "approved"
    assert claims.unseen(tmp_path, "m1") is True and claims.unseen(tmp_path, "m1") is False


class _Model:
    async def think(self, agent_id, context, prompt, **kwargs):
        return ConsciousResponse(content="NONE", tokens_in=1, tokens_out=1, model="m")

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(content="ok", tokens_in=1, tokens_out=1, model="m")


class _Feelings:
    def __init__(self) -> None:
        self.felt: list[tuple[str, str, dict]] = []

    async def dispatch_hook(self, source, event_type, payload):
        self.felt.append((source, event_type, payload))


@pytest.mark.asyncio
async def test_a_false_statement_is_put_to_the_person_who_made_it_and_kept_against_them(
    tmp_path: Path, monkeypatch
) -> None:
    f = Fabric(agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model())
    ceo, cto = f.register_agent("ceo"), f.register_agent("cto")
    (ceo.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    ceo.task_queue = TaskQueue(tasks=[])
    f._chat_resolve = lambda name: "cto" if str(name).startswith("samantha") else None
    f._load_directory_cards = lambda: [
        {"id": "cto", "name": "Samantha", "role": "CTO"},
        {"id": "ceo", "name": "Maren", "role": "CEO"},
    ]
    f._known_repos = lambda: ["o/r"]
    f.plugin_manager = _Feelings()
    monkeypatch.setattr(ledger, "read_pull", lambda repo, number, token: dict(_OPEN))
    inbox = ceo.directory / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "m.json").write_text(
        json.dumps(
            {
                "from": "samantha@workforce.example",
                "subject": "Approved: PR #345 — block cleared",
                "text": "Maren,\n\nPR #345 is reviewed, approved and merged.",
                "message_id": "<m1>",
                "received_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    assert await f._check_what_colleagues_stated(ceo, []) == 1
    head = ceo.task_queue.tasks[0].description
    assert head.startswith("Samantha told you o/r#345 is ")
    assert "GitHub shows: open, no approval, no review" in head and "Tell Samantha" in head
    # Kept against the person who said it, and felt by them.
    row = claims.standing(f.agents_dir)["cto"]
    assert (row["checked"], row["false"]) == (3, 3)
    assert f.plugin_manager.felt[0][0] == "cto" and f.plugin_manager.felt[0][2]["valence"] < 0
    # Everyone is shown the record, the person themselves included.
    assert "Samantha (CTO): 3 of 3 checked statements were false" in f._statements_context(ceo)
    assert "**You**: 3 of 3" in f._statements_context(cto)
    # The same message is not judged twice.
    assert await f._check_what_colleagues_stated(ceo, []) == 0
    assert len(ceo.task_queue.tasks) == 1


@pytest.mark.asyncio
async def test_a_true_statement_counts_for_the_person_and_one_that_cannot_be_read_counts_for_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    f = Fabric(agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model())
    ceo = f.register_agent("ceo")
    f.register_agent("cto")
    (ceo.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    ceo.task_queue = TaskQueue(tasks=[])
    f._chat_resolve = lambda name: "cto"
    f._known_repos = lambda: ["o/r"]
    inbox = ceo.directory / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    def _mail(mid: str, text: str) -> None:
        (inbox / f"{mid}.json").write_text(
            json.dumps(
                {
                    "from": "samantha@x",
                    "subject": "",
                    "text": text,
                    "message_id": mid,
                    "received_at": datetime.now(UTC).isoformat(),
                }
            )
        )

    monkeypatch.setattr(ledger, "read_pull", lambda repo, number, token: {**_OPEN, "merged": True})
    _mail("a", "PR #349 is merged.")
    assert await f._check_what_colleagues_stated(ceo, []) == 0
    assert claims.standing(f.agents_dir)["cto"] == {"checked": 1, "false": 0, "latest": None}
    assert f._statements_context(ceo) == ""

    def _down(repo, number, token):
        raise OSError("unreachable")

    monkeypatch.setattr(ledger, "read_pull", _down)
    _mail("b", "PR #350 is merged.")
    assert await f._check_what_colleagues_stated(ceo, []) == 0
    assert claims.standing(f.agents_dir)["cto"]["checked"] == 1
    assert ceo.task_queue.tasks == []
