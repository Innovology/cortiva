"""Another route to the same end.

A route being closed is not the end being out of reach. Five people spent
sixteen hours on a mail key none of them could set, so that an automatic
email would tell the founder about a lead; any of them writing to him would
have had the same effect, and none of the five wrote that down as a
possibility (Mini-2, 2026-11-20). What the system did when a member was
blocked was send the block to their manager, and its guidance said a
workaround keeps you blocked.

When a member says they cannot go on, the request for help still goes. They
are also asked two short things. First, what is it blocking: something in
the way closes one particular route to one particular sub-goal, and leaves
the goal and everything already working where they were. Second, how else
to that sub-goal: a place to start now, and their bolder ideas. The place
to start is their next task; the bolder ideas are kept as work to put to
whoever can say yes.

Nobody has to say "blocked" for the questions to be asked. A team that is
busy does not say it: one condition stood for twenty-two hours while its
members finished fifteen pieces of work and no question was put to anyone
(Mini-2, 2026-11-20). When nothing in a mission has moved for half a day the
system asks the same two things, with what was finished in that time set
beside the reading that did not move.
"""

from __future__ import annotations

MARK = "Another route to the same end"

SYSTEM = "You are a member of a working team. Answer in the form asked for and nothing else."

_HOLLOW = ("none", "nothing", "n/a", "na ", "no blocker", "nope", "no, ")


def is_obstacle(said: str) -> bool:
    """Whether what a member said names something actually in the way."""
    text = (said or "").strip()
    return len(text) >= 8 and not text.lower().lstrip(" .—-").startswith(_HOLLOW)


def still_text(row: dict[str, object], finished: list[str]) -> tuple[str, str]:
    """(what the work is, what is in the way) for a condition that has stood
    still: nobody said no, and nothing that was done moved it."""
    task = f'Mission "{row["title"]}": {row["describe"]}'
    did = (
        " In that time you finished: " + "; ".join(f'"{d}"' for d in finished) + "."
        if finished
        else " In that time you finished nothing that is on record."
    )
    wall = (
        f"Nobody has said no. The system has read this as not so for {row['not_met_for']} "
        f"({row['observed']}), and nothing in the mission has moved for {row['still_for']}."
        f"{did} None of it moved the reading."
    )
    return task, wall


def what_prompt(task: str, obstacle: str, standing: str = "", *, still: bool = False) -> str:
    """First ask: what is it blocking? A block closes one route to one
    sub-goal; it is not the goal that is blocked."""
    lines = [
        f"{'The end you are working to' if still else 'You were doing'}: {task.strip()}",
        f"What stands in the way: {obstacle.strip()}",
    ]
    if standing.strip():
        lines.append(
            "What the system reads on the real thing right now (these are facts, not "
            f"opinions):\n{standing.strip()}"
        )
    lines.append(
        (
            "Work that does not move the reading is on a route that is not getting there, "
            "whether or not anyone has said no. Something in the way closes one particular "
            "route to one particular sub-goal. It does not close the goal, and it does not "
            "break what is already working. Say which route you have been on that is not "
            "getting there, and no more than that.\n"
            if still
            else "Something in the way closes one particular route to one particular "
            "sub-goal. It does not close the goal, and it does not break what is already "
            "working. Say exactly what this one is blocking, and no more than that.\n"
        )
        + "Answer on these lines:\n"
        "ROUTE: <the one way of doing something that is closed>\n"
        "SUBGOAL: <the result that route was for, said as what a person would then have or "
        "know, naming no tool or system>\n"
        "UNTOUCHED: <what still works without it>"
    )
    return "\n\n".join(lines)


def parse_what(answer: str) -> tuple[str, str, str]:
    """(the route that is closed, the sub-goal it served, what still works)."""
    got = {"ROUTE": "", "SUBGOAL": "", "UNTOUCHED": ""}
    for raw in (answer or "").splitlines():
        line = raw.strip().lstrip("*-• ").strip()
        head, _, rest = line.partition(":")
        key = head.strip().strip("*").upper().replace("-", "").replace(" ", "")
        if key in got and not got[key]:
            got[key] = rest.strip().strip("*").strip()[:300]
    return got["ROUTE"], got["SUBGOAL"], got["UNTOUCHED"]


def prompt(subgoal: str, closed: str, untouched: str = "", goal: str = "") -> str:
    """Second ask: how else to that sub-goal?"""
    lines = [
        f"The sub-goal: {subgoal.strip()}",
        f"The route to it that is closed: {closed.strip()}",
    ]
    if untouched.strip():
        lines.append(f"What still works and needs no fixing: {untouched.strip()}")
    if goal.strip():
        lines.append(f"The goal the sub-goal sits under:\n{goal.strip()}")
    lines.append(
        "Whoever could open that route may say no, or answer too late to matter. Think of "
        "other routes to the same sub-goal. Think widely: the crude, the bold and the "
        "unreasonable are all welcome, and an idea nobody has tried is worth more than "
        "waiting. Leave alone what still works.\n"
        "Answer on these lines:\n"
        "FIRST: <the place to start: one thing you can do now, with what you already have, "
        "waiting on nobody, by hand if need be>\n"
        "BOLD: <a bolder route to the same sub-goal, even if it needs someone's yes>\n"
        "WILD: <the boldest idea you have for it>\n"
        "Leave out BOLD or WILD if you have none. If there is truly nowhere to start, "
        "write FIRST: NONE and why in a few words."
    )
    return "\n\n".join(lines)


def parse(answer: str) -> tuple[str, list[str]]:
    """(the place to start, the bolder ideas) from the member's answer. The
    place to start is empty when they named none."""
    first = ""
    bold: list[str] = []
    for raw in (answer or "").splitlines():
        line = raw.strip().lstrip("*-• ").strip()
        head, _, rest = line.partition(":")
        key = head.strip().strip("*").upper()
        said = rest.strip().strip("*").strip()
        if key in ("FIRST", "ROUTE") and not first:
            first = said
        elif key in ("BOLD", "WILD") and len(said) >= 12 and not said.upper().startswith("NONE"):
            bold.append(said[:400])
    if not first or first.upper().startswith("NONE") or len(first) < 12:
        first = ""
    return first[:400], bold[:2]


def task_text(route: str, end: str, task: str, *, still: bool = False) -> str:
    """The next task, in the member's own words, tied to what it replaces."""
    served = f" The end it serves: {end}." if end else ""
    if still:
        return (
            f'{MARK}: {route}{served} ("{task.strip()[:120]}" has not moved while the '
            "first way was worked; this is another way to it.)"
        )
    return (
        f'{MARK}: {route}{served} (The first way, "{task.strip()[:120]}", is waiting on '
        "someone else; this does not wait.)"
    )


BOLD_MARK = "A bolder idea of yours"


def bold_text(idea: str, end: str) -> str:
    """A bolder idea, kept as work: it is put to whoever can say yes, with
    what it would take, so it is weighed and not lost."""
    served = f" for the same end ({end})" if end else " for the same end"
    return (
        f"{BOLD_MARK}{served}: {idea} Put it to whoever can say yes, with what it would "
        "take and what it would get; start it yourself if it needs nobody's yes."
    )


def meanwhile(route: str, end: str, bold: list[str] | None = None) -> str:
    """What the person asked for help is told the member is doing meanwhile."""
    more = "".join(f"\nA bolder idea I have for it: {b}" for b in bold or [])
    if route:
        served = f" (the end it serves: {end})" if end else ""
        return f"Meanwhile I am taking another route to the same end{served}: {route}{more}"
    return f"I have not found another place to start; this one waits on you.{more}"
