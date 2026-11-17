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


class TestKnowHow:
    """What was achieved is written down as the clean path, the false steps
    go to a recovery sheet, and both are handed to the next session."""

    def _fab(self, tmp_path):
        f = _fab(tmp_path)
        f.memory = _Mem()

        async def search(agent_id, query, *, limit=10, tags=None, **_):
            rows = [r for r in f.memory.rows if r.agent_id == agent_id]
            return rows[:limit]  # an adapter that ignores the tag filter

        f.memory.search = search
        return f

    def test_a_procedure_keeps_its_steps_and_names_the_task(self, tmp_path):
        f = self._fab(tmp_path)
        task = SimpleNamespace(id="task-1", description="Put admin behind sign-in")
        tok = f._chat_session_env(f.agents["amara"], task)["CORTIVA_CHAT_TOKEN"]
        steps = "1. add host constraint in config/routes.rb\n\n2. bin/rails test\n3. merge"
        assert asyncio.run(f._ipc_memory_note(token=tok, kind="procedure", text=steps))["ok"]
        row = f.memory.rows[0]
        assert row.tags == ["session", "procedure"] and row.importance == 7.5
        assert row.content.splitlines() == [
            "How I did it — Put admin behind sign-in",
            "1. add host constraint in config/routes.rb",
            "2. bin/rails test",
            "3. merge",
        ]

    def test_the_next_session_is_handed_procedures_and_recoveries_only(self, tmp_path):
        f = self._fab(tmp_path)
        task = SimpleNamespace(id="task-1", description="Put admin behind sign-in")
        tok = f._chat_session_env(f.agents["amara"], task)["CORTIVA_CHAT_TOKEN"]
        asyncio.run(f._ipc_memory_note(token=tok, kind="lesson", text="A general lesson here."))
        asyncio.run(f._ipc_memory_note(token=tok, kind="procedure", text="1. do the thing"))
        asyncio.run(
            f._ipc_memory_note(token=tok, kind="recovery", text="CI red on lint -> run ruff format")
        )
        block = asyncio.run(f._know_how_for(f.agents["amara"], task))
        assert "How I did it" in block and "How to recover" in block
        assert "A general lesson" not in block
        assert "trust the world" in block
        # A colleague's know-how is not handed over: memory is the agent's own.
        assert asyncio.run(f._know_how_for(f.agents["cto"], task)) == ""

    def test_nothing_known_means_no_block(self, tmp_path):
        f = self._fab(tmp_path)
        task = SimpleNamespace(id="t", description="x")
        assert asyncio.run(f._know_how_for(f.agents["amara"], task)) == ""


def test_a_debrief_is_asked_of_sessions_that_did_real_work():
    env = {"CORTIVA_CHAT_TOKEN": "t"}
    assert Fabric._wants_debrief(12, "sid", env) is True
    assert Fabric._wants_debrief(2, "sid", env) is False  # barely touched anything
    assert Fabric._wants_debrief(12, "", env) is False  # no session to resume
    assert Fabric._wants_debrief(12, "sid", {}) is False  # no way to write memory


def test_the_debrief_carries_the_reviewers_verdict_and_allows_writing_nothing():
    p = Fabric._debrief_prompt("Put admin behind sign-in", "Not verified: /login still 404")
    assert "Not verified: /login still 404" in p
    assert "--kind procedure" in p and "--kind recovery" in p
    assert "If it was not achieved, write no procedure" in p
    assert "none given" in Fabric._debrief_prompt("x", "")


class TestArrivalTriage:
    """Something lands on a full plate: the agent calls it now or queue,
    and the checklist is otherwise left as it was."""

    def _agent(self, f):
        from cortiva.core.agent import Agent

        agent = Agent(id="amara", directory=f.agents_dir / "amara")
        agent.persist_runtime_state = lambda: None
        agent.update_plan(
            "# amara — Plan\n\n- [x] Ship the fix\n- [ ] Write the report\n- [ ] Review PR 12\n"
        )
        return agent

    def _open(self, text):
        return [ln for ln in text.splitlines() if ln.startswith("- [")]

    def test_now_goes_to_the_head_and_queue_to_the_foot(self, tmp_path):
        f = _fab(tmp_path)
        agent = self._agent(f)
        out = f._plan_with_arrivals(
            agent, "NOW: Restart the lead form — it cannot wait\n- QUEUE: Answer Yuki on pricing\n"
        )
        assert self._open(out) == [
            "- [ ] **[CRITICAL]** Restart the lead form",
            "- [x] Ship the fix",
            "- [ ] Write the report",
            "- [ ] Review PR 12",
            "- [ ] Answer Yuki on pricing",
        ]
        agent.update_plan(out)
        assert agent.next_task().description == "Restart the lead form"

    def test_none_leaves_the_checklist_as_it_was(self, tmp_path):
        f = _fab(tmp_path)
        agent = self._agent(f)
        out = f._plan_with_arrivals(agent, "NONE — nothing that arrived needs work from you")
        assert self._open(out) == [
            "- [x] Ship the fix",
            "- [ ] Write the report",
            "- [ ] Review PR 12",
        ]

    def test_an_item_already_queued_is_not_added_twice_and_now_moves_it_up(self, tmp_path):
        f = _fab(tmp_path)
        agent = self._agent(f)
        same = f._plan_with_arrivals(agent, "QUEUE: review PR 12")
        assert self._open(same).count("- [ ] Review PR 12") == 1 and len(self._open(same)) == 3
        up = f._plan_with_arrivals(agent, "**NOW**: Review PR 12")
        assert self._open(up) == [
            "- [ ] **[CRITICAL]** Review PR 12",
            "- [x] Ship the fix",
            "- [ ] Write the report",
        ]

    def test_a_form_filled_in_with_none_adds_nothing(self, tmp_path):
        f = _fab(tmp_path)
        agent = self._agent(f)
        out = f._plan_with_arrivals(agent, "NOW: Unblock Amara\nQUEUE: NONE\nNONE: NONE")
        assert len(self._open(out)) == 4 and "NONE" not in out

    def test_a_reply_that_is_a_checklist_is_not_a_verdict(self, tmp_path):
        f = _fab(tmp_path)
        agent = self._agent(f)
        assert f._plan_with_arrivals(agent, "- [ ] Write the report\n- [ ] Something new") is None


def test_know_how_is_handed_once_each_and_oldest_first(tmp_path):
    f = _fab(tmp_path)
    f._ipc_socket_path = "/tmp/x.sock"
    rows = [
        SimpleNamespace(
            content="How to recover — X\nrevert it",
            tags=["session", "recovery"],
            created_at="2026-11-17T12:26",
        ),
        SimpleNamespace(
            content="How to recover — X\nedit the file",
            tags=["session", "recovery"],
            created_at="2026-11-17T12:25",
        ),
        SimpleNamespace(
            content="How to recover — X\nrevert it",
            tags=["session", "recovery"],
            created_at="2026-11-17T12:26",
        ),
    ]

    async def search(agent_id, query, *, limit=10, tags=None, **_):
        return rows

    f.memory = SimpleNamespace(search=search)
    block = asyncio.run(
        f._know_how_for(f.agents["amara"], SimpleNamespace(id="task-1", description="X"))
    )
    assert block.count("revert it") == 1
    assert block.index("edit the file") < block.index("revert it")
    assert "the later one is the correction" in block


def test_an_arrival_shows_what_arrived_and_only_counts_what_was_already_there(tmp_path):
    import time
    from datetime import UTC, datetime, timedelta

    f = _fab(tmp_path)
    now = datetime.now(UTC)
    old, new = (now - timedelta(days=3)).isoformat(), now.isoformat()
    f._open_directives = lambda agent: [
        {"subject": "Old order", "opened_at": old, "label": "Maren"},
        {"subject": "Lead form today", "opened_at": new, "label": "Maren", "snippet": "start it"},
    ]
    f._open_github_feedback = lambda agent: [
        {"ref": "repo#1", "subject": "stale review", "last_seen": old},
        {"ref": "repo#2", "subject": "CI failed", "last_seen": new},
    ]
    block = f._arrivals_context(f.agents["amara"], time.time() - 600)
    assert "Lead form today" in block and "repo#2" in block
    assert "Old order" not in block and "repo#1" not in block
    assert "1 open directive(s) and 1 GitHub thread(s)" in block
