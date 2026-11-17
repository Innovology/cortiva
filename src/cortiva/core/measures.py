"""Numbers a company is held to, read by the system from their sources.

An agent declares a number: what it counts, who owns it, and where the
system can read it. A number may be declared as driving another, so the
numbers form a tree with the outcome at the top and what feeds it
beneath: a funnel is a tree with one driver at each level, a profit
figure is one with several. The fabric reads every number itself on a
schedule; nobody reports one.

Nothing here knows what any number means. What is counted, the stages of
a chain and the place each is read from are the company's, written by its
own agents.

Files, beside the agent directories:

- ``.measures.json``       what was declared
- ``.measure_state.json``  what the fabric last read
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cortiva.core.missions import (
    CHECK_TIMEOUT_S,
    _age,
    _default_fetch,
    _hours_between,
    _is_http_url,
    _json_at,
)

logger = logging.getLogger(__name__)

MEASURES_FILE = ".measures.json"
STATE_FILE = ".measure_state.json"
PROBE_INTERVAL_S = 300.0
MAX_MEASURES = 200

Fetch = Callable[[str, float], tuple[int, str]]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:60]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.debug("could not write %s", path, exc_info=True)


def load(agents_dir: Path) -> list[dict[str, Any]]:
    data = _read_json(Path(agents_dir) / MEASURES_FILE)
    rows = data.get("measures") if isinstance(data, dict) else data
    return [m for m in rows or [] if isinstance(m, dict) and m.get("id")]


def load_state(agents_dir: Path) -> dict[str, Any]:
    data = _read_json(Path(agents_dir) / STATE_FILE)
    return data if isinstance(data, dict) else {}


def declare(
    agents_dir: Path,
    owner: str,
    *,
    name: str,
    what: str = "",
    url: str = "",
    path: str = "",
    drives: str = "",
    better: str = "up",
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Declare a number, or change one the same agent declared.

    A number may be declared before anything exposes it: it then stands in
    the chain as having no source, which is the fact the team has to fix."""
    mid = _slug(name)
    if not mid:
        return False, "a number needs a name"
    if url and not _is_http_url(url):
        return False, "the source must be an http(s) address the system can read"
    measures = load(agents_dir)
    by_id = {str(m["id"]): m for m in measures}
    drives_id = _slug(drives) if drives else ""
    if drives_id and drives_id not in by_id:
        known = ", ".join(sorted(by_id)) or "none declared yet"
        return False, f"'{drives}' is not a declared number (declared: {known})"
    if drives_id == mid:
        return False, "a number cannot drive itself"
    seen, cur = {mid}, drives_id
    while cur:
        if cur in seen:
            return False, "that would run in a circle"
        seen.add(cur)
        cur = str((by_id.get(cur) or {}).get("drives") or "")
    better = str(better or "up").strip().lower()
    if better not in ("up", "down"):
        return False, "better must be 'up' or 'down'"
    existing = by_id.get(mid)
    if existing is not None and str(existing.get("owner")) != owner:
        return False, f"'{mid}' is {existing.get('owner')}'s number; theirs to change"
    if existing is None and len(measures) >= MAX_MEASURES:
        return False, "too many numbers declared"
    row = {
        "id": mid,
        "name": str(name).strip()[:120],
        "what": str(what).strip()[:400],
        "owner": owner,
        "source": {"url": str(url).strip(), "path": str(path).strip()},
        "drives": drives_id,
        "better": better,
        "declared_at": (now or datetime.now(UTC)).isoformat(),
    }
    if existing is None:
        measures.append(row)
    else:
        existing.update(row)
    _write_json(Path(agents_dir) / MEASURES_FILE, {"measures": measures})
    return True, mid


def read_one(measure: dict[str, Any], fetch: Fetch | None = None) -> tuple[float | None, str]:
    """Read one number from its source. ``(value, note)``; never raises.
    A number that could not be read is ``None`` with the reason."""
    source = measure.get("source") if isinstance(measure.get("source"), dict) else {}
    url = str((source or {}).get("url") or "")
    if not url:
        return None, "no source yet: nothing exposes this number"
    try:
        status, body = (fetch or _default_fetch)(url, CHECK_TIMEOUT_S)
    except Exception as e:
        return None, f"no answer from the source ({type(e).__name__})"
    if status != 200:
        return None, f"the source answered HTTP {status}"
    path = str((source or {}).get("path") or "")
    raw: Any = body.strip()
    try:
        found, raw = _json_at(json.loads(body), path)
        if not found:
            return None, f"'{path}' is not in what the source returned"
    except ValueError:
        if path:
            return None, "the source did not return JSON"
    try:
        if isinstance(raw, bool):
            raise TypeError
        return float(raw), ""
    except (TypeError, ValueError):
        return None, "the source did not return a number"


def probe(
    agents_dir: Path, fetch: Fetch | None = None, now: datetime | None = None
) -> list[tuple[str, float | None, float]]:
    """Read every declared number and record it. Returns what moved, as
    ``(id, value before, value now)``."""
    now_iso = (now or datetime.now(UTC)).isoformat()
    state = load_state(agents_dir)
    moved: list[tuple[str, float | None, float]] = []
    for m in load(agents_dir):
        mid = str(m["id"])
        prev = state.get(mid) if isinstance(state.get(mid), dict) else {}
        prev = prev or {}
        value, note = read_one(m, fetch)
        entry = dict(prev)
        entry["read_at"] = now_iso
        entry["note"] = note
        if value is not None:
            before = prev.get("value")
            if before is None or float(before) != value:
                entry["moved_at"] = now_iso
                entry["before"] = before
                if before is not None:
                    moved.append((mid, float(before), value))
            entry["value"] = value
            entry.setdefault("first_read_at", now_iso)
        state[mid] = entry
    _write_json(Path(agents_dir) / STATE_FILE, state)
    return moved


def tree(measures: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Every number with its depth: an outcome at 0, what drives it at 1,
    what drives those at 2. Outcomes come first, each followed by its
    drivers."""
    by_id = {str(m["id"]): m for m in measures}
    drivers: dict[str, list[dict[str, Any]]] = {}
    for m in measures:
        drivers.setdefault(str(m.get("drives") or ""), []).append(m)
    out: list[tuple[int, dict[str, Any]]] = []

    def _walk(m: dict[str, Any], depth: int) -> None:
        out.append((depth, m))
        for d in drivers.get(str(m["id"])) or []:
            _walk(d, depth + 1)

    for m in measures:
        if not m.get("drives") or str(m.get("drives")) not in by_id:
            _walk(m, 0)
    return out


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def context_block(
    measures: list[dict[str, Any]],
    state: dict[str, Any],
    agent_id: str,
    now: datetime | None = None,
) -> str:
    """The numbers as they stand, for any agent's context."""
    if not measures:
        return ""
    now = now or datetime.now(UTC)

    def _value(m: dict[str, Any]) -> float | None:
        entry = state.get(str(m["id"]))
        value = entry.get("value") if isinstance(entry, dict) else None
        return float(value) if value is not None else None

    drivers: dict[str, list[dict[str, Any]]] = {}
    for m in measures:
        drivers.setdefault(str(m.get("drives") or ""), []).append(m)
    lines = [
        "## 📈 The numbers this company is held to",
        "Read by the system from their sources. Nobody reports them, and saying one has "
        "moved does not move it. Each number is followed, indented, by what drives it.",
        "",
    ]
    empty: list[tuple[int, str]] = []
    for depth, m in tree(measures):
        entry = state.get(str(m["id"])) if isinstance(state.get(str(m["id"])), dict) else {}
        entry = entry or {}
        value = _value(m)
        down = str(m.get("better")) == "down"
        who = "yours" if str(m.get("owner")) == agent_id else str(m.get("owner"))
        if value is None:
            shown = str(entry.get("note") or "not read yet")
        else:
            moved = _hours_between(str(entry.get("moved_at") or ""), now)
            shown = _fmt(value) + (f", unchanged for {_age(moved)}" if moved is not None else "")
            fed_by = drivers.get(str(m["id"])) or []
            # One driver is a funnel step: show how much of it came through.
            if len(fed_by) == 1 and not down:
                below = _value(fed_by[0])
                if below:
                    shown += f" — {value / below:.1%} of {fed_by[0].get('name')}"
        tag = f"{who}, lower is better" if down else who
        lines.append(f"{'  ' * depth}- **{m.get('name')}** ({tag}): {shown}")
        if value is None or (value == 0 and not down):
            empty.append((depth, str(m.get("name"))))
    if empty and len(measures) > 1:
        deepest = max(d for d, _ in empty)
        names = ", ".join(f"**{n}**" for d, n in empty if d == deepest)
        lines += [
            "",
            f"Nothing to show yet, furthest down: {names}. What these drive cannot "
            "move, or be judged, until they do.",
        ]
    return "\n".join(lines)


def guide() -> str:
    """How a work session declares and reads numbers."""
    return (
        "## 📈 Numbers\n"
        "A number you are held to counts only if the system can read it itself.\n"
        "- `cortiva measure list` shows every declared number as last read.\n"
        '- `cortiva measure declare --name "<short name>" --what "<what it counts>" '
        '--url <address returning it> --path <field in the JSON> [--drives "<the number '
        'it feeds>"] [--better down]` declares one, or changes one of yours. Start with '
        "the number you are judged by, then declare what drives it, then what drives "
        "those. Leave out `--url` for a number nothing exposes yet: it stands as having "
        "no source until someone builds one."
    )
