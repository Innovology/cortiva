"""Another route to the same end.

A route being closed is not the end being out of reach. Five people spent
sixteen hours on a mail key none of them could set, so that an automatic
email would tell the founder about a lead; any of them writing to him would
have had the same effect, and none of the five wrote that down as a
possibility (Mini-2, 2026-11-20). What the system did when a member was
blocked was send the block to their manager, and its guidance said a
workaround keeps you blocked.

When a member says they cannot go on, the request for help still goes. They
are also asked, once and briefly, what the work was for and how else to get
there with what they already have, and the answer becomes their next task.
"""

from __future__ import annotations

MARK = "Another route to the same end"

SYSTEM = "You are a member of a working team. Answer in the form asked for and nothing else."

_HOLLOW = ("none", "nothing", "n/a", "na ", "no blocker", "nope", "no, ")


def is_obstacle(said: str) -> bool:
    """Whether what a member said names something actually in the way."""
    text = (said or "").strip()
    return len(text) >= 8 and not text.lower().lstrip(" .—-").startswith(_HOLLOW)


def prompt(task: str, obstacle: str, ends: str = "") -> str:
    lines = [
        f"You were doing: {task.strip()}",
        f"What stands in the way: {obstacle.strip()}",
        "Your request for help has gone to the person who can clear it. They may say no, "
        "or answer too late to matter.",
    ]
    if ends.strip():
        lines.append(f"What your team is here to reach:\n{ends.strip()}")
    lines.append(
        "The thing in the way belongs to one way of doing this. Think about what the work "
        "was for: what would be true, and for whom, if it had gone through. Then name one "
        "other way to make that true that you can start now, with what you already have, "
        "waiting on nobody. A cruder way that has the same effect counts.\n"
        "Answer on two lines:\n"
        "END: <what the work was for, in a few words>\n"
        "ROUTE: <the one action you will take now>\n"
        "If there is truly no other way, write ROUTE: NONE and why in a few words."
    )
    return "\n\n".join(lines)


def parse(answer: str) -> tuple[str, str]:
    """(the end the work served, the other route) from the member's answer.
    The route is empty when they named none."""
    end = route = ""
    for raw in (answer or "").splitlines():
        line = raw.strip().lstrip("*-• ").strip()
        head, _, rest = line.partition(":")
        key = head.strip().strip("*").upper()
        if key == "END" and not end:
            end = rest.strip().strip("*").strip()
        elif key == "ROUTE" and not route:
            route = rest.strip().strip("*").strip()
    if not route or route.upper().startswith("NONE") or len(route) < 12:
        return end[:200], ""
    return end[:200], route[:400]


def task_text(route: str, end: str, task: str) -> str:
    """The next task, in the member's own words, tied to what it replaces."""
    served = f" The end it serves: {end}." if end else ""
    return (
        f'{MARK}: {route}{served} (The first way, "{task.strip()[:120]}", is waiting on '
        "someone else; this does not wait.)"
    )


def meanwhile(route: str, end: str) -> str:
    """What the person asked for help is told the member is doing meanwhile."""
    if route:
        served = f" (the end it serves: {end})" if end else ""
        return f"Meanwhile I am taking another route to the same end{served}: {route}"
    return "I have not found another route to the same end; this one waits on you."
