"""A pull request that waits on someone is read from GitHub and placed on
their checklist. Five people idled for four and a half hours beside three
pull requests that each waited on one of them (Mini-2, 2026-11-18)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core import grounding, ledger
from cortiva.core.agent import Task, TaskQueue
from cortiva.core.fabric import Fabric


def _pull(**kw):
    base = {
        "repo": "o/r",
        "number": 346,
        "title": "wire the failure count",
        "author": "amara",
        "draft": False,
        "requested": [],
        "mergeable_state": "blocked",
        "decisions": {},
        "pushed": "2026-11-18T12:41:28Z",
        "failing": [],
        "running": False,
    }
    return {**base, **kw}


def test_a_reviewer_asked_for_the_first_time_owes_a_review() -> None:
    kind, item = ledger.owed(_pull(requested=["samantha"]), "Samantha")
    assert kind == "review" and "o/r#346" in item and "amara's" in item


def test_the_author_pushed_after_changes_were_asked_so_the_reviewer_owes_a_second_look() -> None:
    asked = {"samantha": ("CHANGES_REQUESTED", "2026-11-18T12:28:55Z")}
    assert ledger.owed(_pull(decisions=asked), "samantha")[0] == "review_again"
    assert ledger.owed(_pull(decisions=asked), "amara") is None  # it waits on the reviewer
    # Nothing pushed since: it waits on the author.
    early = _pull(decisions=asked, pushed="2026-11-18T12:00:00Z")
    assert ledger.owed(early, "samantha") is None
    kind, item = ledger.owed(early, "amara")
    assert kind == "changes" and "samantha asked for changes" in item


def test_failing_checks_are_the_authors_and_the_reviewer_waits_for_green() -> None:
    red = _pull(author="samantha", requested=["yuki"], failing=["AC Lint", "AI Delegation"])
    kind, item = ledger.owed(red, "samantha")
    assert kind == "checks" and "AC Lint; AI Delegation" in item
    assert ledger.owed(red, "yuki") is None


def test_approved_green_and_clean_is_the_authors_to_merge() -> None:
    ok = _pull(decisions={"yuki": ("APPROVED", "2026-11-18T13:00:00Z")}, mergeable_state="clean")
    assert ledger.owed(ok, "amara")[0] == "merge"
    assert ledger.owed({**ok, "mergeable_state": "blocked"}, "amara") is None
    assert ledger.owed({**ok, "draft": True}, "amara") is None


def test_what_waits_is_read_with_the_persons_own_token(monkeypatch) -> None:
    def _get(url, token, timeout):
        assert token == "t"
        if url.endswith("/user"):
            return {"login": "samantha"}
        if "/search/issues" in url:
            hit = {"repository_url": "https://api.github.com/repos/o/r", "number": 346}
            return {"items": [hit] if "review-requested" in url else []}
        if url.endswith("/pulls/346"):
            return {
                "title": "wire the failure count",
                "user": {"login": "amara"},
                "head": {"sha": "abc"},
                "requested_reviewers": [{"login": "samantha"}],
                "mergeable_state": "blocked",
            }
        if "/reviews" in url:
            return [
                {
                    "user": {"login": "samantha"},
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-11-18T12:28:55Z",
                }
            ]
        if "/check-runs" in url:
            return {"check_runs": [{"status": "completed", "conclusion": "success", "name": "CI"}]}
        if url.endswith("/commits/abc"):
            return {"commit": {"committer": {"date": "2026-11-18T12:41:28Z"}}}
        raise AssertionError(url)

    monkeypatch.setattr(grounding, "_github_get", _get)
    waiting = ledger.waiting_on("t")
    assert [w["key"] for w in waiting] == ["o/r#346:review_again"]
    assert ledger.involved("o/r", 346, "Samantha", "t") is True
    assert ledger.involved("o/r", 346, "andre", "t") is False


def test_it_cannot_be_known_is_not_the_same_as_nothing_waiting(monkeypatch) -> None:
    def _down(url, token, timeout):
        raise OSError("unreachable")

    monkeypatch.setattr(grounding, "_github_get", _down)
    assert ledger.waiting_on("t") is None


class _Model:
    async def think(self, agent_id, context, prompt, **kwargs):
        raise AssertionError("the model is not asked")

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(content="ok", tokens_in=1, tokens_out=1, model="m")


def _rig(tmp_path: Path, monkeypatch, waiting):
    f = Fabric(agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model())
    agent = f.register_agent("cto")
    (agent.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    agent.task_queue = TaskQueue(tasks=[Task(id="a", description="Earlier work", status="done")])
    monkeypatch.setattr(ledger, "waiting_on", lambda token, **kw: list(waiting))
    return f, agent


_AGAIN = {
    "key": "o/r#346:review_again",
    "kind": "review_again",
    "repo": "o/r",
    "number": 346,
    "item": "o/r#346 (amara's: wire the failure count) changed since your review: review it "
    "again on GitHub and approve it if your points are met",
}


@pytest.mark.asyncio
async def test_the_step_owed_is_placed_by_the_system_and_goes_to_a_work_session(
    tmp_path: Path, monkeypatch
) -> None:
    f, agent = _rig(tmp_path, monkeypatch, [_AGAIN])
    assert await f._place_what_github_waits_for(agent) is True
    last = agent.task_queue.tasks[-1]
    assert last.status == "pending" and "o/r#346" in last.description
    assert f._is_terminal_task(last.description)
    # Already open on the checklist: nothing is added beside it.
    n = len(agent.task_queue.tasks)
    assert await f._place_what_github_waits_for(agent) is False
    assert len(agent.task_queue.tasks) == n


@pytest.mark.asyncio
async def test_ticked_while_github_still_shows_it_owed_it_is_opened_again_within_the_hour_once(
    tmp_path: Path, monkeypatch
) -> None:
    f, agent = _rig(tmp_path, monkeypatch, [_AGAIN])
    await f._place_what_github_waits_for(agent)
    item = agent.task_queue.tasks[-1]
    item.status = "done"
    assert await f._place_what_github_waits_for(agent) is False  # placed minutes ago
    f._github_placed["cto"].clear()  # an hour later
    n = len(agent.task_queue.tasks)
    assert await f._place_what_github_waits_for(agent) is True
    assert item.status == "pending" and len(agent.task_queue.tasks) == n


@pytest.mark.asyncio
async def test_a_clear_checklist_takes_what_github_waits_for_before_the_model_is_asked(
    tmp_path: Path, monkeypatch
) -> None:
    f, agent = _rig(tmp_path, monkeypatch, [_AGAIN])
    await f._replan_light(agent, [], idle=True)
    assert "o/r#346" in agent.task_queue.tasks[-1].description


def test_the_reviewer_who_lands_a_colleagues_pull_request_keeps_the_task(
    tmp_path: Path, monkeypatch
) -> None:
    f = Fabric(agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model())
    agent = f.register_agent("cto")
    (agent.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    monkeypatch.setattr(grounding, "github_login", lambda token, timeout=8.0: "samantha")
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {**ref, "status": "read", "author": "amara", "title": "x"},
    )
    reviews = {"346": True, "48": False}
    monkeypatch.setattr(
        ledger, "involved", lambda repo, num, login, token, timeout=8.0: reviews[str(num)]
    )
    agent.task_queue = TaskQueue(
        tasks=[
            Task(id="a", description="Review and merge o/r#346 as code owner"),
            Task(id="b", description="Merge o/r#48"),
        ]
    )
    assert f._strike_phantom_refs(agent) == 1
    assert [t.status for t in agent.task_queue.tasks] == ["pending", "skipped"]


@pytest.mark.asyncio
async def test_a_login_two_agents_hold_is_placed_on_the_one_it_is_named_after(
    tmp_path: Path, monkeypatch
) -> None:
    f, cto = _rig(tmp_path, monkeypatch, [_AGAIN])
    ceo = f.register_agent("ceo")
    (ceo.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    ceo.task_queue = TaskQueue(tasks=[])
    monkeypatch.setattr(grounding, "github_login", lambda token, timeout=8.0: "samantha-innovology")
    f._load_directory_cards = lambda: [
        {"id": "cto", "name": "Samantha Reyes"},
        {"id": "ceo", "name": "Maren Holt"},
    ]
    assert await f._place_what_github_waits_for(ceo) is False
    assert ceo.task_queue.tasks == []
    assert await f._place_what_github_waits_for(cto) is True


def test_one_review_of_the_head_as_it_stands_is_enough() -> None:
    """One approval lands a pull request here. With two people asked, the
    second was shown a review a colleague had already given."""
    both = _pull(
        requested=["samantha", "yuki"],
        decisions={"yuki": ("CHANGES_REQUESTED", "2026-11-18T19:01:10Z")},
    )
    assert ledger.owed(both, "samantha") is None
    assert ledger.owed(both, "amara")[0] == "changes"
    # Nobody has decided on this head: each person asked is shown it.
    fresh = _pull(requested=["samantha", "yuki"])
    assert ledger.owed(fresh, "samantha")[0] == "review"
    assert ledger.owed(fresh, "yuki")[0] == "review"
    # Her own older request for changes still blocks: that one is hers to lift.
    hers = _pull(
        decisions={
            "samantha": ("CHANGES_REQUESTED", "2026-11-18T12:28:55Z"),
            "yuki": ("APPROVED", "2026-11-18T19:01:10Z"),
        }
    )
    assert ledger.owed(hers, "samantha")[0] == "review_again"


def test_a_conflict_with_main_is_the_authors_and_reviewers_wait() -> None:
    stuck = _pull(requested=["yuki"], mergeable_state="dirty")
    assert ledger.owed(stuck, "amara")[0] == "conflicts"
    assert ledger.owed(stuck, "yuki") is None


@pytest.mark.asyncio
async def test_a_step_a_report_has_owed_for_hours_is_the_leaders_to_ask_about(
    tmp_path: Path, monkeypatch
) -> None:
    owed_six_hours = {
        **_AGAIN,
        "since": "2026-11-18T12:30:56Z",
        "what": "has not repaired the failing checks on o/r#350",
    }
    f, cto = _rig(tmp_path, monkeypatch, [owed_six_hours])
    ceo = f.register_agent("ceo")  # no GitHub token of her own
    ceo.task_queue = TaskQueue(tasks=[])
    f._active_reports = lambda agent: ["cto"] if agent.id == "ceo" else []
    f._load_directory_cards = lambda: [{"id": "cto", "name": "Samantha", "role": "CTO"}]
    monkeypatch.setattr(ledger, "hours_since", lambda stamp: 6.6)
    assert await f._place_what_github_waits_for(ceo) is True
    item = ceo.task_queue.tasks[-1].description
    assert item.startswith("Samantha has not repaired the failing checks on o/r#350")
    assert "7 hours" in item and "ask Samantha what is in the way" in item
    # Asked about once, not on every pass.
    ceo.task_queue.tasks[-1].status = "done"
    assert await f._place_what_github_waits_for(ceo) is False
    # The head of the meeting is shown the same thing, stated by the system.
    shown = await f._team_waits_context(["cto"], f._allhands_names())
    assert "Samantha (CTO) has not repaired the failing checks on o/r#350: waiting 6.6" in shown
    # A wait of minutes is nobody's to chase.
    monkeypatch.setattr(ledger, "hours_since", lambda stamp: 0.4)
    f._github_placed["ceo"].clear()
    assert await f._place_what_github_waits_for(ceo) is False


@pytest.mark.asyncio
async def test_a_pull_request_a_standing_order_covers_is_nobodys_wait(
    tmp_path: Path, monkeypatch
) -> None:
    """Shown a nine-hour wait on a pull request the founder had ordered
    stopped, the head told its reviewer to approve it at once."""
    from cortiva.core import standing_orders

    stopped = {
        **_AGAIN,
        "key": "o/r#345:review",
        "number": 345,
        "title": "mandatory spec fields in PR template",
        "item": "o/r#345 (amara's: mandatory spec fields in PR template) waits for your review",
        "since": "2026-11-18T10:31:43Z",
        "what": "has not reviewed o/r#345",
    }
    f, cto = _rig(tmp_path, monkeypatch, [stopped])
    order = {"text": "Stop building red tape", "issued_by": {"name": "the founder"}}
    monkeypatch.setattr(standing_orders, "load", lambda agents_dir: [order])
    monkeypatch.setattr(
        standing_orders,
        "matching_order",
        lambda orders, text: orders[0] if "pr template" in text.lower() else None,
    )
    assert await f._github_waiting(cto) == []
    assert await f._place_what_github_waits_for(cto) is False
    assert await f._team_waits(["cto"]) == []
    # Named by number alone, the task is struck all the same.
    cto.task_queue.tasks.append(Task(id="z", description="Review and approve PR #345 immediately"))
    assert f._strike_under_standing_orders(cto) == 1
    assert "standing order from the founder" in cto.task_queue.tasks[-1].description


@pytest.mark.asyncio
async def test_a_step_owed_for_hours_goes_to_the_head_even_when_the_checklist_is_not_clear(
    tmp_path: Path, monkeypatch
) -> None:
    f, agent = _rig(tmp_path, monkeypatch, [{**_AGAIN, "since": "2026-11-18T12:30:56Z"}])
    agent.task_queue = TaskQueue(tasks=[Task(id="a", description="Something else in hand")])
    monkeypatch.setattr(ledger, "hours_since", lambda stamp: 7.0)
    assert await f._place_what_github_waits_for(agent, leading=False) is True
    assert "o/r#346" in agent.task_queue.tasks[0].description
    assert "- [ ] **[CRITICAL]** o/r#346" in agent.read_identity("plan")
