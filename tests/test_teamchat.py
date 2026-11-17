"""Team chat: cheap messages between colleagues (cortiva.core.teamchat)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from cortiva.core import teamchat as tc
from cortiva.core.fabric import Fabric


class TestPost:
    def test_a_direct_message_lands_in_the_recipients_unread(self, tmp_path):
        out = tc.post(tmp_path, "amara", "ask", "which service is prod?", to=["cto"])
        assert out["ok"] and out["message"]["thread"] == "dm:amara+cto"
        waiting = tc.unread(tmp_path, "cto")
        assert [m["body"] for m in waiting] == ["which service is prod?"]
        assert tc.unread(tmp_path, "cto") == []  # read once
        assert tc.unread(tmp_path, "amara") == []  # the sender is not a recipient

    def test_a_reply_in_a_thread_reaches_everyone_already_in_it(self, tmp_path):
        tc.post(tmp_path, "amara", "ask", "q", to=["cto", "yuki"], thread="site#312")
        tc.unread(tmp_path, "cto")
        tc.unread(tmp_path, "yuki")
        tc.post(tmp_path, "cto", "answer", "a", thread="site#312")
        assert [m["kind"] for m in tc.unread(tmp_path, "amara")] == ["answer"]
        assert [m["kind"] for m in tc.unread(tmp_path, "yuki")] == ["answer"]

    def test_an_unknown_kind_is_refused_and_names_the_vocabulary(self, tmp_path):
        out = tc.post(tmp_path, "amara", "shout", "x", to=["cto"])
        assert not out["ok"] and "claim" in out["error"]

    def test_no_thread_and_no_single_recipient_is_refused(self, tmp_path):
        assert not tc.post(tmp_path, "amara", "say", "x")["ok"]

    def test_the_team_can_add_a_kind(self, tmp_path):
        (tmp_path / tc.CHAT_DIR).mkdir()
        (tmp_path / tc.CHAT_DIR / tc.VOCABULARY_FILE).write_text(
            json.dumps({"deployed": {"means": "it is live", "attention": False}})
        )
        out = tc.post(tmp_path, "amara", "deployed", "", to=["cto"], ref="run 91")
        assert out["ok"] and not tc.needs_attention(tmp_path, out["message"])


class TestClaim:
    """One owner per item, decided without a model: 71% of a week's work
    sessions were on a task run four or more times (Mini-2, Nov 2026)."""

    def test_the_second_claim_is_refused_and_says_who_holds_it(self, tmp_path):
        assert tc.post(tmp_path, "amara", "claim", thread="site#312", to=["yuki"])["ok"]
        out = tc.post(tmp_path, "yuki", "claim", thread="site#312")
        assert not out["ok"] and out["held_by"] == "amara"
        assert tc.claims(tmp_path)["site#312"]["owner"] == "amara"

    def test_claiming_again_is_fine_and_release_frees_it(self, tmp_path):
        tc.post(tmp_path, "amara", "claim", thread="site#312", to=["yuki"])
        assert tc.post(tmp_path, "amara", "claim", thread="site#312")["ok"]
        tc.post(tmp_path, "yuki", "release", thread="site#312")  # not the owner: no effect
        assert "site#312" in tc.claims(tmp_path)
        tc.post(tmp_path, "amara", "release", thread="site#312")
        assert tc.post(tmp_path, "yuki", "claim", thread="site#312")["ok"]


def test_routine_kinds_need_no_attention_and_questions_do(tmp_path):
    for kind, wants in (("claim", False), ("done", False), ("ask", True), ("blocked", True)):
        assert tc.needs_attention(tmp_path, {"kind": kind}) is wants


def test_a_line_is_one_line(tmp_path):
    m = tc.post(tmp_path, "amara", "done", "merged\nand deployed", thread="site#312", to=["cto"])
    assert tc.line(m["message"]) == "amara done site#312: merged and deployed"


# ---------------------------------------------------------------------------
# Fabric wiring
# ---------------------------------------------------------------------------


def _fab(tmp_path: Path) -> Fabric:
    f = Fabric.__new__(Fabric)
    f.agents_dir = tmp_path
    f._ipc_socket_path = tmp_path / "fabric.sock"
    f.agents = {
        "amara": SimpleNamespace(id="amara", directory=tmp_path / "amara"),
        "cto": SimpleNamespace(id="cto", directory=tmp_path / "cto"),
    }
    f._agent_git_identity = lambda a: (  # type: ignore[method-assign]
        {"amara": "Amara Diallo", "cto": "Samantha Reyes"}[a.id],
        "",
    )
    return f


def _token(f: Fabric, agent_id: str, task_id: str = "task-1") -> str:
    env = f._chat_session_env(f.agents[agent_id], SimpleNamespace(id=task_id))
    return env["CORTIVA_CHAT_TOKEN"]


def test_the_sender_is_whoever_the_token_was_issued_to(tmp_path):
    f = _fab(tmp_path)
    tok = _token(f, "amara")
    out = asyncio.run(f._ipc_chat_send(token=tok, kind="ask", body="q", to="samantha"))
    assert out["ok"] and out["message"]["from"] == "amara" and out["message"]["to"] == ["cto"]
    refused = asyncio.run(f._ipc_chat_send(token="made-up", body="q", to="cto"))
    assert not refused["ok"] and "token" in refused["error"]


def test_an_unknown_colleague_is_refused(tmp_path):
    f = _fab(tmp_path)
    out = asyncio.run(f._ipc_chat_send(token=_token(f, "amara"), body="q", to="nobody"))
    assert not out["ok"] and "nobody" in out["error"]


def test_a_relaunched_task_replaces_its_token(tmp_path):
    f = _fab(tmp_path)
    old = _token(f, "amara")
    new = _token(f, "amara")
    assert f._chat_whoami(old) is None and f._chat_whoami(new) == ("amara", "task-1")


def test_no_socket_means_no_chat_for_the_session(tmp_path):
    f = _fab(tmp_path)
    f._ipc_socket_path = None
    assert f._chat_session_env(f.agents["amara"], SimpleNamespace(id="t")) == {}


def test_a_session_reads_a_reply_and_what_it_said_goes_back_to_the_agent(tmp_path):
    f = _fab(tmp_path)
    a, c = _token(f, "amara"), _token(f, "cto")
    asyncio.run(f._ipc_chat_send(token=a, kind="ask", body="prod service name?", to="cto"))
    got = asyncio.run(f._ipc_chat_read(token=c))
    assert "amara ask: prod service name?" in got["text"]
    asyncio.run(f._ipc_chat_send(token=c, kind="answer", body="innovology-web", to="amara"))
    back = asyncio.run(f._ipc_chat_read(token=a, wait_s=5))
    assert "cto answer: innovology-web" in back["text"]
    said = f._chat_transcripts[("amara", "task-1")]
    assert said == ["sent amara ask: prod service name?", "read cto answer: innovology-web"]


def test_the_agent_meets_questions_and_only_sees_a_line_for_routine(tmp_path):
    f = _fab(tmp_path)
    tc.post(tmp_path, "cto", "claim", thread="site#312", to=["amara"])
    tc.post(tmp_path, "cto", "ask", "can you review #320?", to=["amara"])
    agent = SimpleNamespace(id="amara")
    attention = f._chat_intake(agent)
    assert [m.content for m in attention] == ["[chat] cto ask: can you review #320?"]
    assert attention[0].sender == "cto" and attention[0].metadata["chat"] is True
    block = f._team_chat_context(agent)
    assert "cto claim site#312" in block and "no reply needed" in block
    assert f._team_chat_context(agent) == ""  # shown once


def test_only_routine_chat_costs_the_agent_nothing(tmp_path):
    f = _fab(tmp_path)
    tc.post(tmp_path, "cto", "done", thread="site#312", to=["amara"], ref="PR 320")
    assert f._chat_intake(SimpleNamespace(id="amara")) == []


def test_the_agents_own_message_goes_through_chat_to_a_local_colleague(tmp_path):
    f = _fab(tmp_path)
    amara = f.agents["amara"]
    assert f._chat_from_mind(
        amara, {"to": "Samantha", "content": "innovology-web", "kind": "answer"}
    )
    assert [m["kind"] for m in tc.unread(tmp_path, "cto")] == ["answer"]
    # A colleague this fabric does not run is left to the older paths (email).
    assert f._chat_from_mind(amara, {"to": "willow", "content": "x"}) is False


def test_a_refused_claim_from_the_mind_is_not_resent_another_way(tmp_path):
    f = _fab(tmp_path)
    tc.post(tmp_path, "cto", "claim", thread="site#312", to=["amara"])
    tc.unread(tmp_path, "amara")
    msg = {"to": "cto", "content": "", "kind": "claim", "thread": "site#312"}
    assert f._chat_from_mind(f.agents["amara"], msg) is True
    assert tc.claims(tmp_path)["site#312"]["owner"] == "cto"
    assert tc.unread(tmp_path, "cto") == []


def test_a_kind_the_model_invented_still_arrives(tmp_path):
    f = _fab(tmp_path)
    assert f._chat_from_mind(f.agents["amara"], {"to": "cto", "content": "hi", "kind": "memo"})
    assert [m["kind"] for m in tc.unread(tmp_path, "cto")] == ["say"]


# ---------------------------------------------------------------------------
# A session's access to its agent's memory
# ---------------------------------------------------------------------------


class _Mem:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def store(self, agent_id, content, *, tags=None, importance=5.0, metadata=None):
        row = SimpleNamespace(
            id=f"m{len(self.rows)}",
            agent_id=agent_id,
            content=content,
            tags=tags or [],
            importance=importance,
            metadata=metadata or {},
            created_at=None,
        )
        self.rows.append(row)
        return row

    async def search(self, agent_id, query, *, limit=10, **_):
        return [
            r for r in self.rows if r.agent_id == agent_id and query.lower() in r.content.lower()
        ][:limit]

    async def recall(self, agent_id, *, limit=20, min_importance=0.0):
        return [r for r in self.rows if r.agent_id == agent_id and r.importance >= min_importance][
            :limit
        ]


def test_a_session_reads_and_writes_only_its_own_agents_memory(tmp_path):
    f = _fab(tmp_path)
    f.memory = _Mem()
    a, c = _token(f, "amara"), _token(f, "cto")
    out = asyncio.run(
        f._ipc_memory_note(token=a, kind="decision", text="Use the host rule, not a second app.")
    )
    assert out["ok"]
    row = f.memory.rows[0]
    assert row.agent_id == "amara" and row.tags == ["session", "decision"]
    assert row.importance == 6.5 and row.metadata["task_id"] == "task-1"
    mine = asyncio.run(f._ipc_memory_search(token=a, query="host rule"))
    assert "Use the host rule" in mine["text"]
    theirs = asyncio.run(f._ipc_memory_search(token=c, query="host rule"))
    assert theirs["text"] == "nothing found"


def test_memory_needs_a_token_a_known_kind_and_a_real_sentence(tmp_path):
    f = _fab(tmp_path)
    f.memory = _Mem()
    a = _token(f, "amara")
    assert not asyncio.run(f._ipc_memory_search(token="forged", query="x"))["ok"]
    assert not asyncio.run(f._ipc_memory_note(token=a, kind="vibe", text="a long enough text"))[
        "ok"
    ]
    assert not asyncio.run(f._ipc_memory_note(token=a, kind="fact", text="done"))["ok"]
    assert f.memory.rows == []


def test_identity_is_readable_and_only_the_known_parts(tmp_path):
    f = _fab(tmp_path)
    f.agents["amara"].read_identity = lambda part: f"<{part}>"
    a = _token(f, "amara")
    assert asyncio.run(f._ipc_memory_identity(token=a, part="soul"))["text"] == "<soul>"
    assert not asyncio.run(f._ipc_memory_identity(token=a, part="../../etc/passwd"))["ok"]


def test_a_memory_whose_date_is_a_string_is_still_returned(tmp_path):
    f = _fab(tmp_path)
    f.memory = _Mem()
    a = _token(f, "amara")
    asyncio.run(f._ipc_memory_note(token=a, kind="fact", text="PR cycle time baseline is 4h."))
    f.memory.rows[0].created_at = "2026-11-17T03:11:55.123456+00:00"
    out = asyncio.run(f._ipc_memory_search(token=a, query="baseline"))
    assert out["ok"] and "[2026-11-17T03:11] PR cycle time baseline" in out["text"]
