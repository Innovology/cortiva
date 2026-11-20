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
        "was for: what would be true, and for whom, if it had gone through. Then think of "
        "other ways to make that true. Think widely: the crude, the bold and the "
        "unreasonable are all welcome, and an idea nobody has tried is worth more than "
        "waiting.\n"
        "Answer on these lines:\n"
        "END: <what the work was for, in a few words>\n"
        "FIRST: <the place to start: one thing you can do now, with what you already have, "
        "waiting on nobody>\n"
        "BOLD: <a bolder way to the same end, even if it needs someone's yes>\n"
        "WILD: <the boldest idea you have for it>\n"
        "Leave out BOLD or WILD if you have none. If there is truly nowhere to start, "
        "write FIRST: NONE and why in a few words."
    )
    return "\n\n".join(lines)


def parse(answer: str) -> tuple[str, str, list[str]]:
    """(the end the work served, the place to start, the bolder ideas) from
    the member's answer. The place to start is empty when they named none."""
    end = first = ""
    bold: list[str] = []
    for raw in (answer or "").splitlines():
        line = raw.strip().lstrip("*-• ").strip()
        head, _, rest = line.partition(":")
        key = head.strip().strip("*").upper()
        said = rest.strip().strip("*").strip()
        if key == "END" and not end:
            end = said
        elif key in ("FIRST", "ROUTE") and not first:
            first = said
        elif key in ("BOLD", "WILD") and len(said) >= 12 and not said.upper().startswith("NONE"):
            bold.append(said[:400])
    if not first or first.upper().startswith("NONE") or len(first) < 12:
        first = ""
    return end[:200], first[:400], bold[:2]


def task_text(route: str, end: str, task: str) -> str:
    """The next task, in the member's own words, tied to what it replaces."""
    served = f" The end it serves: {end}." if end else ""
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
