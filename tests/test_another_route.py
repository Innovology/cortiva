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


WHAT = (
    "ROUTE: sending the notification through the mail service\n"
    "SUBGOAL: the founder knows about each new lead\n"
    "UNTOUCHED: the form and the stored leads"
)
HOW = (
    "FIRST: Email Alex myself with the lead's details and tell the team in chat\n"
    "BOLD: Post the finding on LinkedIn with a link to the contact form\n"
    "WILD: Ring the five firms we most want and ask them straight out"
)


def test_the_first_ask_is_what_is_it_blocking():
    asked = rt.what_prompt(TASK, WALL, "- reads as working: the form stores a lead")
    assert TASK in asked and WALL in asked and "the form stores a lead" in asked
    assert "one particular route to one particular sub-goal" in asked
    assert rt.parse_what(WHAT) == (
        "sending the notification through the mail service",
        "the founder knows about each new lead",
        "the form and the stored leads",
    )


def test_the_second_ask_is_how_else_to_that_sub_goal():
    asked = rt.prompt("the founder knows about each new lead", "the mail service", "the form")
    assert "The sub-goal: the founder knows about each new lead" in asked
    assert "Leave alone what still works" in asked and "by hand if need be" in asked
    route, bold = rt.parse(HOW)
    assert route.startswith("Email Alex myself")
    assert [b.split()[0] for b in bold] == ["Post", "Ring"]


def test_nowhere_to_start_is_an_answer_too():
    assert rt.parse("FIRST: NONE, the bank holds the only copy") == ("", [])
    assert rt.parse("I cannot think of anything.") == ("", [])


def test_saying_nothing_is_in_the_way_is_not_an_obstacle():
    assert not rt.is_obstacle("None")
    assert not rt.is_obstacle("Nothing's blocking me")
    assert rt.is_obstacle(WALL)


def _fabric(tmp_path: Path, *answers: str) -> tuple[Fabric, SimpleNamespace, list[str]]:
    fab = Fabric.__new__(Fabric)
    fab.agents_dir = tmp_path
    asked: list[str] = []
    replies = list(answers)

    async def think(**kw):
        asked.append(kw["prompt"])
        return SimpleNamespace(content=replies.pop(0))

    fab.consciousness = SimpleNamespace(think=think)
    fab._meter_model_call = lambda agent, kind: None
    agent = SimpleNamespace(
        id="andre", directory=tmp_path / "andre", task_queue=TaskQueue(tasks=[])
    )
    return fab, agent, asked


def test_a_blocked_member_is_asked_both_and_the_place_to_start_is_their_next_task(tmp_path):
    fab, agent, asked = _fabric(tmp_path, WHAT, HOW)
    agent.task_queue.tasks.append(Task(id="t1", description="Something queued", priority=3))
    said = asyncio.run(fab._with_another_route(agent, TASK, WALL))
    assert len(asked) == 2 and "The sub-goal: the founder knows about each new lead" in asked[1]
    first = agent.task_queue.next_pending()
    assert first.description.startswith(rt.MARK) and "Email Alex myself" in first.description
    assert WALL in said and "Meanwhile I am taking another route" in said
    # The bolder ideas are kept as work, behind the place to start, and the
    # person asked for help hears them.
    kept = [t for t in agent.task_queue.tasks if t.description.startswith(rt.BOLD_MARK)]
    assert len(kept) == 2 and "LinkedIn" in kept[0].description
    assert "A bolder idea I have for it: Post the finding on LinkedIn" in said
    # The same wall told to a second person is one wall: nothing more is asked.
    asyncio.run(fab._with_another_route(agent, TASK, WALL))
    assert len(asked) == 2
    assert sum(t.description.startswith(rt.MARK) for t in agent.task_queue.tasks) == 1


def test_when_they_name_nowhere_to_start_the_person_asked_is_told_so(tmp_path):
    fab, agent, _ = _fabric(tmp_path, WHAT, "FIRST: NONE, only the bank has it")
    said = asyncio.run(fab._with_another_route(agent, TASK, WALL))
    assert "I have not found another place to start" in said
    assert agent.task_queue.tasks == []


def test_nothing_in_the_way_asks_nobody_anything(tmp_path):
    fab, agent, asked = _fabric(tmp_path, WHAT, HOW)
    assert asyncio.run(fab._with_another_route(agent, TASK, "None")) == "None"
    assert asked == []
