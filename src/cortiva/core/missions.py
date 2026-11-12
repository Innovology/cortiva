"""Missions — an objective is an end state the system observes for itself.

For a week (2026-11-05 → 11-12) a five-agent team worked an objective whose
last line read "done means live in production, not merged". The line was
prose. The team merged 37 pull requests, closed the issue, and told the
founder it was live; nothing had reached production, because a deployment
gate nobody on the team could see had held every release since day one.
Three things were missing, and none of them is about that objective:

* **Intent the team cannot author.** The end state lived in an issue body.
  Whoever closed the issue decided it was met.
* **A proven route.** Nobody pushed one trivial change all the way to where
  the end state is observed, so the gate went unfound for six days.
* **Status that is observed, not written.** One agent's correct "this is
  NOT live" became "live, loop closed" two relays later.

A mission fixes the three with one mechanism. The human who owns the
objective states the end state as conditions the fabric can check with no
help from the team (today: an HTTP request and what must come back). The
fabric runs the checks, records what it saw, and

* shows every member the observed state at plan, execute and reassess;
* asks for the route to be proven first, and counts the days it has not been;
* stamps the observed state on every report a member sends to a human, so
  no relay can upgrade it.

HQ owns the register; nodes receive the active set verbatim as
``<agents_dir>/.missions.json`` (the same shape as standing orders). The
fabric writes what it observed to ``<agents_dir>/.mission_state.json``.
Nothing here calls a model.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Org-wide file the node writes (verbatim mirror of HQ's active set).
MISSIONS_FILE = ".missions.json"
#: What the fabric observed, per mission. Rewritten on every probe.
STATE_FILE = ".mission_state.json"

#: Probe at most this often; a wake reads the file, it never waits on a probe.
PROBE_INTERVAL_S = 300.0
CHECK_TIMEOUT_S = 10.0
#: Enough of a page to find a phrase in; never a download.
MAX_BODY_BYTES = 512 * 1024
#: A route unproven for this long is the team's first problem, not a footnote.
ROUTE_OVERDUE_HOURS = 24.0

#: ``fetch(url, timeout) -> (status, body)``; raises on a transport failure.
Fetch = Callable[[str, float], tuple[int, str]]


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


def problems(mission: dict[str, Any]) -> list[str]:
    """Why this mission cannot be accepted (empty when it can).

    The rule the whole module rests on: no objective without an end state
    the system can observe. HQ applies it when a mission is issued; the
    fabric applies it again on load, so a hand-edited register cannot
    smuggle in an objective whose only judge is the team.
    """
    out: list[str] = []
    if not str(mission.get("mission_id") or "").strip():
        out.append("no mission_id")
    if not str(mission.get("title") or "").strip():
        out.append("no title")
    conditions = mission.get("end_state")
    if not isinstance(conditions, list) or not conditions:
        out.append("no end state: at least one condition the system can check is required")
        conditions = []
    seen: set[str] = set()
    for i, cond in enumerate(conditions):
        label = f"end_state[{i}]"
        if not isinstance(cond, dict):
            out.append(f"{label} is not an object")
            continue
        cid = str(cond.get("id") or "").strip()
        if not cid:
            out.append(f"{label} has no id")
        elif cid in seen:
            out.append(f"{label} repeats id {cid!r}")
        seen.add(cid)
        if not str(cond.get("describe") or "").strip():
            out.append(f"{label} has no description")
        out.extend(f"{label}: {p}" for p in _check_problems(cond.get("check")))
    route = mission.get("route")
    if route is not None:
        if not isinstance(route, dict) or not _is_http_url(route.get("url")):
            out.append("route needs an http(s) url where the team's change can be observed")
    return out


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list | tuple) else [value]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _check_problems(check: Any) -> list[str]:
    if not isinstance(check, dict):
        return ["no check"]
    if str(check.get("type") or "http") == "owner":
        return []
    if str(check.get("type") or "http") != "http":
        return [f"unknown check type {check.get('type')!r}"]
    out: list[str] = []
    if not _is_http_url(check.get("url")):
        out.append("check needs an http(s) url")
    asserts = [k for k in ("status", "contains", "absent", "json") if check.get(k) is not None]
    if not asserts:
        out.append("check asserts nothing (give status, contains, absent or json)")
    spec = check.get("json")
    if spec is not None:
        if not isinstance(spec, dict) or not str(spec.get("path") or "").strip():
            out.append("json check needs a path")
        elif not any(k in spec for k in ("equals", "min", "exists")):
            out.append("json check needs equals, min or exists")
    return out


def load(agents_dir: Path) -> list[dict[str, Any]]:
    """The org's active, acceptable missions (empty when none / unreadable)."""
    path = Path(agents_dir) / MISSIONS_FILE
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8")) or []
    except (ValueError, OSError):
        logger.debug("unreadable %s", path, exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict) or m.get("status", "active") != "active":
            continue
        why = problems(m)
        if why:
            logger.warning(
                "Mission %r not accepted: %s", m.get("mission_id") or m.get("title"), "; ".join(why)
            )
            continue
        out.append(m)
    return out


def for_agent(missions: list[dict[str, Any]], agent_id: str) -> list[dict[str, Any]]:
    """Missions this agent is on. An empty team means the whole org."""
    aid = str(agent_id or "").lower()
    out = []
    for m in missions:
        team = [str(t).lower() for t in _as_list(m.get("team")) if str(t).strip()]
        if not team or aid in team:
            out.append(m)
    return out


def route_of(mission: dict[str, Any]) -> dict[str, str] | None:
    """The route proof this mission asks for: a url and the token that must
    appear there. The token is the system's, derived from the mission id, so
    a page that already answers proves nothing — only a change the team got
    all the way through does."""
    route = mission.get("route")
    if not isinstance(route, dict) or not _is_http_url(route.get("url")):
        return None
    token = str(route.get("token") or "").strip()
    if not token:
        mid = "".join(ch for ch in str(mission.get("mission_id") or "") if ch.isalnum())
        token = f"cortiva-route-{mid[-8:].lower() or 'proof'}"
    return {"url": str(route["url"]), "token": token}


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


def _default_fetch(url: str, timeout: float) -> tuple[int, str]:
    """GET ``url`` without following redirects: a sign-in wall that answers
    302 must be seen as 302, not as the 200 of the page it sends you to."""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a: Any, **_k: Any) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "cortiva-mission-probe"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return int(r.status), r.read(MAX_BODY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        except Exception:
            body = ""
        return int(e.code), body


def _json_at(doc: Any, path: str) -> tuple[bool, Any]:
    cur = doc
    for part in [p for p in str(path).split(".") if p != ""]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and cur:
            try:
                cur = cur[int(part)]
            except IndexError:
                return False, None
        else:
            return False, None
    return True, cur


def run_check(check: dict[str, Any], fetch: Fetch | None = None) -> tuple[bool, str]:
    """Run one check. Returns ``(met, what was observed)``. Never raises:
    a check that could not be run is not met, and says why.

    ``type: owner`` is for what the system cannot see (a person signed in, a
    customer said yes). The judge is then the human who issued the mission,
    through the register HQ owns: it is met when the register carries
    ``confirmed_at``. It is never the team's to set."""
    if str(check.get("type") or "http") == "owner":
        when = str(check.get("confirmed_at") or "").strip()
        if when:
            return True, f"confirmed by the mission's owner, {_clock(when)}"
        return False, "only the mission's owner can confirm this, and has not yet"
    url = str(check.get("url") or "")
    try:
        status, body = (fetch or _default_fetch)(url, CHECK_TIMEOUT_S)
    except Exception as e:
        return False, f"no answer from {url} ({type(e).__name__})"
    seen = [f"HTTP {status}"]
    met = True
    wanted = [int(s) for s in _as_list(check.get("status")) if str(s).lstrip("-").isdigit()]
    if wanted and status not in wanted:
        met = False
        seen.append(f"wanted {' or '.join(str(s) for s in wanted)}")
    for phrase in (str(p) for p in _as_list(check.get("contains"))):
        if phrase.lower() in body.lower():
            seen.append(f"found {phrase!r}")
        else:
            met = False
            seen.append(f"{phrase!r} not on the page")
    for phrase in (str(p) for p in _as_list(check.get("absent"))):
        if phrase.lower() in body.lower():
            met = False
            seen.append(f"{phrase!r} still on the page")
    spec = check.get("json")
    if isinstance(spec, dict):
        try:
            found, value = _json_at(json.loads(body), str(spec.get("path") or ""))
        except ValueError:
            found, value = False, None
            seen.append("body is not JSON")
        path = spec.get("path")
        if not found:
            if spec.get("exists") is False:
                seen.append(f"{path} absent")
            else:
                met = False
                seen.append(f"{path} missing")
        else:
            seen.append(f"{path} = {value!r}"[:120])
            if spec.get("exists") is False:
                met = False
            if "equals" in spec and value != spec["equals"]:
                met = False
                seen.append(f"wanted {spec['equals']!r}")
            if "min" in spec:
                try:
                    if float(value) < float(spec["min"]):
                        met = False
                        seen.append(f"wanted at least {spec['min']}")
                except (TypeError, ValueError):
                    met = False
                    seen.append("not a number")
    return met, "; ".join(seen)


def load_state(agents_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(agents_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(agents_dir: Path, state: dict[str, Any]) -> None:
    path = Path(agents_dir) / STATE_FILE
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.debug("could not write %s", path, exc_info=True)


def _observe(prev: dict[str, Any], met: bool, observed: str, now_iso: str) -> dict[str, Any]:
    """Fold one observation into a condition's record, keeping when the
    current answer began and when it was first met."""
    changed = not prev or bool(prev.get("met")) != met
    return {
        "met": met,
        "observed": observed,
        "since": now_iso if changed else str(prev.get("since") or now_iso),
        "first_met_at": str(prev.get("first_met_at") or "") or (now_iso if met else ""),
    }


def probe(
    agents_dir: Path,
    *,
    fetch: Fetch | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check every active mission's end state and route, fold the result
    into the state file, and return the new state. Blocking (network): call
    it off the event loop."""
    now_iso = (now or datetime.now(UTC)).isoformat()
    old = load_state(agents_dir)
    state: dict[str, Any] = {}
    for m in load(agents_dir):
        mid = str(m["mission_id"])
        prev = _dict(old.get(mid))
        prev_conds = _dict(prev.get("conditions"))
        conds: dict[str, Any] = {}
        for cond in m["end_state"]:
            cid = str(cond["id"])
            met, observed = run_check(cond["check"], fetch)
            conds[cid] = _observe(_dict(prev_conds.get(cid)), met, observed, now_iso)
        entry: dict[str, Any] = {
            "checked_at": now_iso,
            "first_checked_at": str(prev.get("first_checked_at") or now_iso),
            "conditions": conds,
            "met": sum(1 for c in conds.values() if c["met"]),
            "total": len(conds),
        }
        # When anything last moved. A mission whose conditions have not moved
        # since the first look is one where nothing the team did has arrived.
        moved = [c["since"] for c in conds.values() if c["since"] != entry["first_checked_at"]]
        entry["last_change_at"] = max(moved) if moved else ""
        route = route_of(m)
        if route:
            prev_route = _dict(prev.get("route"))
            if prev_route.get("first_met_at"):
                # Proven once is proven: the token may be removed afterwards.
                entry["route"] = dict(prev_route)
            else:
                met, observed = run_check(
                    {"url": route["url"], "contains": route["token"]},
                    fetch,
                )
                entry["route"] = _observe(prev_route, met, observed, now_iso)
        complete = entry["met"] == entry["total"]
        entry["complete"] = complete
        entry["completed_at"] = (str(prev.get("completed_at") or "") or now_iso) if complete else ""
        state[mid] = entry
    save_state(agents_dir, state)
    return state


# ---------------------------------------------------------------------------
# What the team sees, and what a human is told
# ---------------------------------------------------------------------------


def _hours_between(start_iso: str, end: datetime) -> float | None:
    try:
        start = datetime.fromisoformat(str(start_iso))
    except (TypeError, ValueError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _age(hours: float | None) -> str:
    if hours is None:
        return "an unknown time"
    if hours < 1:
        return "under an hour"
    if hours < 48:
        return f"{int(hours)} h"
    return f"{int(hours // 24)} days"


def _clock(iso: str) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone(UTC).strftime("%d %b %H:%M UTC")
    except (TypeError, ValueError):
        return "not yet"


def _route_lines(m: dict[str, Any], entry: dict[str, Any], now: datetime) -> list[str]:
    route = route_of(m)
    if not route:
        return []
    seen = entry.get("route") or {}
    if seen.get("first_met_at"):
        return [f"Route proven {_clock(seen['first_met_at'])}: a change of yours reached it."]
    waited = _hours_between(str(m.get("issued_at") or entry.get("first_checked_at") or ""), now)
    overdue = waited is not None and waited >= ROUTE_OVERDUE_HOURS
    lines = [
        f"**ROUTE NOT PROVEN — {_age(waited)} since this mission was issued."
        f"{' This is now the first problem.' if overdue else ''}**",
        f"Before any other work on this mission, make the text `{route['token']}` appear at "
        f"{route['url']}, by the same path your real work will travel. It is a rehearsal: "
        "it finds every gate between your work and the end state while the change is trivial.",
        "If a step on that path is one you cannot see or cannot do, that is the finding. "
        f"Send ONE message to {_issuer(m)} naming the step, the place and what you need. "
        "Do not build further behind a gate you have not passed.",
    ]
    return lines


def _issuer(m: dict[str, Any]) -> str:
    by = m.get("issued_by") or {}
    if isinstance(by, dict):
        return str(by.get("name") or by.get("address") or "the person who issued it")
    return str(by or "the person who issued it")


def context_block(
    missions: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    """The block injected at plan / execute / reassess for a member."""
    if not missions:
        return ""
    now = now or datetime.now(UTC)
    lines = [
        "## 🎯 Missions — the end state, as the system observed it\n",
        "The checks below are run by the system against the real thing. They are the only "
        "judge of done. Nothing you write, close, merge or report changes them; only the "
        "world changing does. Say what they say. If you believe a check is wrong, tell the "
        "person who issued the mission; do not report around it.\n",
    ]
    for m in missions:
        entry = state.get(str(m["mission_id"])) or {}
        conds = entry.get("conditions") or {}
        total = len(m["end_state"])
        if not entry:
            lines.append(f"### {m['title']} — not yet observed\n")
        elif entry.get("complete"):
            lines.append(
                f"### {m['title']} — END STATE REACHED {_clock(entry.get('completed_at', ''))}\n"
            )
        else:
            lines.append(
                f"### {m['title']} — {entry.get('met', 0)} of {total} conditions met "
                f"(observed {_clock(entry.get('checked_at', ''))})\n"
            )
        intent = str(m.get("intent") or "").strip()
        if intent:
            lines.append(f"Intent ({_issuer(m)}): {intent}\n")
        lines.extend(_route_lines(m, entry, now))
        for cond in m["end_state"]:
            seen = conds.get(str(cond["id"])) or {}
            if not seen:
                lines.append(f"- ⏳ {cond['describe']} — not yet checked")
            elif seen.get("met"):
                lines.append(f"- ✅ {cond['describe']} — met since {_clock(seen.get('since', ''))}")
            else:
                lines.append(f"- ❌ {cond['describe']} — NOT MET ({seen.get('observed', '')})")
                if str((cond.get("check") or {}).get("type") or "") == "owner":
                    lines.append(
                        f"  Make it true, then ask {_issuer(m)} ONCE to confirm it, saying "
                        "exactly what to do and where to look. Do not ask before it is true."
                    )
        if entry and not entry.get("complete"):
            if entry.get("last_change_at"):
                idle = _hours_between(entry["last_change_at"], now)
                lines.append(f"\nLast time any condition moved: {_age(idle)} ago.")
            else:
                watched = _hours_between(str(entry.get("first_checked_at") or ""), now)
                line = f"\nNo condition has moved since the system began watching, {_age(watched)} ago."
                if watched is not None and watched >= ROUTE_OVERDUE_HOURS:
                    line += (
                        " Nothing this team has done has yet arrived where the end state "
                        "is observed."
                    )
                lines.append(line)
            lines.append(
                "Your next action is whatever moves the first ❌. Work that moves no "
                "condition is not work on this mission."
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def status_stamp(missions: list[dict[str, Any]], state: dict[str, Any]) -> str:
    """What the system appends to a member's report to a human: the observed
    state of each mission, in the system's words. It travels beside the body,
    not inside it, so a report can add to what was observed and cannot
    replace it."""
    if not missions:
        return ""
    lines = [
        "—",
        "Mission status as observed by the system. This is added automatically; "
        "the sender did not write it and cannot change it.",
    ]
    for m in missions:
        entry = state.get(str(m["mission_id"])) or {}
        conds = entry.get("conditions") or {}
        if not entry:
            lines.append(f"{m['title']}: not yet observed.")
            continue
        head = (
            "end state reached"
            if entry.get("complete")
            else f"{entry.get('met', 0)} of {len(m['end_state'])} conditions met"
        )
        lines.append(f"{m['title']}: {head} (checked {_clock(entry.get('checked_at', ''))}).")
        route = entry.get("route")
        if route is not None and not route.get("first_met_at"):
            lines.append("  ✗ route to the end state not yet proven")
        for cond in m["end_state"]:
            seen = conds.get(str(cond["id"])) or {}
            mark = "✓" if seen.get("met") else "✗"
            lines.append(f"  {mark} {cond['describe']}")
    return "\n".join(lines)
