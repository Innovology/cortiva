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
    "NEEDS: someone decides who hears about each lead\n"
    "BY: TEAM\n"
    "ROUTE: sending the notification through the mail service\n"
    "SUBGOAL: the founder knows about each new lead\n"
    "UNTOUCHED: the form and the stored leads"
)
HOW = (
    "FIRST: Email Alex myself with the lead's details and tell the team in chat\n"
    "BOLD: Post the finding on LinkedIn with a link to the contact form\n"
    "WILD: Ring the five firms we most want and ask them straight out"
)
HELD = "USES: the team's own mail\nHELD: YES"
NOT_HELD = (
    "USES: a LinkedIn account; a list of fifty directors\n"
    "HELD: NO\n"
    "MISSING: a LinkedIn account and a list of directors\n"
    "INSTEAD: Publish the purchasing case study as a page on the site from the repository"
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


def test_the_first_thing_asked_is_what_has_to_happen_in_the_world_and_who_does_it():
    asked = rt.what_prompt(TASK, WALL, goal="- Leads: new clients")
    assert "Start from the end, not from the reading" in asked and "new clients" in asked
    assert asked.index("NEEDS:") < asked.index("ROUTE:")
    assert asked.index("NEEDS:") < asked.index("BY:") < asked.index("ROUTE:")
    assert rt.parse_needs(WHAT) == ("someone decides who hears about each lead", "")
    needs, by = rt.parse_needs("NEEDS: a person fills in the form\nBY: a prospective client")
    assert by == "a prospective client"
    # Where someone outside has to act, the second ask is how to reach them.
    how = rt.reach_prompt(needs, by, taken=["Write to five firms"])
    assert "Nobody on your team can do it for them" in how and "Write to five firms" in how
    assert "is not that person acting" in how
    assert "Bold is welcome and false is not" in how


def test_an_end_that_needs_an_outsider_asks_how_to_reach_them(tmp_path):
    what = WHAT.replace("BY: TEAM", "BY: a prospective client")
    fab, agent, asked = _fabric(tmp_path, what, "FIRST: Write today to the five firms we know")
    asyncio.run(fab._with_another_route(agent, TASK, WALL))
    assert "Who does it: a prospective client" in asked[1] and "The sub-goal" not in asked[1]
    assert "Write today to the five firms" in agent.task_queue.next_pending().description


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


def _still_mission(tmp_path: Path, moved_hours_ago: float) -> None:
    import json
    from datetime import UTC, datetime, timedelta

    from cortiva.core import missions as ms

    now = datetime.now(UTC)
    old = (now - timedelta(days=8)).isoformat()
    moved = (now - timedelta(hours=moved_hours_ago)).isoformat()
    (tmp_path / ms.MISSIONS_FILE).write_text(
        json.dumps(
            [
                {
                    "mission_id": "m1",
                    "title": "Leads reach the founder",
                    "intent": "A lead the founder can try to close.",
                    "issued_by": {"name": "Alex", "role": "founder"},
                    "team": ["maren", "andre"],
                    "status": "active",
                    "end_state": [
                        {
                            "id": "up",
                            "describe": "The site answers",
                            "check": {"url": "https://example.test/health", "status": 200},
                        },
                        {
                            "id": "told",
                            "describe": "The founder has been told of a lead",
                            "check": {"type": "team"},
                        },
                    ],
                }
            ]
        )
    )
    ms.save_state(
        tmp_path,
        {
            "m1": {
                "checked_at": now.isoformat(),
                "first_checked_at": old,
                "last_change_at": moved,
                "complete": False,
                "conditions": {
                    "up": {"met": True, "observed": "HTTP 200", "since": moved},
                    "told": {"met": False, "observed": "founder_notified = 0", "since": old},
                },
            }
        },
    )
    (tmp_path / ms.PROOFS_FILE).write_text(
        json.dumps({"m1": {"told": {"shown": {"by": "andre", "at": old}}}})
    )


def test_a_mission_standing_still_asks_the_team_though_nobody_said_blocked(tmp_path):
    import json
    import time

    from cortiva.core import missions as ms

    _still_mission(tmp_path, moved_hours_ago=22)
    rows = ms.standing_still(tmp_path)
    # Everyone on the team is asked, the one who showed it first.
    assert [(r["condition"], r["who"]) for r in rows] == [("told", ["andre", "maren"])]

    fab, andre, asked = _fabric(tmp_path, WHAT, HOW, NOT_HELD, WHAT, HOW, HELD)
    maren = SimpleNamespace(id="maren", directory=tmp_path / "maren", task_queue=None)
    fab.agents = {"andre": andre, "maren": maren}
    (tmp_path / "andre").mkdir()
    # Nobody chooses a route before someone has looked at what the business has.
    mission = ms.load(tmp_path)[0]
    asyncio.run(fab._ask_where_still())
    assert asked == [] and ms.standing_still(tmp_path) != []
    look = andre.task_queue.next_pending()
    page = ms.have_path(tmp_path, "andre", str(mission["mission_id"]))
    assert look.description.startswith(rt.LOOK_MARK) and str(page) in look.description
    look.status = "done"
    page.parent.mkdir(parents=True)
    page.write_text("## What we have already made or published\n" + "Fourteen articles. " * 20)
    (tmp_path / "andre" / "done.jsonl").write_text(
        json.dumps({"at": time.time() - 3600, "description": "Build the deliverability station"})
        + "\n"
    )
    asyncio.run(fab._ask_where_still())
    # What was finished is set beside the reading that did not move.
    assert "Build the deliverability station" in asked[0] and "founder_notified = 0" in asked[0]
    assert "Nobody has said no" in asked[0] and "not getting there" in asked[0]
    # The route is chosen with that page in front of them.
    assert "Fourteen articles" in asked[1] and "uses only what it says your team holds" in asked[1]
    # The second is told what the first named, and asked for something different.
    assert "Others on your team have already named" not in asked[1]
    # The place to start is then walked through against what the team holds.
    assert "Email Alex myself" in asked[2] and "Fourteen articles" in asked[2]
    assert "does not say your team holds" in asked[2]
    # The second is told what the first is taking, and asked for something different.
    assert "Publish the purchasing case study" in asked[4]
    assert "Name something different" in asked[4]
    for member in (andre, maren):
        first = member.task_queue.next_pending()
        assert first.description.startswith(rt.MARK) and "has not moved" in first.description
    # What needed a thing nobody holds is kept as a bolder idea, with what it needs,
    # and what the member can do today is the route they take.
    took = [t.description for t in andre.task_queue.tasks]
    assert "Publish the purchasing case study" in andre.task_queue.next_pending().description
    assert any(
        t.startswith(rt.BOLD_MARK) and "Email Alex myself" in t and "It needs: a LinkedIn" in t
        for t in took
    )
    assert "Email Alex myself" in maren.task_queue.next_pending().description
    # Asked once: the next look, minutes later, asks nobody again.
    assert ms.standing_still(tmp_path) == []
    asyncio.run(fab._ask_where_still())
    assert len(asked) == 6


def test_a_mission_that_moved_today_is_left_to_get_on(tmp_path):
    from cortiva.core import missions as ms

    _still_mission(tmp_path, moved_hours_ago=3)
    assert ms.standing_still(tmp_path) == []


def test_another_route_is_not_handed_to_the_station_already_built(tmp_path, monkeypatch):
    # A station is the route already built: it is never even looked up.
    from cortiva.core import fabric as fabric_module

    looked: list[str] = []
    monkeypatch.setattr(fabric_module._factory, "stations", lambda *a, **k: looked.append("x"))
    fab = Fabric.__new__(Fabric)
    agent = SimpleNamespace(id="andre", directory=tmp_path / "nowhere")
    for text in (
        rt.task_text("Write to five firms", "a lead", TASK),
        rt.bold_text("Ring them", ""),
    ):
        task = Task(id="t", description=text, priority=1)
        assert asyncio.run(fab._run_through_station(agent, task)) is False
    assert looked == []


def test_a_finished_proof_is_weighed_and_one_found_not_so_does_not_meet_the_condition(tmp_path):
    import json

    from cortiva.core import missions as ms

    (tmp_path / ms.MISSIONS_FILE).write_text(
        json.dumps(
            [
                {
                    "mission_id": "m1",
                    "title": "First client",
                    "intent": "A client signs.",
                    "team": ["amara", "yuki", "andre"],
                    "end_state": [
                        {
                            "id": "lead",
                            "describe": "A real visitor leaves their details on the site",
                            "check": {"type": "team"},
                        }
                    ],
                }
            ]
        )
    )
    (tmp_path / ms.PROOFS_FILE).write_text(
        json.dumps(
            {
                "m1": {
                    "lead": {
                        "shown": {
                            "by": "amara",
                            "url": "https://x.test/leads",
                            "contains": "lead",
                            "read_when_shown": {"met": False},
                        },
                        "seen": [{"by": "yuki", "saw": "I read the count and it said one lead."}],
                    }
                }
            }
        )
    )

    def fetch(url, timeout, bearer=""):
        return 200, "one lead"

    rows = ms.unweighed(tmp_path, fetch)
    assert [r["condition"]["id"] for r in rows] == ["lead"]
    asked = ms.weigh_prompt(rows[0])
    assert "A real visitor leaves" in asked and "it said one lead" in asked
    assert "does not make it so" in asked and "SO: YES or NO" in asked
    assert ms.parse_weigh(
        "NEEDS: x\nBY: a visitor\nDID: NOT SHOWN\nSO: NO\nWHY: Nobody says who."
    ) == (
        False,
        "Nobody says who.",
    )
    assert ms.parse_weigh("I am not sure") == (None, "")

    mission = ms.load(tmp_path)[0]
    cond = mission["end_state"][0]
    met, _ = ms.run_team_check(mission, cond, ms.load_proofs(tmp_path), tmp_path, fetch)
    assert met  # the team's word, until it is weighed
    ms.record_weigh(tmp_path, "m1", "lead", False, "Nobody says who left them.", "andre")
    met, observed = ms.run_team_check(mission, cond, ms.load_proofs(tmp_path), tmp_path, fetch)
    assert not met and "not so: Nobody says who left them." in observed
    # Weighed once: it is not asked again until a member shows it anew.
    assert ms.unweighed(tmp_path, fetch) == []


def test_the_fabric_weighs_with_a_member_who_took_no_part_and_asks_the_team_at_once(tmp_path):
    import json

    from cortiva.core import missions as ms

    (tmp_path / ms.MISSIONS_FILE).write_text(
        json.dumps(
            [
                {
                    "mission_id": "m1",
                    "title": "First client",
                    "intent": "A client signs.",
                    "team": ["amara", "yuki", "andre"],
                    "end_state": [
                        {
                            "id": "lead",
                            "describe": "A real visitor leaves their details on the site",
                            "check": {"type": "team"},
                        }
                    ],
                }
            ]
        )
    )
    row = {
        "mission": ms.load(tmp_path)[0],
        "condition": ms.load(tmp_path)[0]["end_state"][0],
        "proof": {"shown": {"by": "amara"}, "seen": [{"by": "yuki", "saw": "x"}]},
        "observed": "shown by amara, seen by yuki",
    }
    (tmp_path / ms.PROOFS_FILE).write_text(json.dumps({"m1": {"lead": row["proof"]}}))
    fab, andre, asked = _fabric(tmp_path, "SO: NO\nWHY: Nobody says who left them.")
    fab.agents = {"andre": andre}
    weighed_by: list[str] = []
    real = fab.consciousness.think

    async def think(**kw):
        weighed_by.append(kw["agent_id"])
        return await real(**kw)

    fab.consciousness.think = think
    orig = ms.unweighed
    ms.unweighed = lambda agents_dir, fetch=None: [row]
    try:
        asyncio.run(fab._weigh_proofs())
    finally:
        ms.unweighed = orig
    assert weighed_by == ["andre"]
    assert ms.load_proofs(tmp_path)["m1"]["lead"]["weighed"]["so"] is False
    owed = fab._asks_owed[("m1", "lead")]
    assert "found not so: Nobody says who left them." in owed["observed"] and owed["who"] == [
        "andre"
    ]


def test_a_member_already_holding_another_route_is_not_handed_one_more(tmp_path):
    fab, andre, asked = _fabric(tmp_path, WHAT, HOW)
    andre.task_queue = TaskQueue(
        tasks=[
            Task(
                id="r", description=rt.task_text("Write to five firms", "a lead", TASK), priority=1
            )
        ]
    )
    asyncio.run(fab._with_another_route(andre, "Some other piece of work", "Blocked: no key"))
    assert asked == []


def test_no_verdict_on_what_is_held_leaves_the_step_as_named():
    assert rt.parse_held("I am not sure.") == (None, "", "")
    assert rt.parse_held("HELD: YES\nMISSING: none") == (True, "", "")
    held, missing, instead = rt.parse_held(NOT_HELD)
    assert held is False and "LinkedIn" in missing and instead.startswith("Publish")


def test_a_long_page_keeps_what_the_team_holds():
    page = "\n\n".join(f"## {h}\n" + f"{h} line. " * 400 for h in rt.HEADINGS)
    digest = rt.page_digest(page)
    assert len(digest) < 6000
    for heading in rt.HEADINGS:
        assert f"## {heading}" in digest
    assert "What we hold line." in rt.have_block(page) and "What we hold line." in rt.held_prompt(
        "Post it", page
    )
    assert rt.page_digest("short page") == "short page"


def test_a_page_written_before_a_heading_was_asked_for_is_added_to() -> None:
    old = "\n\n".join(f"## {h}\nsomething" for h in rt.HEADINGS[:4])
    assert rt.missing_headings(old) == ("Who is already here",)
    assert rt.missing_headings(old + "\n\n## Who is already here\n9 sessions") == ()
    again = rt.look_text("Leads", "a lead", "/p/have.md", rt.missing_headings(old))
    assert "A page is already there" in again and "'Who is already here'" in again
    first = rt.look_text("Leads", "a lead", "/p/have.md")
    assert "A page is already there" not in first
    assert "one source you cannot open is one source" in first


def test_the_one_choosing_is_pointed_at_who_already_comes() -> None:
    page = "\n\n".join(f"## {h}\n" + f"{h} line. " * 400 for h in rt.HEADINGS)
    block = rt.have_block(page)
    assert "Who is already here line." in block
    assert "already comes and stops short is nearer than a stranger" in block
    # An older page, without the heading, is still shown by its headings.
    older = "\n\n".join(f"## {h}\n" + f"{h} line. " * 400 for h in rt.HEADINGS[:4])
    digest = rt.page_digest(older)
    assert all(h in digest for h in rt.HEADINGS[:4])


def test_what_the_teams_own_stations_did_is_set_before_the_one_choosing() -> None:
    page = "\n\n".join(f"## {h}\n89 leads captured" for h in rt.HEADINGS) + " " * 200
    acts = [("lead-capture-mission", 12, "submit a test lead via the contact form")]
    block = rt.have_block(page, "andre", "26 Nov", acts)
    assert "'lead-capture-mission' ran 12 time(s): submit a test lead" in block
    assert "nobody outside did it" in block
    assert block.index("lead-capture-mission") < block.index("89 leads captured")
    assert "own stations" not in rt.have_block(page)
    assert "'lead-capture-mission' ran 12" in rt.look_text("Leads", "a lead", "/p", (), acts)
