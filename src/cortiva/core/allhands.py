"""The all-hands: the head of the organisation says where things stand, and
everyone working hears it, thinks about what it means for them, and answers.

It is run by the fabric as a sequence of steps, each one a piece of one
agent's own thinking against that agent's own memory, role and state:

1. The convener opens from what the system itself shows.
2. Each person on the floor, in turn, takes the opening and everything said
   before them, and comes back with questions, challenges and suggestions.
   The convener answers each before the next speaks.
3. The convener reads the room and gives a position.
4. Each person decides what, if anything, they take away as their own work.

Everything said is written into every participant's memory as it happens.
Nothing here knows what the organisation does.

Files, under ``<agents_dir>/.allhands/``:

- ``config.json``        when it is held (``{"at": "HH:MM"}``, UTC)
- ``<date>.json``        the record of that day's meeting
- ``<date>.md``          the same, to be read
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DIR = ".allhands"
CONFIG = "config.json"
MAX_TAKES = 2
SAID_CHARS = 2400

OPENING = (
    "You are opening today's all-hands. Everyone working today will hear this, "
    "think about what it means for them, and then question and challenge it one "
    "at a time. In under 300 words, as yourself, in plain prose. Start with the "
    "number you are held to, exactly as it is stated above, and what the system "
    "reads for it today; then the driver of it, furthest down, that shows "
    "nothing: that is where the organisation actually stands. A number has a "
    "source only when its owner declares an address the system can read it "
    "from. If anything is listed above as waiting on someone, name the longest "
    "wait, who it is with and for how many hours. Then what you intend to do "
    "about it, and the one place "
    "where you most want the room's ideas. Be honest. Where what you have said "
    "or believed differs from what the system shows, say so. Do not lean on "
    "anyone listed as not currently working."
)

FLOOR = (
    "{convener} has opened the all-hands, and the colleagues before you have "
    "spoken (above). First think about what this means for you: your role, what "
    "you remember, what you hold to be true, what you have promised. Then "
    "answer in under 250 words, as yourself. A challenge must rest on something "
    "specific you can point to: a line from what you remember (above), a number "
    "as the system shows it, or a fact you hold. Quote it. 'In my experience' "
    "with nothing behind it is not a challenge; if you have nothing specific, "
    "ask the question you would need answered. Say what you need made clear, "
    "what you challenge, and at most one suggestion, naming the number it would "
    "move and how the system would see it move. Do not propose a new form, "
    "template or checklist where a standing order forbids it. Do not repeat a "
    "colleague unless you disagree with the answer they were given. If you have "
    "nothing to add, say so in one line."
)

ANSWER = (
    "{speaker} has spoken (above, last). Answer them directly in under 200 "
    "words. Answer any question first. For each challenge or suggestion say "
    "whether you accept it or decline it, and why. Accepting everything is not "
    "an answer: the people in this room can carry only so much, so if you "
    "accept something say what gives way for it, and decline what does not move "
    "the number you are held to. Where you were wrong, say so. Do not defend a "
    "position that the facts above do not support."
)

CLOSING = (
    "The floor has finished; everything said is above. Read the room and give "
    "your position now, in under 300 words. Choose at most two things to be "
    "done before the next all-hands. For each: whose idea it was, the number it "
    "moves, and how the system will see that it moved. Then say plainly what, "
    "of everything proposed, is not being done now, and why. Then what changed "
    "in your own view because of what you heard, and who changed it. Do not "
    "hand work to anyone listed as not currently working."
)

TAKE = (
    "The all-hands has ended. {convener}'s closing position is above, with what "
    "colleagues have already taken on. The call is yours and nobody else's: is "
    "there work from this meeting that you take on yourself and will do before "
    "the next one? One piece of work has one owner: do not take what a "
    "colleague has already taken, even in other words. Reply with one line per "
    "item, at most two, and nothing else:\n"
    "TAKE: <the task, one line>\n"
    "NONE — nothing of yours comes out of this"
)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        text = data if isinstance(data, str) else json.dumps(data, indent=2)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.debug("could not write %s", path, exc_info=True)


def config(agents_dir: Path) -> dict[str, Any]:
    data = _read(Path(agents_dir) / DIR / CONFIG)
    return data if isinstance(data, dict) else {}


def schedule(agents_dir: Path, at: str) -> tuple[bool, str]:
    """Set the time of day (UTC) it is held; an empty time stops it."""
    at = str(at or "").strip()
    if at and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        return False, "the time is HH:MM, 24-hour, UTC"
    _write(Path(agents_dir) / DIR / CONFIG, {"at": at})
    return True, at


def record_path(agents_dir: Path, day: str) -> Path:
    return Path(agents_dir) / DIR / f"{day}.json"


def load(agents_dir: Path, day: str) -> dict[str, Any]:
    data = _read(record_path(agents_dir, day))
    return data if isinstance(data, dict) else {}


def latest(agents_dir: Path) -> dict[str, Any]:
    try:
        days = sorted(
            p.stem
            for p in (Path(agents_dir) / DIR).glob("*.json")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}(-\d+)?", p.stem)
        )
    except OSError:
        days = []
    return load(agents_dir, days[-1]) if days else {}


def next_id(agents_dir: Path, day: str) -> str:
    """The day for the first meeting of a day, ``<day>-2`` for a second."""
    if not load(agents_dir, day):
        return day
    n = 2
    while load(agents_dir, f"{day}-{n}"):
        n += 1
    return f"{day}-{n}"


_TAKE_RE = re.compile(r"^\s*(?:[-*\d.)]+\s*)?[*_`]*TAKE\b[*_`]*\s*[:—–-]?\s*(.+)$", re.IGNORECASE)


def takes(reply: str) -> list[str]:
    """The items someone took on, from their one-line-each answer."""
    out: list[str] = []
    for line in str(reply or "").splitlines():
        m = _TAKE_RE.match(line)
        if not m:
            continue
        item = m.group(1).strip(" *_`<>").strip()
        if item and item.rstrip(".").upper() not in ("NONE", "NOTHING", "N/A") and item not in out:
            out.append(item)
    return out[:MAX_TAKES]


_SMALL = frozenset(
    "the a an and or of to for in on with by from at as is are be this that my our "
    "your their it its into via per up out all any new before after next".split()
)


def _words(item: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", str(item).lower()) if len(w) > 2 and w not in _SMALL
    }


def already_taken(item: str, taken: list[dict[str, Any]]) -> str:
    """Whose it already is, when most of this item's words are in one
    someone took before; empty when it is new work."""
    mine = _words(item)
    if len(mine) < 3:
        return ""
    for t in taken:
        theirs = _words(str(t.get("item")))
        if theirs and len(mine & theirs) / min(len(mine), len(theirs)) >= 0.6:
            return str(t.get("agent"))
    return ""


def due(agents_dir: Path, now: datetime | None = None) -> bool:
    """True when a time is set, it has passed today, and today's has not
    been started."""
    at = str(config(agents_dir).get("at") or "")
    if not at:
        return False
    now = now or datetime.now(UTC)
    if now.strftime("%H:%M") < at:
        return False
    return not load(agents_dir, now.strftime("%Y-%m-%d"))


def order(floor: list[str], day: str) -> list[str]:
    """Who speaks when. The last to speak has heard the most, so the order
    turns by one each day."""
    people = sorted(floor)
    if not people:
        return []
    try:
        k = datetime.strptime(day, "%Y-%m-%d").toordinal() % len(people)
    except ValueError:
        k = 0
    return people[k:] + people[:k]


def said(text: str) -> str:
    """What someone said, as it is kept: trimmed, bounded."""
    text = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    return text if len(text) <= SAID_CHARS else text[:SAID_CHARS].rstrip() + " […]"


def transcript(record: dict[str, Any], names: dict[str, str]) -> str:
    """Everything said so far, in the order it was said."""

    def who(aid: str) -> str:
        return names.get(aid) or aid

    parts: list[str] = []
    if record.get("opening"):
        parts.append(f"### {who(str(record.get('convener')))} opened\n{record['opening']}")
    for turn in record.get("turns") or []:
        brought = turn.get("recalled")
        note = f" (brought {brought} memories)" if brought is not None else ""
        parts.append(
            f"### {who(str(turn.get('agent')))} said{note}\n{turn.get('said') or '(nothing)'}"
        )
        if turn.get("answer"):
            parts.append(f"### {who(str(record.get('convener')))} answered\n{turn['answer']}")
    if record.get("closing"):
        parts.append(
            f"### {who(str(record.get('convener')))} closed with this position\n{record['closing']}"
        )
    taken = [f"- {who(str(t.get('agent')))}: {t.get('item')}" for t in record.get("taken") or []]
    if taken:
        parts.append("### Taken on\n" + "\n".join(taken))
    refused = [
        f"- {who(str(t.get('agent')))}: {t.get('item')} (already {who(str(t.get('owner')))}'s)"
        for t in record.get("not_placed") or []
    ]
    if refused:
        parts.append("### Not placed: one owner each\n" + "\n".join(refused))
    return "\n\n".join(parts)


def minutes(record: dict[str, Any], names: dict[str, str]) -> str:
    floor = ", ".join(names.get(a) or a for a in record.get("floor") or [])
    return (
        f"# All-hands, {record.get('day')}\n\n"
        f"Opened by {names.get(str(record.get('convener'))) or record.get('convener')}. "
        f"On the floor, in speaking order: {floor or 'nobody'}.\n\n"
        + transcript(record, names)
        + "\n"
    )
