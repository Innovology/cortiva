"""What colleagues state as fact, checked where it can be checked.

A person told their manager a pull request was "reviewed, approved and
merged" when it was none of the three, and nothing looked (Mini-2,
2026-11-18). A statement about a pull request is read out of a message, the
truth is read from GitHub with the recipient's own access, and the result is
kept against the person who said it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FILE = ".claims.json"
KEEP_SEEN = 2000
KEEP_CHECKED = 2000

_REF = re.compile(
    r"(?:(?P<repo>[\w.-]+/[\w.-]+)#|(?:\bPR|\bpull request)\s*#?)(?P<num>\d{1,6})\b",
    re.IGNORECASE,
)
# What can be read back from GitHub, and the words that state it as done.
_STATED = {
    "approved": re.compile(r"\bapproved\b", re.IGNORECASE),
    "merged": re.compile(r"\b(?:merged|landed)\b", re.IGNORECASE),
    "reviewed": re.compile(r"\breviewed\b", re.IGNORECASE),
    "closed": re.compile(r"\bclosed\b", re.IGNORECASE),
    "green": re.compile(
        r"\b(?:checks?|ci|tests?)\b[^.;\n]{0,24}\b(?:green|pass(?:ing|ed|es)?)\b|\ball green\b",
        re.IGNORECASE,
    ),
}
# Not a statement that it has happened: asked for, awaited, conditional, denied.
_NOT_STATED = re.compile(
    r"\b(?:not|never|needs?|to be|once|when|if|until|after|before|please|will|should|must|"
    r"awaiting|pending|waiting|ready|cannot|unless|get|getting|whether|can)\b|n't",
    re.IGNORECASE,
)
SAID = {
    "approved": "approved",
    "merged": "merged",
    "reviewed": "reviewed",
    "closed": "closed",
    "green": "passing its checks",
}


def extract(text: str) -> list[tuple[str, int, str]]:
    """Statements about pull requests in ``text``, as (repo or '', number,
    kind). A sentence that names no pull request speaks of the message's
    only one, when there is exactly one."""
    text = text or ""
    every = {(m.group("repo") or "", int(m.group("num"))) for m in _REF.finditer(text)}
    only = next(iter(every)) if len({n for _, n in every}) == 1 else None
    out: list[tuple[str, int, str]] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if not sentence.strip() or sentence.rstrip().endswith("?"):
            continue
        named = [(m.group("repo") or "", int(m.group("num"))) for m in _REF.finditer(sentence)]
        about = named or ([only] if only else [])
        if not about:
            continue
        for kind, stated in _STATED.items():
            for m in stated.finditer(sentence):
                if _NOT_STATED.search(sentence[max(0, m.start() - 40) : m.start()]):
                    continue
                for repo, number in about:
                    if (repo, number, kind) not in out:
                        out.append((repo, number, kind))
                break
    # Named once in full and once by number alone, it is one pull request.
    full = {(number, kind) for repo, number, kind in out if repo}
    return [c for c in out if c[0] or (c[1], c[2]) not in full]


def holds(kind: str, pull: dict[str, Any]) -> bool:
    """Whether GitHub shows what was stated."""
    if kind == "merged":
        return bool(pull.get("merged"))
    if kind == "closed":
        return str(pull.get("state")) == "closed"
    if pull.get("merged"):
        return True  # it landed: reviewed, approved and passing are behind it
    if kind == "approved":
        return any(state == "APPROVED" for state, _ in pull["decisions"].values())
    if kind == "reviewed":
        return bool(pull.get("reviewers"))
    if kind == "green":
        return not pull["failing"] and not pull["running"]
    return True


def shows(pull: dict[str, Any]) -> str:
    """What GitHub shows for the pull request, in a few words."""
    if pull.get("merged"):
        return "merged"
    parts = [str(pull.get("state") or "open")]
    approvals = [who for who, (state, _) in pull["decisions"].items() if state == "APPROVED"]
    parts.append(f"approved by {', '.join(approvals)}" if approvals else "no approval")
    if not pull.get("reviewers"):
        parts.append("no review")
    if pull["failing"]:
        parts.append(f"{len(pull['failing'])} failing check(s)")
    elif pull["running"]:
        parts.append("checks still running")
    else:
        parts.append("checks passing")
    return ", ".join(parts)


def knowable(kind: str, pull: dict[str, Any]) -> bool:
    """False while GitHub cannot yet say: checks still running are neither
    passing nor failing. A statement read in that moment was called false
    seven seconds before the last check passed (Mini-2, 2026-11-18)."""
    return not (kind == "green" and pull["running"] and not pull["failing"])


def _load(agents_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((agents_dir / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("seen", [])
    data.setdefault("checked", [])
    for n, c in enumerate(data["checked"]):
        c.setdefault("id", f"r{n}-{c.get('at', '')}")  # kept before records had ids
    return data


def _save(agents_dir: Path, data: dict[str, Any]) -> None:
    data["seen"] = data["seen"][-KEEP_SEEN:]
    data["checked"] = data["checked"][-KEEP_CHECKED:]
    tmp = agents_dir / (FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(agents_dir / FILE)


def unseen(agents_dir: Path, message_id: str) -> bool:
    """True the first time a message is offered; it is then marked seen."""
    data = _load(agents_dir)
    if message_id in data["seen"]:
        return False
    data["seen"].append(message_id)
    _save(agents_dir, data)
    return True


def record(
    agents_dir: Path,
    *,
    by: str,
    to: str,
    ref: str,
    kind: str,
    true: bool,
    shown: str,
    pending: bool = False,
) -> None:
    """Keep one checked statement. ``pending`` marks a first look that found
    it false: it counts against nobody until a second look agrees."""
    import uuid

    data = _load(agents_dir)
    data["checked"].append(
        {
            "id": uuid.uuid4().hex,
            "pending": bool(pending),
            "at": datetime.now(UTC).isoformat(),
            "by": by,
            "to": to,
            "ref": ref,
            "kind": kind,
            "true": bool(true),
            "shown": shown,
        }
    )
    _save(agents_dir, data)


def records(agents_dir: Path) -> list[dict[str, Any]]:
    return list(_load(agents_dir)["checked"])


def settle(agents_dir: Path, record_id: str, **changes: Any) -> None:
    """Change one kept statement: a second look confirming or correcting it.
    ``drop=True`` removes it."""
    data = _load(agents_dir)
    if changes.pop("drop", False):
        data["checked"] = [c for c in data["checked"] if c.get("id") != record_id]
    else:
        for c in data["checked"]:
            if c.get("id") == record_id:
                c.update(changes)
    _save(agents_dir, data)


def standing(agents_dir: Path, days: int = 7) -> dict[str, dict[str, Any]]:
    """Per person: how many statements were checked in the last ``days``,
    how many were false, and the latest false one."""
    since = datetime.now(UTC) - timedelta(days=days)
    out: dict[str, dict[str, Any]] = {}
    for c in _load(agents_dir)["checked"]:
        try:
            at = datetime.fromisoformat(str(c.get("at")))
        except ValueError:
            continue
        if at < since or c.get("pending"):
            continue
        row = out.setdefault(str(c.get("by")), {"checked": 0, "false": 0, "latest": None})
        row["checked"] += 1
        if not c.get("true"):
            row["false"] += 1
            row["latest"] = c
    return out
