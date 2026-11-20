"""Team chat: short messages between colleagues, cheap to send and to receive.

Mail between agents is slow and expensive here. Every arrival, however
routine, costs the recipient a re-plan on the local model (about a minute,
on a model five agents already fill), and a question that comes up in the
middle of a task has to travel through two of those to get an answer.

Chat separates the traffic by what it needs:

* **Routine** messages are a small shared vocabulary (``claim``, ``release``,
  ``ready``, ``done``, ``ack``). The system records them, keeps the one fact
  that matters (who holds an item) and shows the recipient a line the next
  time they look. No model call on either side.
* **Attention** messages (``ask``, ``answer``, ``blocked``, ``say``) are for
  a colleague to read and act on.

Who sent a message is set by the system, never typed by the sender: a work
session refused an instruction it could not source (Mini-2, 2026-11-12), and
a chat message it cannot source is no better.

Everything is written from the fabric process (the CLI goes through IPC),
so the files need no lock. No model calls in this module.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortiva.teamchat")

CHAT_DIR = ".chat"
VOCABULARY_FILE = "vocabulary.json"
CLAIMS_FILE = "claims.json"

# kind -> (what it means, whether it needs a colleague's attention)
VOCABULARY: dict[str, tuple[str, bool]] = {
    "claim": ("I have taken this item; nobody else should", False),
    "release": ("I have let go of this item; it is free", False),
    "ready": ("this item is ready for the next person (review, test, sign-off)", False),
    "done": ("this item is finished; the evidence is in ref", False),
    "ack": ("seen", False),
    "ask": ("a question I need answered to carry on", True),
    "answer": ("the answer to an ask", True),
    "blocked": ("I cannot carry on; body names what is needed", True),
    "say": ("anything else, in as few words as it takes", True),
}

MAX_BODY = 2000


def _dir(agents_dir: Path) -> Path:
    return Path(agents_dir) / CHAT_DIR


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._#+-]", "_", str(name))[:120] or "_"


def vocabulary(agents_dir: Path) -> dict[str, tuple[str, bool]]:
    """The built-in kinds plus any the team has added in ``vocabulary.json``
    (``{"kind": {"means": "...", "attention": false}}``). A kind that keeps
    recurring as free text belongs here, the way a repeated step becomes a
    station."""
    vocab = dict(VOCABULARY)
    try:
        extra = json.loads((_dir(agents_dir) / VOCABULARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return vocab
    if isinstance(extra, dict):
        for kind, spec in extra.items():
            if isinstance(spec, dict) and re.fullmatch(r"[a-z][a-z0-9_-]{1,23}", str(kind)):
                vocab.setdefault(
                    str(kind), (str(spec.get("means") or ""), bool(spec.get("attention")))
                )
    return vocab


def needs_attention(agents_dir: Path, msg: dict[str, Any]) -> bool:
    spec = vocabulary(agents_dir).get(str(msg.get("kind")))
    return True if spec is None else spec[1]


def dm_thread(a: str, b: str) -> str:
    return "dm:" + "+".join(sorted((str(a), str(b))))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def claims(agents_dir: Path) -> dict[str, dict[str, str]]:
    """item thread -> {"owner", "at"}."""
    try:
        data = json.loads((_dir(agents_dir) / CLAIMS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_claims(agents_dir: Path, data: dict[str, dict[str, str]]) -> None:
    path = _dir(agents_dir) / CLAIMS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def participants(agents_dir: Path, thread: str) -> list[str]:
    """Everyone who has sent to or been sent a message in a thread."""
    seen: list[str] = []
    for row in _read_jsonl(_dir(agents_dir) / "threads" / f"{_safe(thread)}.jsonl"):
        for who in [row.get("from"), *(row.get("to") or [])]:
            if who and who not in seen:
                seen.append(str(who))
    return seen


def post(
    agents_dir: Path,
    sender: str,
    kind: str,
    body: str = "",
    *,
    to: list[str] | None = None,
    thread: str = "",
    ref: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one message and put it in each recipient's unread list.

    ``sender`` is supplied by the caller that authenticated them, never by
    the message. Returns ``{"ok": True, "message": ...}`` or
    ``{"ok": False, "error": ...}``. A ``claim`` on an item someone else
    holds is refused and says who holds it: that refusal is how two idle
    colleagues end up on two different things."""
    kind = str(kind or "say").strip().lower()
    if kind not in vocabulary(agents_dir):
        known = ", ".join(sorted(vocabulary(agents_dir)))
        return {"ok": False, "error": f"unknown kind '{kind}'; use one of: {known}"}
    recipients = [str(t).strip() for t in (to or []) if str(t).strip() and str(t) != sender]
    if not thread:
        if len(recipients) != 1:
            return {"ok": False, "error": "name a thread, or exactly one recipient"}
        thread = dm_thread(sender, recipients[0])
    if not recipients:
        recipients = [p for p in participants(agents_dir, thread) if p != sender]
    stamp = (now or datetime.now(UTC)).isoformat()

    if kind in ("claim", "release"):
        held = claims(agents_dir)
        current = held.get(thread)
        if kind == "claim":
            if current and current.get("owner") != sender:
                return {
                    "ok": False,
                    "error": f"{thread} is held by {current['owner']} since {current['at']}",
                    "held_by": current["owner"],
                }
            held[thread] = {"owner": sender, "at": (current or {}).get("at") or stamp}
        elif current and current.get("owner") == sender:
            held.pop(thread, None)
        _save_claims(agents_dir, held)

    msg: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "at": stamp,
        "from": sender,
        "to": recipients,
        "thread": thread,
        "kind": kind,
        "body": str(body or "")[:MAX_BODY],
        "ref": str(ref or "")[:500],
    }
    base = _dir(agents_dir)
    _append(base / "threads" / f"{_safe(thread)}.jsonl", msg)
    for who in recipients:
        _append(base / "unread" / f"{_safe(who)}.jsonl", msg)
    return {"ok": True, "message": msg}


def unread(agents_dir: Path, agent_id: str, *, take: bool = True) -> list[dict[str, Any]]:
    """Messages waiting for an agent. ``take`` removes them: whoever reads
    first, the agent or one of its work sessions, has read them."""
    path = _dir(agents_dir) / "unread" / f"{_safe(agent_id)}.jsonl"
    rows = _read_jsonl(path)
    if rows and take:
        try:
            path.unlink()
        except OSError:
            logger.debug("could not clear %s", path, exc_info=True)
    return rows


def unread_names(agents_dir: Path) -> list[str]:
    """Every name that has chat waiting under it."""
    try:
        return sorted(p.stem for p in (_dir(agents_dir) / "unread").glob("*.jsonl"))
    except OSError:
        return []


def readdress(agents_dir: Path, name: str, agent_id: str) -> int:
    """Move what waits under ``name`` to ``agent_id``, oldest first, each
    message marked with the name it was sent to. Returns how many moved."""
    src = _dir(agents_dir) / "unread" / f"{_safe(name)}.jsonl"
    rows = _read_jsonl(src)
    for row in rows:
        _append(
            _dir(agents_dir) / "unread" / f"{_safe(agent_id)}.jsonl",
            {**row, "to": [agent_id], "sent_to_name": name},
        )
    try:
        src.unlink()
    except OSError:
        logger.debug("could not clear %s", src, exc_info=True)
    return len(rows)


def line(msg: dict[str, Any]) -> str:
    """One message on one line."""
    parts = [str(msg.get("from", "?")), str(msg.get("kind", "say"))]
    thread = str(msg.get("thread") or "")
    if thread and not thread.startswith("dm:"):
        parts.append(thread)
    text = " ".join(parts)
    body = " ".join(str(msg.get("body") or "").split())
    if body:
        text += f": {body}"
    if msg.get("ref"):
        text += f" [{msg['ref']}]"
    if msg.get("sent_to_name"):
        sent = str(msg.get("at") or "")[:16].replace("T", " ")
        text += f' (sent {sent} UTC to "{msg["sent_to_name"]}"; it reached you only now)'
    return text


def digest(messages: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {line(m)}" for m in messages)


def session_guide(agents_dir: Path, agent_id: str) -> str:
    """How a work session uses chat. Rides in the session's prompt."""
    kinds = "; ".join(f"`{k}` = {v[0]}" for k, v in vocabulary(agents_dir).items())
    return (
        "## 💬 Team chat\n"
        f"You can message colleagues directly as {agent_id}. The system stamps who a "
        "message is from, so one you receive is from who it says.\n"
        '- Send: `cortiva chat send --to <colleague> [--kind <kind>] [--thread <item>] "<text>"`\n'
        "- Read what is waiting for you: `cortiva chat read` (add `--wait 60` to wait for "
        "a reply to a question you just asked).\n"
        f"- Kinds: {kinds}.\n"
        "- Before starting on a shared item (an issue, a PR, a page), `claim` it with "
        "`--thread <repo#number>`. If the claim is refused, a colleague has it: do not "
        "duplicate their work.\n"
        "- Be brief and exact: identifiers, paths, commands. Any language or notation "
        "your colleague will read correctly is fine.\n"
        "- Ask a colleague when they know something you would otherwise spend minutes "
        "finding. Do not use chat to report status nobody asked for."
    )


def memory_guide(agent_id: str) -> str:
    """How a work session reaches its agent's own memory and identity."""
    return (
        "## 🧠 Your memory\n"
        f"You are working as {agent_id}, and {agent_id}'s memory is yours to use. Ask it "
        "for what you need when you need it:\n"
        '- `cortiva memory search "<what you want to know>"` finds what you have learned or '
        "done about something. With no query it returns what matters most to you now.\n"
        "- `cortiva memory identity <soul|identity|responsibilities|skills|plan>` reads who "
        "you are and what you hold yourself to.\n"
        '- `cortiva memory note --kind <lesson|decision|fact|plan> "<one sentence>"` writes '
        "back something worth keeping: a decision and its reason, a fact you had to find "
        "out, a lesson. Write what a colleague could act on, not a record that you worked.\n"
        "Search before you investigate something you may already know.\n"
        "\n"
        "When you have ACHIEVED something (it works, you have seen it work), write down how, "
        "so the next time is shorter:\n"
        '- `cortiva memory note --kind procedure "<steps>"`: the steps that got you there, '
        "one per line, in order, with the exact commands, paths and names. Leave out "
        "everything you tried that did not work. Someone following it should not repeat "
        "your detours.\n"
        '- `cortiva memory note --kind recovery "<what went wrong> -> <what fixed it>"`: one '
        "note for each false step that cost you time: the symptom as you saw it (the error "
        "text, the wrong result), then what got you past it. This is your cheat sheet for "
        "the next time the same thing goes wrong.\n"
        "Write neither for work that is not finished or that you have not seen working."
    )
