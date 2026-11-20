"""A route being closed is not the end being out of reach.

Five people spent sixteen hours on a mail key none of them could set; any of
them writing to the founder would have had the same effect (Mini-2, 2026-11-20)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from cortiva.core import routes as rt
from cortiva.core.agent import Task, TaskQueue
from cortiva.core.fabric import Fabric

TASK = "Set RESEND_API_KEY in Railway to enable founder notifications for new leads"
WALL = "I have no Railway access; Samantha needs to set the key."


def test_the_ask_names_what_was_being_done_what_is_in_the_way_and_the_end():
    asked = rt.prompt(TASK, WALL, "- Leads: get a lead the founder can close.")
    assert TASK in asked and WALL in asked and "get a lead the founder can close" in asked
    assert "what the work" in asked and "waiting on nobody" in asked


def test_an_answer_gives_the_end_and_the_route():
    end, route, bold = rt.parse(
        "END: the founder knows about each new lead\n"
        "FIRST: Email Alex myself with the lead's details and tell the team in chat\n"
        "BOLD: Post the finding on LinkedIn with a link to the contact form\n"
        "WILD: Ring the five firms we most want and ask them straight out"
    )
    assert end == "the founder knows about each new lead"
    assert route.startswith("Email Alex myself")
    assert [b.split()[0] for b in bold] == ["Post", "Ring"]


def test_no_other_way_is_an_answer_too():
    assert rt.parse("END: books closed\nFIRST: NONE, the bank holds the only copy") == (
        "books closed",
        "",
        [],
    )
    assert rt.parse("I cannot think of anything.") == ("", "", [])


def test_saying_nothing_is_in_the_way_is_not_an_obstacle():
    assert not rt.is_obstacle("None")
    assert not rt.is_obstacle("Nothing's blocking me")
    assert rt.is_obstacle(WALL)


def _fabric(tmp_path: Path, answer: str) -> tuple[Fabric, SimpleNamespace, list[str]]:
    fab = Fabric.__new__(Fabric)
    fab.agents_dir = tmp_path
    asked: list[str] = []

    async def think(**kw):
        asked.append(kw["prompt"])
        return SimpleNamespace(content=answer)

    fab.consciousness = SimpleNamespace(think=think)
    fab._meter_model_call = lambda agent, kind: None
    agent = SimpleNamespace(
        id="andre", directory=tmp_path / "andre", task_queue=TaskQueue(tasks=[])
    )
    return fab, agent, asked


def test_a_blocked_member_is_asked_and_the_other_route_is_their_next_task(tmp_path):
    fab, agent, asked = _fabric(
        tmp_path,
        "END: the founder knows about each new lead\n"
        "FIRST: Email Alex myself with the lead's details and tell the team in chat\n"
        "BOLD: Post the finding on LinkedIn with a link to the contact form",
    )
    agent.task_queue.tasks.append(Task(id="t1", description="Something queued", priority=3))
    said = asyncio.run(fab._with_another_route(agent, TASK, WALL))
    assert len(asked) == 1
    first = agent.task_queue.next_pending()
    assert first.description.startswith(rt.MARK) and "Email Alex myself" in first.description
    assert WALL in said and "Meanwhile I am taking another route" in said
    # The bolder idea is kept as work, behind the place to start, and the
    # person asked for help hears it.
    kept = [t for t in agent.task_queue.tasks if t.description.startswith(rt.BOLD_MARK)]
    assert len(kept) == 1 and "LinkedIn" in kept[0].description
    assert "A bolder idea I have for it: Post the finding on LinkedIn" in said
    # The same wall told to a second person is one wall: asked once, queued once.
    asyncio.run(fab._with_another_route(agent, TASK, WALL))
    assert len(asked) == 1
    assert sum(t.description.startswith(rt.MARK) for t in agent.task_queue.tasks) == 1


def test_when_they_name_none_the_person_asked_is_told_so(tmp_path):
    fab, agent, _ = _fabric(tmp_path, "END: books closed\nFIRST: NONE, only the bank has it")
    said = asyncio.run(fab._with_another_route(agent, TASK, WALL))
    assert "I have not found another place to start" in said
    assert agent.task_queue.tasks == []


def test_nothing_in_the_way_asks_nobody_anything(tmp_path):
    fab, agent, asked = _fabric(tmp_path, "FIRST: something long enough to count")
    assert asyncio.run(fab._with_another_route(agent, TASK, "None")) == "None"
    assert asked == []
