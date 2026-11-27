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

A condition the system cannot see without help is the team's to prove
(``type: team``), not the owner's to confirm. A register that read "the
founder has signed in" and "the founder has seen a real lead" told five
people for six and a half days that nothing was theirs to do (Mini-2,
2026-11-12 to 11-19), when any of them could sign in, build themselves a way
in and judge a lead as well as the founder. One member shows the system an
address where it can read the thing for itself; a second member does it and
puts their name to what they saw. ``type: owner`` remains for what truly
only that person can do.
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
#: What the team has shown and seen, per condition. Written by the fabric
#: when a member says so; a probe only reads it.
PROOFS_FILE = ".mission_proofs.json"
#: A member's account of what they did and saw is a sentence, not a tick.
MIN_SAW_CHARS = 40

#: Probe at most this often; a wake reads the file, it never waits on a probe.
PROBE_INTERVAL_S = 300.0
CHECK_TIMEOUT_S = 10.0
#: Enough of a page to find a phrase in; never a download.
MAX_BODY_BYTES = 512 * 1024
#: A route unproven for this long is the team's first problem, not a footnote.
ROUTE_OVERDUE_HOURS = 24.0
#: A mission where nothing has moved for this long is standing still, however
#: busy the team is. Nobody has to say they are blocked for that to be so.
STILL_AFTER_HOURS = 12.0

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
    if str(check.get("type") or "http") in ("owner", "team"):
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


def _default_fetch(url: str, timeout: float, bearer: str = "") -> tuple[int, str]:
    """GET ``url`` without following redirects: a sign-in wall that answers
    302 must be seen as 302, not as the 200 of the page it sends you to."""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a: Any, **_k: Any) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    headers = {"User-Agent": "cortiva-mission-probe"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=headers)
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


def run_check(
    check: dict[str, Any], fetch: Fetch | None = None, bearer: str = ""
) -> tuple[bool, str]:
    """Run one check. Returns ``(met, what was observed)``. Never raises:
    a check that could not be run is not met, and says why.

    ``type: owner`` is for what only the person who issued the mission can
    do. It is met when the register carries ``confirmed_at``, and is never
    the team's to set. What the team can do and show for itself is
    ``type: team``; see :func:`run_team_check`."""
    if str(check.get("type") or "http") == "owner":
        when = str(check.get("confirmed_at") or "").strip()
        if when:
            return True, f"confirmed by the mission's owner, {_clock(when)}"
        return False, "only the mission's owner can confirm this, and has not yet"
    url = str(check.get("url") or "")
    try:
        if bearer and fetch is None:
            status, body = _default_fetch(url, CHECK_TIMEOUT_S, bearer)
        else:
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


# ---------------------------------------------------------------------------
# What the team proves for itself
# ---------------------------------------------------------------------------


def _host(url: str) -> str:
    import urllib.parse

    return (urllib.parse.urlsplit(str(url)).hostname or "").lower()


def _site(host: str) -> str:
    return ".".join(host.split(".")[-2:])


def mission_sites(mission: dict[str, Any]) -> set[str]:
    """The sites this mission is about: where its other checks and its route
    look. Proof is read from the thing itself. A number a member typed into
    a page of their own is a report, and the system does not read reports."""
    urls = [str(_dict(c.get("check")).get("url") or "") for c in mission.get("end_state") or []]
    urls.append(str(_dict(mission.get("route")).get("url") or ""))
    return {_site(_host(u)) for u in urls if _host(u)}


def load_proofs(agents_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(agents_dir) / PROOFS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_proofs(agents_dir: Path, proofs: dict[str, Any]) -> None:
    path = Path(agents_dir) / PROOFS_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(proofs, indent=2), encoding="utf-8")
    tmp.replace(path)


def _team_condition(
    missions: list[dict[str, Any]], member: str, condition: str, mission_id: str = ""
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """The mission and the team-proved condition a member means."""
    cid = str(condition or "").strip()
    mine = for_agent(missions, member)
    if mission_id:
        mine = [m for m in mine if str(m["mission_id"]) == mission_id]
    open_ids: list[str] = []
    for m in mine:
        for cond in m["end_state"]:
            if str(_dict(cond.get("check")).get("type") or "") != "team":
                continue
            open_ids.append(str(cond["id"]))
            if str(cond["id"]) == cid:
                return m, cond, ""
    if not mine:
        return None, None, "you are on no such mission"
    known = ", ".join(open_ids) or "none"
    return None, None, f"'{cid}' is not a condition the team proves (those are: {known})"


def _secret(agents_dir: Path, member: str, token_file: str) -> tuple[str, str]:
    """The credential a member keeps in a file under their own directory.
    ``(secret, problem)``. The file is named in the proof; its content never is."""
    if not token_file:
        return "", ""
    home = (Path(agents_dir) / member).resolve()
    path = Path(token_file)
    path = (path if path.is_absolute() else home / path).resolve()
    if home != path and home not in path.parents:
        return "", f"the credential file must be under {home}"
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "", f"the credential file {path} cannot be read"
    if not secret or len(secret) > 4096 or "\n" in secret:
        return "", f"the credential file {path} does not hold one credential"
    return secret, ""


def prove(
    agents_dir: Path,
    missions: list[dict[str, Any]],
    member: str,
    *,
    condition: str,
    url: str = "",
    path: str = "",
    minimum: float | None = None,
    contains: str = "",
    token_file: str = "",
    sent_to: str = "",
    mission_id: str = "",
    now: datetime | None = None,
    fetch: Fetch | None = None,
) -> tuple[bool, str]:
    """A member shows the system where it can read a condition for itself:
    an address on the thing, or a message the system itself will have sent.
    Showing it again replaces what was shown and clears who had seen it.

    A team whose notification email could not go out spent sixteen hours on
    the mail key, because a member writing to the founder by hand was
    something the system had no way to read (Mini-2, 2026-11-20).

    The system reads the address as it is shown and keeps what it read. An
    open address that already reads the way that is asked shows nothing
    about anyone doing the thing: a member was told to show a health page
    that had answered "ok" for a week as proof of a sign-in (Mini-2,
    2026-11-19)."""
    m, cond, why = _team_condition(missions, member, condition, mission_id)
    if m is None or cond is None:
        return False, why
    to = str(sent_to or "").strip().lower()
    if to:
        if url or path or token_file:
            return False, "show an address or a message sent, one or the other"
        if "@" not in to:
            return False, "say who the message goes to, as an address"
        kept: dict[str, Any] = {
            "by": member,
            "at": (now or datetime.now(UTC)).isoformat(),
            "sent_to": to,
        }
        if contains:
            kept["contains"] = str(contains)
        proofs = load_proofs(agents_dir)
        proofs.setdefault(str(m["mission_id"]), {})[str(cond["id"])] = {"shown": kept, "seen": []}
        try:
            _save_proofs(agents_dir, proofs)
        except OSError:
            return False, "the proof could not be kept"
        return True, str(m["mission_id"])
    if not _is_http_url(url):
        return False, "give an http(s) address the system can read, or --sent-to <who is told>"
    sites = mission_sites(m)
    if sites and _site(_host(url)) not in sites:
        return False, (
            f"the address must be on the thing this mission is about ({', '.join(sorted(sites))}), "
            "so the system reads the thing itself and not a copy somebody wrote down"
        )
    if not path and not contains:
        return False, "say what the system should find there: --path (with --min) or --contains"
    if token_file:
        _, problem = _secret(agents_dir, member, token_file)
        if problem:
            return False, problem
    shown: dict[str, Any] = {
        "by": member,
        "at": (now or datetime.now(UTC)).isoformat(),
        "url": str(url).strip(),
    }
    if path:
        shown["path"] = str(path).strip()
        shown["min"] = float(minimum) if minimum is not None else 1.0
    if contains:
        shown["contains"] = str(contains)
    if token_file:
        shown["token_file"] = str(token_file)
    else:
        met, observed = run_check(_shown_check(shown), fetch)
        shown["read_when_shown"] = {"met": bool(met), "observed": str(observed)[:200]}
    proofs = load_proofs(agents_dir)
    proofs.setdefault(str(m["mission_id"]), {})[str(cond["id"])] = {"shown": shown, "seen": []}
    try:
        _save_proofs(agents_dir, proofs)
    except OSError:
        return False, "the proof could not be kept"
    return True, str(m["mission_id"])


def _shown_check(shown: dict[str, Any]) -> dict[str, Any]:
    """What the system looks for at the address a member showed."""
    check: dict[str, Any] = {"url": str(shown.get("url") or ""), "status": 200}
    if shown.get("path"):
        check["json"] = {"path": shown["path"], "min": shown.get("min", 1.0)}
    if shown.get("contains"):
        check["contains"] = shown["contains"]
    return check


def _sent_since(
    agents_dir: Path, mission: dict[str, Any], shown: dict[str, Any]
) -> tuple[bool, str]:
    """Whether the system has sent a member's message to the address shown,
    since it was shown. Read from the system's own record of what it sent."""
    to = str(shown.get("sent_to") or "").lower()
    want = str(shown.get("contains") or "").lower()
    try:
        since = datetime.fromisoformat(str(shown.get("at") or ""))
    except ValueError:
        return False, "what was shown carries no time"
    team = [str(t).lower() for t in _as_list(mission.get("team")) if str(t).strip()]
    try:
        dirs = sorted(d for d in Path(agents_dir).iterdir() if d.is_dir())
    except OSError:
        dirs = []
    for d in dirs:
        if team and d.name.lower() not in team:
            continue
        for f in sorted((d / "outbox" / "email" / "sent").glob("*.json")):
            try:
                mail = json.loads(f.read_text(encoding="utf-8"))
                when = datetime.fromisoformat(str(mail.get("sent_at") or ""))
            except (OSError, ValueError):
                continue
            if when <= since or to not in str(mail.get("to") or "").lower():
                continue
            text = f"{mail.get('subject') or ''}\n{mail.get('body') or ''}"
            if want and want not in text.lower():
                continue
            return True, (
                f'the system sent "{str(mail.get("subject") or "")[:80]}" from {d.name} '
                f"to {to}, {_clock(when.isoformat())}"
            )
    saying = f' saying "{shown.get("contains")}"' if want else ""
    return False, (
        f"no message from the team to {to}{saying} has been sent since {_clock(since.isoformat())}"
    )


def witness(
    agents_dir: Path,
    missions: list[dict[str, Any]],
    member: str,
    *,
    condition: str,
    saw: str,
    mission_id: str = "",
    now: datetime | None = None,
) -> tuple[bool, str]:
    """A second member, having done it themselves, says what they saw."""
    m, cond, why = _team_condition(missions, member, condition, mission_id)
    if m is None or cond is None:
        return False, why
    said = " ".join(str(saw or "").split())
    if len(said) < MIN_SAW_CHARS:
        return False, "say what you did and what you saw, in a sentence or two"
    proofs = load_proofs(agents_dir)
    proof = _dict(_dict(proofs.get(str(m["mission_id"]))).get(str(cond["id"])))
    shown = _dict(proof.get("shown"))
    if not shown:
        return False, (
            "nobody has shown the system where to read this yet; that comes first "
            "(`cortiva mission prove`)"
        )
    if str(shown.get("by")) == member:
        return False, (
            "you showed the system this one, so you cannot also be the one who vouches "
            "for it; a second member does it themselves and says what they saw"
        )
    seen = [s for s in proof.get("seen") or [] if str(_dict(s).get("by")) != member]
    seen.append({"by": member, "at": (now or datetime.now(UTC)).isoformat(), "saw": said[:600]})
    proofs[str(m["mission_id"])][str(cond["id"])] = {"shown": shown, "seen": seen}
    try:
        _save_proofs(agents_dir, proofs)
    except OSError:
        return False, "what you saw could not be kept"
    return True, str(m["mission_id"])


def amend(
    agents_dir: Path,
    by: str,
    *,
    condition: str,
    describe: str = "",
    prove_by: str = "",
    mission_id: str = "",
    now: datetime | None = None,
) -> tuple[bool, str]:
    """The register's owner rewords a condition, or changes who proves it.

    Only what the system cannot read for itself may be changed: a condition
    the owner confirms or the team proves. What the system observes directly
    stays as issued. Every change keeps what the condition said before and
    who changed it, and reports to a human carry that. The caller has
    established that ``by`` owns the register."""
    path = Path(agents_dir) / MISSIONS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "there is no mission register to change"
    items = raw.get("missions") if isinstance(raw, dict) else raw
    cid = str(condition or "").strip()
    said = " ".join(str(describe or "").split())
    kind = str(prove_by or "").strip().lower()
    if kind not in ("", "team", "owner"):
        return False, "a condition is proved by 'team' or confirmed by 'owner'"
    if not said and not kind:
        return False, "say what changes: --describe, --prove-by, or both"
    if said and len(said) < 20:
        return False, "describe the condition in a full sentence"
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict) or m.get("status", "active") != "active":
            continue
        if mission_id and str(m.get("mission_id")) != mission_id:
            continue
        for cond in m.get("end_state") or []:
            if not isinstance(cond, dict) or str(cond.get("id")) != cid:
                continue
            check = _dict(cond.get("check"))
            was = str(check.get("type") or "http")
            if was not in ("owner", "team"):
                return False, (
                    f"'{cid}' is read by the system itself and stays as it was issued; "
                    "only a condition someone confirms or proves can be changed"
                )
            cond.setdefault("amended", []).append(
                {
                    "by": by,
                    "at": (now or datetime.now(UTC)).isoformat(),
                    "was": str(cond.get("describe") or ""),
                    "was_proved_by": was,
                }
            )
            if said:
                cond["describe"] = said[:400]
            if kind:
                check["type"] = kind
                cond["check"] = check
            why = problems(m)
            if why:
                return False, "; ".join(why)
            try:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
                tmp.replace(path)
            except OSError:
                return False, "the register could not be written"
            return True, str(m.get("mission_id"))
    return False, f"'{cid}' is not a condition of an active mission"


def restate(
    agents_dir: Path,
    *,
    intent: str = "",
    why: str = "",
    mission_id: str = "",
    by: str = "",
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Whoever issued a mission says again what it is to reach, why it is
    being done, or both.

    The end without the reason is a list to tick: a team that knows why can
    tell when its conditions no longer serve it, and one that does not
    cannot.

    The first mission on Mini-2 was written down as "lead capture live":
    a form, a store and a notification. The person who issued it meant a
    qualified lead he could try to close. The team met seven of eight
    conditions in a week without anyone working on bringing a lead in, and
    nothing let him correct what had been written (19 November 2026).

    What it said before is kept. The conditions are not touched: they were
    written for the earlier wording, the team is told so, and the register's
    owner rewords what the team proves."""
    path = Path(agents_dir) / MISSIONS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "there is no mission register to change"
    items = raw.get("missions") if isinstance(raw, dict) else raw
    said = " ".join(str(intent or "").split())
    reason = " ".join(str(why or "").split())
    if not said and not reason:
        return False, "say what changes: --intent, --why, or both"
    if (said and len(said) < 20) or (reason and len(reason) < 20):
        return False, "say it in a full sentence"
    active = [
        m
        for m in (items if isinstance(items, list) else [])
        if isinstance(m, dict)
        and m.get("status", "active") == "active"
        and (not mission_id or str(m.get("mission_id")) == mission_id)
    ]
    if len(active) != 1:
        return False, (
            "no active mission has that id"
            if not active
            else "more than one mission is active: name one with --mission"
        )
    m = active[0]
    m.setdefault("restated", []).append(
        {
            "by": by or _issuer(m),
            "at": (now or datetime.now(UTC)).isoformat(),
            "was": str(m.get("intent") or "") if said else "",
            "why_was": str(m.get("why") or "") if reason else "",
            "why_added": bool(reason and not m.get("why")),
        }
    )
    if said:
        m["intent"] = said[:600]
    if reason:
        m["why"] = reason[:600]
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return False, "the register could not be written"
    return True, str(m.get("mission_id"))


def _restated_fact(m: dict[str, Any]) -> str:
    """What changed about what the mission is for, and whether any condition
    has been reworded since — '' when it reads as it was issued."""
    rows = [r for r in _as_list(m.get("restated")) if isinstance(r, dict)]
    if not rows:
        return ""
    last = rows[-1]
    at = str(last.get("at") or "")
    since = [
        a
        for c in m.get("end_state") or []
        for a in _as_list(_dict(c).get("amended"))
        if isinstance(a, dict) and str(a.get("at") or "") > at
    ]
    only_why = not last.get("was") and (last.get("why_was") or last.get("why_added"))
    what = "why this mission is being done" if only_why else "what this mission is for"
    text = f"{last.get('by') or _issuer(m)} restated {what} on {_clock(at)}."
    if last.get("was"):
        text += f' Before that it read: "{last["was"]}".'
    if last.get("why_was"):
        text += f' The reason given before was: "{last["why_was"]}".'
    elif last.get("why_added"):
        text += " No reason had been written down before."
    text += (
        f" {len(since)} condition(s) have been reworded since."
        if since
        else " Every condition below was written for the earlier wording; "
        "none has been reworded since."
    )
    return text


def _why_line(m: dict[str, Any]) -> str:
    why = str(m.get("why") or "").strip()
    if why:
        return f"Why it is being done, in {_issuer(m)}'s words: {why}"
    return f"Why it is being done: {_issuer(m)} has not written a reason down."


def _standing_facts(cond: dict[str, Any], seen: dict[str, Any], now: datetime) -> list[str]:
    """What is known about a condition that is not met: how long it has read
    that way, who reworded it and what it said as issued, and who chose how
    it is read. Facts only; what to do about them is the reader's."""
    out: list[str] = []
    stood = _hours_between(str(seen.get("since") or ""), now)
    if stood is not None and stood >= 1:
        out.append(f"  It has read not met for {_age(stood)}.")
    changes = [c for c in _as_list(cond.get("amended")) if isinstance(c, dict)]
    worded = [c for c in changes if str(c.get("was") or "") != str(cond.get("describe") or "")]
    if worded:
        first, last = worded[0], worded[-1]
        out.append(
            f"  {last.get('by', 'Its owner')} reworded it {_clock(str(last.get('at') or ''))}. "
            f'As issued it read: "{first.get("was", "")}"'
        )
    if str(_dict(cond.get("check")).get("type") or "") == "team":
        out.append(
            "  How the system reads it was chosen by whoever showed it, and any member "
            "can show it another way; the sentence is the condition, the reading is not."
        )
    return out


def _amended_note(cond: dict[str, Any]) -> str:
    """Who last changed a condition and what it said before, for a reader
    who knew it as issued."""
    changes = [c for c in _as_list(cond.get("amended")) if isinstance(c, dict)]
    if not changes:
        return ""
    last = changes[-1]
    return (
        f" (changed by {last.get('by', 'its owner')}, {_clock(str(last.get('at') or ''))}; "
        f'it read: "{last.get("was", "")}")'
    )


def owner_guide(missions: list[dict[str, Any]], state: dict[str, Any]) -> str:
    """For the one who owns the register: the conditions that wait on a
    confirmation, and how to make them the team's to prove."""
    lines: list[str] = []
    for m in missions:
        conds = _dict(_dict(state.get(str(m["mission_id"]))).get("conditions"))
        for cond in m["end_state"]:
            if str(_dict(cond.get("check")).get("type") or "") != "owner":
                continue
            if _dict(conds.get(str(cond["id"]))).get("met"):
                continue
            lines.append(f"- `{cond['id']}`: {cond['describe']}")
    if not lines:
        return ""
    return (
        "## 🎯 You own this mission's register\n"
        "These conditions wait on one person's confirmation, and while they do the "
        "system tells your team nothing here is theirs to do:\n" + "\n".join(lines) + "\n"
        "Ask of each: if a member of the team did this in that person's place, would the "
        "end be truly so, or would only its reading move? Only where it would be truly "
        "so, make it the team's to prove, in words that say what a member does:\n"
        '`cortiva mission amend --condition <id> --prove-by team --describe "<the '
        'condition, as the team will prove it>"`\n'
        "What it said before, and that you changed it, stays on the record and on every "
        "report to a human."
    )


def run_team_check(
    mission: dict[str, Any],
    cond: dict[str, Any],
    proofs: dict[str, Any],
    agents_dir: Path,
    fetch: Fetch | None = None,
) -> tuple[bool, str]:
    """A condition the team proves, as it stands once the proof is weighed.

    Shown and seen is the team's word. A proof weighed and found not so does
    not meet the condition, and says why, until a member shows it again."""
    met, observed = _team_reading(mission, cond, proofs, agents_dir, fetch)
    proof = _dict(_dict(proofs.get(str(mission["mission_id"]))).get(str(cond["id"])))
    weighed = _dict(proof.get("weighed"))
    if met and weighed and not weighed.get("so"):
        return False, (
            f"{observed}. Weighed, {_clock(str(weighed.get('at') or ''))}, and not so: "
            f"{str(weighed.get('why') or '')[:300]} Show it again when it is truly so"
        )
    return met, observed


def _team_reading(
    mission: dict[str, Any],
    cond: dict[str, Any],
    proofs: dict[str, Any],
    agents_dir: Path,
    fetch: Fetch | None = None,
) -> tuple[bool, str]:
    """A condition the team proves. Met when the system reads it at the
    address a member showed, and a different member has done it themselves
    and said what they saw. The owner's word in the register still settles it."""
    when = str(_dict(cond.get("check")).get("confirmed_at") or "").strip()
    if when:
        return True, f"confirmed by the mission's owner, {_clock(when)}"
    proof = _dict(_dict(proofs.get(str(mission["mission_id"]))).get(str(cond["id"])))
    shown = _dict(proof.get("shown"))
    if not shown:
        return False, (
            "the team's to prove, and nobody has yet: one of you does it, and shows the "
            "system an address where it can read it for itself"
        )
    by = str(shown.get("by") or "a member")
    if shown.get("sent_to"):
        sent, read = _sent_since(agents_dir, mission, shown)
        if not sent:
            return False, f"{by} showed a message to be sent; {read}"
        others = [_dict(s) for s in proof.get("seen") or [] if str(_dict(s).get("by")) != by]
        if not others:
            return False, (
                f"{read}, shown by {by}. It waits for a second member, not {by}, to say "
                "what they saw of the thing the message is about"
            )
        names = ", ".join(str(s.get("by")) for s in others)
        return True, f"shown by {by}, seen by {names}; {read}"
    url = str(shown.get("url") or "")
    secret, problem = _secret(agents_dir, by, str(shown.get("token_file") or ""))
    if problem:
        return False, f"{by} showed {url}, but {problem}"
    before = _dict(shown.get("read_when_shown"))
    if before.get("met"):
        return False, (
            f"{by} showed {url}, but it already read that way when it was shown "
            f"({before.get('observed')}), so it shows nothing about anyone doing this. "
            "Show the address before the thing is done, or ask for more than it reads "
            "now (--min above the present value), then do it"
        )
    met, observed = run_check(_shown_check(shown), fetch, secret)
    if not met:
        return False, f"{by} showed {url}; the system reads: {observed}"
    if secret:
        # A way in that also answers without the credential is not a way in.
        try:
            status, _ = (fetch or _default_fetch)(url, CHECK_TIMEOUT_S)
        except Exception:
            status = 0
        if fetch is None and status == 200:
            return False, (
                f"{by} showed {url} with a credential, but it answers the same without "
                "one, so it shows nothing about signing in"
            )
    others = [_dict(s) for s in proof.get("seen") or [] if str(_dict(s).get("by")) != by]
    if not others:
        return False, (
            f"the system reads it at {url} ({observed}), shown by {by}. It waits for a "
            f"second member, not {by}, to do it themselves and say what they saw"
        )
    names = ", ".join(str(s.get("by")) for s in others)
    return True, f"shown by {by}, seen by {names}; the system reads {observed}"


def team_guide(missions: list[dict[str, Any]], state: dict[str, Any]) -> str:
    """How a work session proves a condition that is the team's to prove.
    Empty when no such condition is open."""
    lines: list[str] = []
    for m in missions:
        conds = _dict(_dict(state.get(str(m["mission_id"]))).get("conditions"))
        for cond in m["end_state"]:
            if str(_dict(cond.get("check")).get("type") or "") != "team":
                continue
            seen = _dict(conds.get(str(cond["id"])))
            if seen.get("met"):
                continue
            lines.append(
                f"- `{cond['id']}`: {cond['describe']} ({seen.get('observed') or 'not yet checked'})"
            )
    if not lines:
        return ""
    return (
        "## 🎯 Conditions the team proves for itself\n"
        "Nobody outside the team is waited on for these. Do the thing the way the person "
        "it is for would: sign in, look, judge. If there is no way in for you, make one "
        "(an account, a credential, a route for agents) by the path your work normally "
        "travels.\n" + "\n".join(lines) + "\n"
        "- `cortiva mission prove --condition <id> --url <address on the real thing> "
        "--path <field in the JSON> [--min <n>] [--contains <text>] [--token-file <file "
        "under your directory holding the credential>]` shows the system where to read "
        "it. The system reads it as you show it and from then on: show it before the "
        "thing is done, or ask for more than it reads now. An open address that already "
        "reads that way proves nothing.\n"
        '- `cortiva mission prove --condition <id> --sent-to <address> [--contains "<text>"]` '
        "is the same for a condition that ends in somebody being told: the system reads "
        "its own record of what it sent, and counts a message any of you sends to that "
        "address after you show it. Whichever of you sends it, by whatever means, it is "
        "the telling that is read.\n"
        '- `cortiva mission witness --condition <id> --saw "<what you did and what you '
        'saw>"` is for a second member, never the one who showed it. Do it yourself '
        "first; your name stands on what you say you saw.\n"
        "- `cortiva mission list` shows every condition as the system last read it."
    )


def unweighed(agents_dir: Path, fetch: Fetch | None = None) -> list[dict[str, Any]]:
    """Proofs the team has finished that nobody has weighed.

    A member told the founder by hand of "a qualified lead", two others said
    what they saw, and the mission read complete; the lead was one the team
    had entered itself, and forty minutes later another member's own entry
    went to the founder as "a genuine enquiry" (Mini-2, 2026-11-20). Each
    step was honest work on the reading. Nothing asked whether the end was
    so. One row per finished proof not yet weighed."""
    proofs = load_proofs(agents_dir)
    rows: list[dict[str, Any]] = []
    for m in load(agents_dir):
        mid = str(m["mission_id"])
        for cond in m["end_state"]:
            if str(_dict(cond.get("check")).get("type") or "") != "team":
                continue
            proof = _dict(_dict(proofs.get(mid)).get(str(cond["id"])))
            if not proof or proof.get("weighed"):
                continue
            met, observed = _team_reading(m, cond, proofs, Path(agents_dir), fetch)
            if met:
                rows.append(
                    {
                        "mission": m,
                        "condition": cond,
                        "proof": proof,
                        "observed": observed,
                    }
                )
    return rows


def weigh_prompt(row: dict[str, Any]) -> str:
    """The question put to a member who took no part in a proof."""
    m, cond, proof = row["mission"], row["condition"], row["proof"]
    shown = _dict(proof.get("shown"))
    lines = [
        f"The mission: {m.get('title')}: {str(m.get('intent') or '').strip()} {_why_line(m)}",
        f'The condition, as it reads now: "{cond.get("describe")}"',
    ]
    first = next((c for c in _as_list(cond.get("amended")) if isinstance(c, dict)), None)
    if first:
        lines.append(
            f'As it was first issued it read: "{first.get("was")}" '
            f"(changed by {first.get('by')}, a member of the team)"
        )
    lines.append(f"What the team showed, and what the system reads: {row.get('observed')}")
    said = [
        f"- {_dict(s).get('by')}: {str(_dict(s).get('saw') or '')[:700]}"
        for s in _as_list(proof.get("seen"))
    ]
    lines.append(
        f"Shown by {shown.get('by')}. What other members said they saw:\n" + "\n".join(said)
    )
    lines.append(
        "Weigh it as someone outside the team would, who takes nothing on trust. A reading "
        "is how the system looks at the end; it is not the end. Say what has to have "
        "happened in the real world for this to be truly so, and who has to have done it. "
        "Where that is someone outside the team (a customer, a visitor, the person who "
        "issued the mission), something a member did in their place, or an entry the team "
        "made itself, does not make it so, however real it looks. What the proof does not "
        "say, it does not show: if it does not say who did the thing, it is not shown that "
        "they did.\n"
        "Answer on these lines:\n"
        "NEEDS: <what has to have happened>\n"
        "BY: <who has to have done it>\n"
        "DID: <who, by this proof, did it; NOT SHOWN if the proof does not say>\n"
        "SO: YES or NO\n"
        "WHY: <one sentence>"
    )
    return "\n\n".join(lines)


def parse_weigh(answer: str) -> tuple[bool | None, str]:
    """``(so, why)`` from a weighing; ``so`` is None when it gave no verdict."""
    so: bool | None = None
    why = ""
    for line in str(answer or "").splitlines():
        head, _, rest = line.strip().lstrip("*-# ").partition(":")
        key = head.strip().strip("*").upper()
        rest = rest.strip().strip("*").strip()
        if key == "SO" and rest:
            word = rest.split()[0].strip(".,").upper()
            so = True if word == "YES" else False if word == "NO" else so
        elif key == "WHY":
            why = rest
    return so, why


def record_weigh(
    agents_dir: Path,
    mission_id: str,
    condition: str,
    so: bool,
    why: str,
    by: str,
    *,
    now: datetime | None = None,
) -> None:
    """Keep what a weighing found with the proof it weighed."""
    proofs = load_proofs(agents_dir)
    proof = _dict(_dict(proofs.get(mission_id)).get(condition))
    if not proof:
        return
    proof["weighed"] = {
        "so": bool(so),
        "why": " ".join(str(why or "").split())[:400],
        "by": by,
        "at": (now or datetime.now(UTC)).isoformat(),
    }
    proofs[mission_id][condition] = proof
    _save_proofs(agents_dir, proofs)


HAVE_FRESH_DAYS = 7.0


def have_path(agents_dir: Path, member: str, mission_id: str) -> Path:
    """Where a member writes what the business already has for a mission."""
    return Path(agents_dir) / member / "have" / f"{mission_id}.md"


def have_note(
    agents_dir: Path, mission: dict[str, Any], *, now: datetime | None = None
) -> tuple[str, str, str]:
    """``(text, who wrote it, when)``: the newest page a member of the team
    wrote on what the business already has; empty when none is fresh."""
    best: tuple[float, str, Path] | None = None
    for member in (str(t).lower() for t in _as_list(mission.get("team"))):
        path = have_path(agents_dir, member, str(mission["mission_id"]))
        try:
            at = path.stat().st_mtime
        except OSError:
            continue
        if best is None or at > best[0]:
            best = (at, member, path)
    if best is None:
        return "", "", ""
    age = ((now or datetime.now(UTC)).timestamp() - best[0]) / 86400
    if age > HAVE_FRESH_DAYS:
        return "", "", ""
    try:
        text = best[2].read_text(encoding="utf-8").strip()
    except OSError:
        return "", "", ""
    if len(text) < 200:
        return "", "", ""
    when = datetime.fromtimestamp(best[0], UTC).isoformat()
    # The whole page: whoever puts it before a member chooses what of it to show.
    return text[:20000], best[1], _clock(when)


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
    proofs = load_proofs(agents_dir)
    state: dict[str, Any] = {}
    for m in load(agents_dir):
        mid = str(m["mission_id"])
        prev = _dict(old.get(mid))
        prev_conds = _dict(prev.get("conditions"))
        conds: dict[str, Any] = {}
        for cond in m["end_state"]:
            cid = str(cond["id"])
            if str(_dict(cond["check"]).get("type") or "") == "team":
                met, observed = run_team_check(m, cond, proofs, Path(agents_dir), fetch)
            else:
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
        asked = _dict(prev.get("owner_asked"))
        if asked and all(not _dict(conds.get(c)).get("met") for c in asked.get("conditions", [])):
            entry["owner_asked"] = asked
        still = {
            c: at
            for c, at in _dict(prev.get("still_asked")).items()
            if c in conds and not conds[c]["met"]
        }
        if still:
            entry["still_asked"] = still
        complete = entry["met"] == entry["total"]
        entry["complete"] = complete
        entry["completed_at"] = (str(prev.get("completed_at") or "") or now_iso) if complete else ""
        state[mid] = entry
    save_state(agents_dir, state)
    return state


def standing_still(agents_dir: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Conditions of a mission where nothing has moved for STILL_AFTER_HOURS.

    A team that is busy does not say it is blocked. One condition read not
    met for eight days and nothing moved for twenty-two hours while five
    members finished fifteen pieces of work, and no one was asked anything,
    because the asking waited for someone to say "blocked" (Mini-2,
    2026-11-20). The system holds the fact, so the system asks.

    One row per condition the team can move, with who is asked: every member
    of the team not asked about it in that long, the one who showed it first.
    Each is asked separately; that they answer differently is what a team is
    for."""
    at = now or datetime.now(UTC)
    state = load_state(agents_dir)
    proofs = load_proofs(agents_dir)
    rows: list[dict[str, Any]] = []
    for m in load(agents_dir):
        mid = str(m["mission_id"])
        entry = _dict(state.get(mid))
        if not entry or entry.get("complete"):
            continue
        moved = str(entry.get("last_change_at") or entry.get("first_checked_at") or "")
        still = _hours_between(moved, at)
        if still is None or still < STILL_AFTER_HOURS:
            continue
        team = [str(t).lower() for t in _as_list(m.get("team")) if str(t).strip()]
        asked = _dict(entry.get("still_asked"))
        for cond in m["end_state"]:
            cid = str(cond["id"])
            seen = _dict(_dict(entry.get("conditions")).get(cid))
            if not seen or seen.get("met"):
                continue
            if str(_dict(cond["check"]).get("type") or "") == "owner":
                continue  # that one waits on its owner, who is asked elsewhere
            shown = _dict(_dict(_dict(proofs.get(mid)).get(cid)).get("shown"))
            by = str(shown.get("by") or "").lower()
            everyone = list(dict.fromkeys(w for w in (by, *team) if w))
            before = asked.get(cid)
            if isinstance(before, str):
                # Written when only two were asked: the one who showed it and
                # the first named.
                before = {w: before for w in (by, *team[:1]) if w}
            who = []
            for w in everyone:
                since_ask = _hours_between(str(_dict(before).get(w) or ""), at)
                if since_ask is None or since_ask >= STILL_AFTER_HOURS:
                    who.append(w)
            if not who:
                continue
            rows.append(
                {
                    "mission_id": mid,
                    "title": str(m.get("title") or mid),
                    "condition": cid,
                    "describe": str(cond.get("describe") or cid),
                    "observed": str(seen.get("observed") or ""),
                    "not_met_for": _age(_hours_between(str(seen.get("since") or ""), at)),
                    "still_for": _age(still),
                    "still_hours": still,
                    "who": who,
                }
            )
    return rows


def record_still_ask(
    agents_dir: Path,
    mission_id: str,
    condition: str,
    who: list[str],
    *,
    now: datetime | None = None,
) -> None:
    """Note which members were asked about a condition standing still."""
    state = load_state(agents_dir)
    entry = _dict(state.get(mission_id))
    if not entry:
        return
    asked = _dict(entry.get("still_asked"))
    at = (now or datetime.now(UTC)).isoformat()
    before = asked.get(condition)
    asked[condition] = {**(before if isinstance(before, dict) else {}), **dict.fromkeys(who, at)}
    entry["still_asked"] = asked
    state[mission_id] = entry
    save_state(agents_dir, state)


# ---------------------------------------------------------------------------
# What the team sees, and what a human is told
# ---------------------------------------------------------------------------


def _owner_unmet(m: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    """Ids of the conditions only the owner can confirm and has not."""
    conds = _dict(entry.get("conditions"))
    return [
        str(c["id"])
        for c in m["end_state"]
        if str(_dict(c.get("check")).get("type") or "") == "owner"
        and not _dict(conds.get(str(c["id"]))).get("met")
    ]


def _only_owner_left(m: dict[str, Any], entry: dict[str, Any]) -> bool:
    """The route is proven and every condition the system can check is met."""
    if not entry or entry.get("complete"):
        return False
    if route_of(m) and not _dict(entry.get("route")).get("first_met_at"):
        return False
    owner = _owner_unmet(m, entry)
    unmet = int(entry.get("total", 0)) - int(entry.get("met", 0))
    return bool(owner) and unmet == len(owner)


def waits_on_owner(m: dict[str, Any], entry: dict[str, Any]) -> bool:
    """True when the system sees nothing left for the team and the owner
    has been asked."""
    return _only_owner_left(m, entry) and bool(entry.get("owner_asked"))


def record_owner_ask(
    agents_dir: Path,
    missions: list[dict[str, Any]],
    by: str,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Note that the owner has been asked, for the whole team.

    Each member was told "ask once" and each did: ten messages from four
    members reached the owner overnight for the same two confirmations (first
    live run, Mini-2 2026-11-13). "Once" is a fact about the team, so the
    system keeps it. The first message a member sends a human once only owner
    conditions are left is the ask; it stands until one of them is confirmed.
    A message sent while the team still has work is not the ask.
    Returns the missions newly marked."""
    state = load_state(agents_dir)
    marked: list[str] = []
    for m in missions:
        mid = str(m["mission_id"])
        entry = _dict(state.get(mid))
        if not _only_owner_left(m, entry) or entry.get("owner_asked"):
            continue
        owner = _owner_unmet(m, entry)
        entry["owner_asked"] = {
            "at": (now or datetime.now(UTC)).isoformat(),
            "by": by,
            "conditions": owner,
        }
        state[mid] = entry
        marked.append(mid)
    if marked:
        save_state(agents_dir, state)
    return marked


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
        "A step you cannot do yourself goes to the teammate who can: the mission is the "
        "team's, and so is the route. Only a step that NOBODY on the team can see or do is "
        f"the finding. Then, and only then, send ONE message to {_issuer(m)} naming the "
        "step, the place and what the team needs. Do not build further behind a gate "
        "you have not passed.",
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
        lines.append(_why_line(m) + "\n")
        restated = _restated_fact(m)
        if restated:
            lines.append(restated + "\n")
        lines.extend(_route_lines(m, entry, now))
        for cond in m["end_state"]:
            seen = conds.get(str(cond["id"])) or {}
            if not seen:
                lines.append(f"- ⏳ {cond['describe']} — not yet checked")
            elif seen.get("met"):
                lines.append(f"- ✅ {cond['describe']} — met since {_clock(seen.get('since', ''))}")
            else:
                lines.append(f"- ❌ {cond['describe']} — NOT MET ({seen.get('observed', '')})")
                lines.extend(_standing_facts(cond, seen, now))
                if str((cond.get("check") or {}).get("type") or "") == "owner":
                    asked = _dict(entry.get("owner_asked"))
                    if str(cond["id"]) in asked.get("conditions", []):
                        lines.append(f"  {_asked_fact(m, asked, now)}.")
                    else:
                        lines.append(f"  {_not_asked_fact(m)}.")
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


def brief(
    missions: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    """The mission in a few lines, for the small asks: the "what next" replan
    an agent makes many times a day carries a short context by design, and it
    is the ask that decides what the agent does. A mission that is only in
    the long contexts is one the agent reads and then does not act on (first
    live run, Mini-2 2026-11-12: 17 of 27 model calls were this ask, and none
    carried the mission). Empty when every mission is at its end state."""
    now = now or datetime.now(UTC)
    out: list[str] = []
    for m in missions:
        entry = state.get(str(m["mission_id"])) or {}
        if entry.get("complete"):
            continue
        conds = entry.get("conditions") or {}
        unmet = [c for c in m["end_state"] if not (conds.get(str(c["id"])) or {}).get("met")]
        asked = _dict(entry.get("owner_asked"))
        if waits_on_owner(m, entry):
            waiting = ", ".join(str(c["describe"]) for c in unmet)
            out.append(
                f"**{m['title']}** — every condition the system can check is met. What is "
                f"left ({waiting}) is written as {_issuer(m)}'s to confirm. "
                f"{_asked_fact(m, asked, now)}."
            )
            continue
        out.append(
            f"**{m['title']}** — {entry.get('met', 0)} of {len(m['end_state'])} end-state "
            f"conditions met, as observed by the system (issued by {_issuer(m)})."
        )
        intent = str(m.get("intent") or "").strip()
        if intent:
            out.append(f"What it is for, in {_issuer(m)}'s words: {intent}")
        out.append(_why_line(m))
        restated = _restated_fact(m)
        if restated:
            out.append(restated)
        team = [str(t) for t in _as_list(m.get("team")) if str(t).strip()]
        if team:
            out.append(f"The team on it: {', '.join(team)}.")
        route = route_of(m)
        if route and not (entry.get("route") or {}).get("first_met_at"):
            waited = _hours_between(
                str(m.get("issued_at") or entry.get("first_checked_at") or ""), now
            )
            out.append(
                f"- FIRST: the route is not proven ({_age(waited)}). Get the text "
                f"`{route['token']}` to appear at {route['url']} by the path your real work "
                f"travels. A step you cannot do goes to the teammate who can. Only a step "
                f"nobody on the team can see or do goes to {_issuer(m)}, as one message "
                f"naming it."
            )
        for cond in unmet:
            seen = conds.get(str(cond["id"])) or {}
            owner = str((cond.get("check") or {}).get("type") or "") == "owner"
            if owner and str(cond["id"]) in asked.get("conditions", []):
                tail = _asked_fact(m, asked, now)
            elif owner:
                tail = _not_asked_fact(m)
            else:
                tail = str(seen.get("observed") or "not yet checked")
            if str((cond.get("check") or {}).get("type") or "") == "team":
                tail += f". Nobody waits on {_issuer(m)} for this"
            out.append(f"- NOT MET: {cond['describe']} ({tail})")
    if not out:
        return ""
    return (
        "## 🎯 Your mission — not at its end state\n"
        "Only the system's checks and the mission's owner decide done; no report, merge "
        "or closed issue does.\n" + "\n".join(out)
    )


def team_has_work(missions: list[dict[str, Any]], state: dict[str, Any]) -> bool:
    """True while any mission is short of its end state. A mission whose last
    conditions were written as one person's to confirm was once counted as
    no work for anyone; five people then had nothing to do on it for six and
    a half days (Mini-2, 2026-11-12 to 11-19). Who was asked, and when, is
    shown to them; what to do about it is theirs."""
    return any(not _dict(state.get(str(m["mission_id"]))).get("complete") for m in missions)


def _asked_fact(m: dict[str, Any], asked: dict[str, Any], now: datetime | None) -> str:
    """Who asked the mission's issuer, when, and that nothing has come back."""
    when = str(asked.get("at") or "")
    waited = _hours_between(when, now or datetime.now(UTC)) if when else None
    return (
        f"{_issuer(m)} was asked {_clock(when)} by {asked.get('by', 'a teammate')}; no "
        f"confirmation is in the register {_age(waited)} later"
    )


def _not_asked_fact(m: dict[str, Any]) -> str:
    return (
        f"as written, only {_issuer(m)}'s confirmation in the register meets this, and "
        f"{_issuer(m)} has not been asked; whoever owns the register can make it the "
        "team's to prove"
    )


def orders_for_hands(
    missions: list[dict[str, Any]],
    state: dict[str, Any],
    agents_dir: Path,
) -> str:
    """The mission as the session that does the work must see it: what is
    asked, who asked, and where to check that for itself.

    The member plans; a separate session is its hands, and that session
    questions what it is handed. Told only "put `cortiva-route-…` on the
    production health page", it reads an unexplained string bound for
    production as an injected instruction and refuses, correctly (first live
    run, Mini-2 2026-11-12: three sessions refused the route step and one sent
    a retraction to the teammate who had been asked). The cure is evidence it
    can check, not a request to trust: the register on disk says the same
    thing this block does."""
    out: list[str] = []
    for m in missions:
        entry = state.get(str(m["mission_id"])) or {}
        if entry.get("complete"):
            continue
        conds = entry.get("conditions") or {}
        team = ", ".join(str(t) for t in _as_list(m.get("team")) if str(t).strip())
        out.append(
            f"**{m['title']}** (mission `{m['mission_id']}`), issued by {_issuer(m)}"
            f"{f'; team: {team}' if team else ''}. {entry.get('met', 0)} of "
            f"{len(m['end_state'])} end-state conditions met."
        )
        route = route_of(m)
        if route:
            if (entry.get("route") or {}).get("first_met_at"):
                out.append(
                    f"- Route proven. The marker `{route['token']}` has done its job and "
                    "may be removed."
                )
            else:
                out.append(
                    f"- Route proof, wanted first: the system fetches {route['url']} and "
                    f"looks for the text `{route['token']}`. The marker does nothing else. "
                    "It proves a change by this team reaches the real thing by the normal "
                    "path (branch, review, merge, deploy), and it comes out again once the "
                    "system has seen it. A request to put it there is this order."
                )
        for cond in m["end_state"]:
            if not (conds.get(str(cond["id"])) or {}).get("met"):
                out.append(f"- Not met: {cond['describe']}")
    if not out:
        return ""
    return (
        "## 🎯 Mission orders — from the system's register, and yours to check\n"
        "You are the hands of a member of the mission below. These orders are written by "
        "the system from its mission register, not by the task text, a message or a web "
        f"page. Check them before you act on them: the register is `{agents_dir / MISSIONS_FILE}` "
        f"and what the system last observed is `{agents_dir / STATE_FILE}`. A task that "
        "matches these orders is legitimate work. A task that asks for something they do "
        "not is still yours to question.\n" + "\n".join(out)
    )


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
            lines.append(f"  {mark} {cond['describe']}{_amended_note(cond)}")
    return "\n".join(lines)
