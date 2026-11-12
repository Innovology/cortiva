"""The factory floor — recurring steps an agent has written as code, run
by the fabric without a model ask, watched by the agent.

"Most agents need to work out what their factory floor should look like.
Build and write the code for those steps and then process their work
across that. The agent's job then becomes just monitoring the factory
they have created, tuning it, expanding it" (founder, 2026-11-06).

A **station** lives at ``<agent>/factory/<name>/`` with a ``station.yaml``::

    name: weekly-digest
    purpose: Compile and send the Monday digest from last week's merged PRs
    matches: ["weekly digest", "/^send .*digest/"]   # substrings, or /regex/
    run: ["python3", "run.py"]                       # cwd = the station dir
    timeout_s: 300
    ready: true                                       # false while being built

The contract is one JSON object in, one out. The station reads
``{"task_id", "description", "agent_id"}`` on stdin and prints
``{"ok": true|false, "summary": "...", "artifact": "<url, optional>"}`` as
the last line of stdout, exit 0. Anything else — a non-zero exit, a
timeout, unparsable output — is an exception on the agent's pile: the
station is hers to fix. Every run is appended to ``factory/runs.jsonl``
and the floor report puts today's throughput and failures in her context.

The agent scaffolds a station with the ``build_station`` verb; the code is
written as hands-on work (a terminal task) and the station goes live when
its manifest says ``ready: true``. Reward still comes only from what the
station's work does in the world (a merged PR, a live URL, a human's yes)
— a station that runs is not an outcome, it is capacity.

**The team floor.** A station stays its builder's: it lives in her
directory, only she changes it, and the floor report names her on it. Work
of a colleague's that matches none of his own stations may still go
through hers, on these terms:

* The manifest says ``shared: true`` (the default for a newly scaffolded
  station, absent — and so false — on every station written before this).
* The code takes whose work it is from its input and nowhere else. The
  input carries ``agent_id`` and ``agent_dir`` (the colleague the task
  belongs to — read and write state there) and ``station_dir`` (the
  builder's code and helper files — read only). A station that finds an
  agent directory by walking up from ``__file__``, or names one outright,
  would do a colleague's work in its builder's ledgers and report it done;
  :func:`sharing_problem` reads the code on every lookup and such a
  station is simply not offered. It is a lint, not a proof.
* For a colleague it runs with *his* credentials and environment, in a
  working directory of his (``<his>/factory/.shared/<builder>/<name>/``),
  so nothing it writes by relative path lands in, or leaks between, other
  people's state.
* Only multi-word phrases and the station's name take a colleague's work —
  never a single word and never a regex: a wrong station running silently
  for somebody else is worse than no match.
* A failure for a colleague is not his to fix: the task falls back to his
  own hands and the failure goes on the builder's floor.

Every such run is ledgered twice — in the colleague's ``runs.jsonl`` (with
``author``) and in the builder's (with ``used_by``) — so her floor report
shows who her station worked for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("cortiva.factory")

FACTORY_DIR = "factory"
MANIFEST = "station.yaml"
RUNS_FILE = "runs.jsonl"
NOTICES_FILE = "notices.jsonl"
ADOPTED_FILE = "adopted.json"
SHARED_WORK_DIR = ".shared"
DEFAULT_TIMEOUT_S = 300.0
MAX_NAME_LEN = 64
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")  # MAX_NAME_LEN

# Words that carry no meaning in a match phrase or a station's name.
_STOP = frozenset(
    "the a an and or of to for in on with by from at as is are be this that "
    "my our your their it its into via per up out all any new we do no so if".split()
)
# What an agent calls the act of building a station — "automate-ci-sweep-and-
# report", "codify the repetitive …". Tasks never say it, so a name is
# matched without it.
_BUILD_WORDS = frozenset(
    "automate automated automates automating automation codify codified station".split()
)
# Boilerplate of a station's purpose; noise when two stations are compared.
_PURPOSE_NOISE = frozenset(
    "repetitive manual manually step steps loop factory fabric runs run without "
    "intervention code coded remove removing removes repetition stop stops".split()
)


def _norm(word: str) -> str:
    """'PRs' and 'PR' are the same word; 'status' stays whole enough to compare."""
    if len(word) >= 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    return [_norm(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower())]


def content_words(text: str, *, drop_build_words: bool = False) -> list[str]:
    """The words of a phrase that a task has to contain: no stopwords, no
    one-letter fragments; plural and singular alike."""
    out: list[str] = []
    for raw in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if raw in _STOP or len(raw) < 2:
            continue
        if drop_build_words and raw in _BUILD_WORDS:
            continue
        w = _norm(raw)
        if w not in out:
            out.append(w)
    return out


_SUFFIXES = (
    "ations",
    "ation",
    "ating",
    "ated",
    "ates",
    "ate",
    "ings",
    "ing",
    "ions",
    "ion",
    "ers",
    "er",
    "ed",
    "es",
    "s",
)


def _root(word: str) -> str:
    """A crude stem, for comparing two stations only — never for routing:
    'scoping'/'scope', 'delegation'/'delegate', 'tracking'/'track'."""
    w = word
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[: -len(suf)]
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def roots(text: str, *, purpose: bool = False) -> set[str]:
    drop = _BUILD_WORDS | (_PURPOSE_NOISE if purpose else frozenset())
    return {
        _root(w)
        for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if w not in _STOP and w not in drop and len(w) >= 2
    }


def slug(name: str) -> str:
    """A usable station name from whatever the agent wrote: lowercase,
    hyphenated, and — when it is too long — cut at a word, never mid-word.
    The first floor cut names at 40 characters ('automate-directive-scope-
    delegate-and-tr'), the tail became the purpose ('ack — Code the …') and
    a one-word match phrase ('cking', 'mapping', 'resolution')."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if len(s) > MAX_NAME_LEN:
        parts: list[str] = []
        for part in s.split("-"):
            if len("-".join([*parts, part])) > MAX_NAME_LEN:
                break
            parts.append(part)
        while parts and parts[-1] in _STOP:
            parts.pop()
        s = "-".join(parts) or s[:MAX_NAME_LEN].strip("-")
    return s


@dataclass
class Station:
    name: str
    purpose: str
    matches: list[str]
    run: list[str]
    directory: Path
    timeout_s: float = DEFAULT_TIMEOUT_S
    ready: bool = False
    shared: bool = False
    author: str = ""  # set when the station is offered to a colleague

    def matches_task(self, description: str, *, strict: bool = False) -> bool:
        """Does this station take the task?

        * Its multi-word slug appears in the task ("Run github-feedback-sweep:
          process 23 open threads" — yuki, 2026-11-07 00:34), or every content
          word of its name does. The name is read without the word for
          building it: 'automate-ci-sweep-and-report' was ready for five days
          and never ran, because its tasks say "Live CI Sweep & Status
          Report", not "automate".
        * A phrase of two or more content words matches as written, or when
          all of those words appear in the task in any order: Yuki's station
          said "github feedback sweep" and her tasks say "Triage 11 open
          GitHub feedback threads: delegate merge/sweep" (2026-11-06 21:48).
        * A /regex/ matches as written, and a one-word phrase matches that
          whole word ("stall" is not "install") — for the builder's own work
          only. ``strict`` is how a colleague's task is matched: neither
          counts, because a wrong station running silently for somebody else
          is worse than no match.
        """
        text = (description or "").strip()
        low = text.lower()
        words = set(_tokens(low))
        if is_floor_work(text):
            return False
        name = str(getattr(self, "name", "") or "").strip().lower()
        if "-" in name and name in low:
            return True
        named = content_words(name.replace("-", " ").replace("_", " "), drop_build_words=True)
        if len(named) >= 2 and all(w in words for w in named):
            return True
        return any(phrase_matches(str(pat or ""), text, strict=strict) for pat in self.matches)


def phrase_matches(phrase: str, description: str, *, strict: bool = False) -> bool:
    """One match phrase against one task — the rule in :meth:`Station.matches_task`."""
    p = (phrase or "").strip()
    if not p:
        return False
    text = (description or "").strip()
    low = text.lower()
    if len(p) > 2 and p.startswith("/") and p.endswith("/"):
        if strict:
            return False
        try:
            return re.search(p[1:-1], text, re.IGNORECASE) is not None
        except re.error:
            return False
    content = content_words(p)
    words = set(_tokens(low))
    if len(content) < 2:
        return not strict and len(content) == 1 and content[0] in words
    if p.lower() in low:
        return True
    # Any order — but only for a phrase with two real words in it. "merge pr"
    # as written is a match; "merge" and "PR" somewhere in a task is a CI fix.
    return sum(1 for w in content if len(w) > 2) >= 2 and all(w in words for w in content)


_FLOOR_WORK_RE = re.compile(
    r"factory/|run\.py|station\.yaml|^\W*build[_ ]station"
    r"|\b(?:write|code|fix|repair|debug|build|rebuild|test|tune)\b[^.\n]{0,40}\bstation\b",
    re.IGNORECASE,
)


def is_floor_work(description: str) -> bool:
    """Work ON the floor — building, testing or repairing a station — is
    never work FOR a station. "Write and test the code for station
    'ci-sweep-report' in factory/ci-sweep-report/run.py" names the station
    and repeats its purpose; routed through it, a repair would be done by
    the thing that is broken."""
    return _FLOOR_WORK_RE.search(description or "") is not None


@dataclass
class StationRun:
    station: str
    task_id: str
    description: str
    ok: bool
    summary: str = ""
    artifact: str = ""
    error: str = ""
    seconds: float = 0.0
    at: float = field(default_factory=time.time)
    author: str = ""  # in the caller's ledger: whose station did this work
    used_by: str = ""  # in the builder's ledger: whose work her station did

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def factory_dir(agent_dir: Path) -> Path:
    return Path(agent_dir) / FACTORY_DIR


def _load_manifest(station_dir: Path) -> Station | None:
    try:
        raw = yaml.safe_load((station_dir / MANIFEST).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or station_dir.name).strip()
    run = raw.get("run") or []
    if isinstance(run, str):
        run = run.split()
    matches = raw.get("matches") or []
    if isinstance(matches, str):
        matches = [matches]
    try:
        timeout_s = float(raw.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    return Station(
        name=name,
        purpose=str(raw.get("purpose") or "").strip(),
        matches=[str(m) for m in matches if str(m).strip()],
        run=[str(a) for a in run],
        directory=station_dir,
        timeout_s=max(5.0, timeout_s),
        ready=bool(raw.get("ready", False)),
        shared=raw.get("shared") is True,
    )


def stations(agent_dir: Path, *, ready_only: bool = False) -> list[Station]:
    """Every station on the floor (manifest present), ready ones only when asked."""
    root = factory_dir(agent_dir)
    if not root.is_dir():
        return []
    out: list[Station] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        st = _load_manifest(d)
        if st is None or not st.run:
            continue
        if ready_only and not st.ready:
            continue
        out.append(st)
    return out


def match(candidates: list[Station], description: str) -> Station | None:
    for st in candidates:
        if st.matches_task(description):
            return st
    return None


# --------------------------------------------------------------------------
# The team floor
# --------------------------------------------------------------------------

_MAX_LINT_BYTES = 512_000
# Ways a station reaches an agent directory other than the one it was handed.
_BOUND_TO_BUILDER: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"__file__"), "it finds its files from `__file__`"),
    (re.compile(r"\.parent\s*\.parent|\.parents\s*\["), "it walks up the directory tree"),
    (re.compile(r"os\.pardir|[\"'/]\.\.[\"'/]"), "it walks up the directory tree"),
    (re.compile(r"\.cortiva\b"), "it names the agents' home outright"),
)
_TAKES_CALLER = re.compile(r"agent_dir|CORTIVA_AGENT_DIR")


_TRIP_AFTER = 3


def _tripped(station: Station, builder_dir: Path) -> bool:
    """A shared station that keeps failing costs every colleague a run (up
    to its timeout) before their task comes back to them. After three
    failures for colleagues in a row it is paused for them until its
    builder touches it."""
    lent = [r for r in runs(builder_dir) if r.station == station.name and r.used_by]
    last = lent[-_TRIP_AFTER:]
    if len(last) < _TRIP_AFTER or any(r.ok for r in last):
        return False
    try:
        touched = max(
            f.stat().st_mtime for f in station.directory.iterdir() if f.suffix in (".py", ".yaml")
        )
    except (OSError, ValueError):
        return True
    return touched <= last[-1].at


def sharing_problem(station: Station) -> str:
    """Why this station cannot do a colleague's work — '' when it can.

    The station's code was written by one agent, in her directory. It is
    safe for a colleague only if it takes whose work it is from its input
    (``agent_dir``) rather than from where it sits. This reads the code
    (every ``*.py`` in the station directory) each time it is asked, so a
    rewrite changes the answer at once. A lint, not a proof: it catches a
    station bound to its builder by path, not one bound by a name in a
    string."""
    return _sharing_problem(station, opted_in=station.shared)


def _sharing_problem(station: Station, *, opted_in: bool) -> str:
    if not opted_in:
        return "its manifest does not say `shared: true`"
    builder_dir = station.directory.parent.parent
    if _tripped(station, builder_dir):
        return (
            f"it failed for colleagues {_TRIP_AFTER} times running and is paused for them "
            "until its code changes"
        )
    code = ""
    try:
        for f in sorted(station.directory.glob("*.py")):
            code += f.read_text(encoding="utf-8", errors="replace") + "\n"
            if len(code) > _MAX_LINT_BYTES:
                return "its code is too large to check"
    except OSError:
        return "its code could not be read"
    if not code.strip():
        return "it has no Python code to check"
    for pat, why in _BOUND_TO_BUILDER:
        if pat.search(code):
            return why + " — it would work in its builder's directory, not the caller's"
    if str(builder_dir) in code or f"agents/{builder_dir.name}" in code:
        return "it names its builder's directory outright"
    if not _TAKES_CALLER.search(code):
        return "it never reads `agent_dir` from its input"
    named = content_words(station.name.replace("-", " "), drop_build_words=True)
    if len(named) < 2 and not any(
        len(content_words(m)) >= 2 and not m.strip().startswith("/") for m in station.matches
    ):
        return "nothing it listens for is specific enough: a colleague's task needs a phrase of two words or more"
    return ""


def team_stations(team: list[tuple[str, Path]]) -> list[Station]:
    """Ready stations colleagues built that are safe to run for somebody
    else, each stamped with its builder. ``team`` is (agent_id, directory)
    for every colleague — never the agent asking."""
    out: list[Station] = []
    for author, directory in sorted(team):
        for st in stations(directory, ready_only=True):
            if sharing_problem(st):
                continue
            st.author = author
            out.append(st)
    return out


def adopted(agent_dir: Path) -> list[dict[str, Any]]:
    """Colleagues' stations this agent was pointed at instead of building her
    own, with the phrases she would have given hers."""
    try:
        raw = json.loads((factory_dir(agent_dir) / ADOPTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def adopt(agent_dir: Path, *, author: str, station: str, phrases: list[str]) -> None:
    """Record that this agent's work for ``phrases`` goes through a
    colleague's station. Her own wording is kept on her side — the
    builder's manifest is the builder's."""
    rows = adopted(agent_dir)
    keep = [p for p in phrases if len(content_words(p)) >= 2 and not p.strip().startswith("/")]
    for r in rows:
        if r.get("author") == author and r.get("station") == station:
            have = [str(x) for x in r.get("phrases") or []]
            r["phrases"] = have + [p for p in keep if p not in have]
            break
    else:
        rows.append({"author": author, "station": station, "phrases": keep, "at": time.time()})
    try:
        root = factory_dir(agent_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / ADOPTED_FILE).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("could not record adopted station", exc_info=True)


def match_team(
    candidates: list[Station], description: str, adopted_rows: list[dict[str, Any]] | None = None
) -> Station | None:
    """A colleague's station for this task, matched strictly. When more than
    one takes it, the one with the most successful runs — the proven one."""
    hits: list[Station] = []
    for st in candidates:
        if st.matches_task(description, strict=True):
            hits.append(st)
            continue
        for r in adopted_rows or []:
            if r.get("author") == st.author and r.get("station") == st.name:
                if any(
                    phrase_matches(str(p), description, strict=True) for p in r.get("phrases") or []
                ):
                    hits.append(st)
                    break
    if len(hits) <= 1:
        return hits[0] if hits else None

    def proven(st: Station) -> int:
        return sum(1 for r in runs(st.directory.parent.parent) if r.station == st.name and r.ok)

    return max(hits, key=lambda st: (proven(st), st.author, st.name))


def add_notice(agent_dir: Path, text: str) -> None:
    """Something the floor has to tell its agent once — shown in her floor
    report for a day."""
    try:
        root = factory_dir(agent_dir)
        root.mkdir(parents=True, exist_ok=True)
        with (root / NOTICES_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "text": text}) + "\n")
    except OSError:
        logger.debug("could not record floor notice", exc_info=True)


def notices(agent_dir: Path, *, since: float) -> list[str]:
    out: list[str] = []
    try:
        for line in (factory_dir(agent_dir) / NOTICES_FILE).read_text("utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and float(d.get("at") or 0) >= since:
                text = str(d.get("text") or "").strip()
                if text and text not in out:
                    out.append(text)
    except OSError:
        pass
    return out


def find_equivalent(
    *, name: str, purpose: str, matches: list[str], candidates: list[Station]
) -> Station | None:
    """A station that already does what this one would. Deliberately hard to
    trip — telling an agent her tool exists when it does not costs her the
    tool:

    * the shorter name's words are (nearly) all in the longer's
      ('automate-directive-scope-and-delegate' against
      'automate-directive-scope-delegate-and-track'); or
    * they share a match phrase, word for word; or
    * the purposes are the same sentence, give or take a few words.

    'narrative-asset-routing' and 'narrative-asset-drafting' are two
    stations; so are three weekly sweeps that end in the same boilerplate."""
    n = roots(name.replace("-", " ").replace("_", " "))
    pr = roots(purpose, purpose=True)
    # A phrase counts once the words for building and the boilerplate are
    # out of it: "automate manual sweep" sat on four different stations.
    phrase_sets = [ps for ps in (frozenset(roots(m, purpose=True)) for m in matches) if len(ps) > 1]
    for st in candidates:
        other = roots(st.name.replace("-", " ").replace("_", " "))
        both = n & other
        if len(both) >= 2 and len(both) / max(1, min(len(n), len(other))) >= 0.75:
            return st
        theirs = {frozenset(roots(m, purpose=True)) for m in st.matches}
        if any(ps in theirs for ps in phrase_sets):
            return st
        opr = roots(st.purpose, purpose=True)
        shared_words = pr & opr
        if len(shared_words) >= 4 and len(shared_words) / max(1, min(len(pr), len(opr))) >= 0.75:
            return st
    return None


def _last_json_line(text: str) -> dict[str, Any] | None:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


async def run_station(
    station: Station,
    *,
    task_id: str,
    description: str,
    agent_id: str,
    env: dict[str, str] | None = None,
    agent_dir: Path | None = None,
    author: str = "",
) -> StationRun:
    """Run one task through a station. Never raises: a failure of any kind
    is a StationRun with ok=False and the reason in ``error``.

    ``agent_id`` / ``agent_dir`` are whose work this is. With ``author``
    set the station is a colleague's: it runs in a working directory of the
    caller's, its code addressed by absolute path and left where it is."""
    job: dict[str, Any] = {"task_id": task_id, "description": description, "agent_id": agent_id}
    argv = list(station.run)
    cwd = station.directory
    if agent_dir is not None:
        job["agent_dir"] = str(agent_dir)
        job["station_dir"] = str(station.directory)
    if author and agent_dir is not None:
        job["station_author"] = author
        argv = [
            str(station.directory / a) if (station.directory / a).is_file() else a for a in argv
        ]
        cwd = factory_dir(agent_dir) / SHARED_WORK_DIR / author / station.name
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return StationRun(
                station=station.name,
                task_id=task_id,
                description=description[:200],
                ok=False,
                error=f"could not start: {exc}"[:300],
            )
    payload = json.dumps(job)
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        return StationRun(
            station=station.name,
            task_id=task_id,
            description=description[:200],
            ok=False,
            error=f"could not start: {exc}"[:300],
        )
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(payload.encode("utf-8")), timeout=station.timeout_s
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return StationRun(
            station=station.name,
            task_id=task_id,
            description=description[:200],
            ok=False,
            error=f"timed out after {station.timeout_s:.0f}s",
            seconds=round(time.monotonic() - started, 2),
        )
    seconds = round(time.monotonic() - started, 2)
    out = out_b.decode("utf-8", "replace")
    err = err_b.decode("utf-8", "replace")
    result = _last_json_line(out)
    if proc.returncode != 0:
        return StationRun(
            station=station.name,
            task_id=task_id,
            description=description[:200],
            ok=False,
            error=f"exit {proc.returncode}: {(err or out).strip()[-300:]}",
            seconds=seconds,
        )
    if result is None:
        return StationRun(
            station=station.name,
            task_id=task_id,
            description=description[:200],
            ok=False,
            error=f"no JSON result on stdout: {out.strip()[-200:]}",
            seconds=seconds,
        )
    ok = bool(result.get("ok"))
    return StationRun(
        station=station.name,
        task_id=task_id,
        description=description[:200],
        ok=ok,
        summary=str(result.get("summary") or "")[:400],
        artifact=str(result.get("artifact") or "")[:300],
        error="" if ok else str(result.get("error") or result.get("summary") or "failed")[:300],
        seconds=seconds,
    )


def record_run(agent_dir: Path, run: StationRun) -> None:
    try:
        root = factory_dir(agent_dir)
        root.mkdir(parents=True, exist_ok=True)
        with (root / RUNS_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(run.to_dict()) + "\n")
    except OSError:
        logger.debug("could not record station run", exc_info=True)


def runs(agent_dir: Path, *, since: float | None = None) -> list[StationRun]:
    path = factory_dir(agent_dir) / RUNS_FILE
    out: list[StationRun] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            if since is not None and float(d.get("at") or 0) < since:
                continue
            try:
                out.append(
                    StationRun(**{k: d[k] for k in StationRun.__dataclass_fields__ if k in d})
                )
            except TypeError:
                continue
    except OSError:
        pass
    return out


def _age_s(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return 0.0


def _midnight() -> float:
    return datetime.combine(date.today(), datetime.min.time()).timestamp()


def floor_report(
    agent_dir: Path,
    *,
    team: list[tuple[str, Path]] | None = None,
    demand: dict[str, int] | None = None,
) -> str:
    """One block for context: every station, today's runs and failures, the
    last exception verbatim. The agent's job is to read this and act on it.

    With ``team`` it is also the team's floor seen from her side: which of
    her stations her colleagues' work went through, and which of theirs
    take hers — each with its builder's name on it. ``demand`` is, per
    station of hers that is not shared, how often colleagues did matching
    work by hand in the last fortnight."""
    all_stations = stations(agent_dir)
    theirs = team_stations(team) if team else []
    told = notices(agent_dir, since=time.time() - 86400)
    if not all_stations and not theirs and not told:
        return ""
    ledger = runs(agent_dir)
    midnight = _midnight()
    week = time.time() - 7 * 86400
    lines = ["## 🏭 Your factory floor", ""]
    # The advice lines are kept few: the three stations colleagues most
    # needed, and three that have been ready a day without a single task.
    most_wanted = sorted((demand or {}).items(), key=lambda kv: -kv[1])[:3]
    asked_for = {name for name, n in most_wanted if n > 0}
    idle_hints = 3
    for st in all_stations:
        rows = [r for r in ledger if r.station == st.name and not r.author]
        mine = [r for r in rows if not r.used_by and r.at >= midnight]
        failed = [r for r in mine if not r.ok]
        lent = [r for r in rows if r.used_by and r.at >= week]
        problem = sharing_problem(st) if st.ready else ""
        if not st.ready:
            state = "BEING BUILT — not yet taking work"
        elif not team:
            state = "ready"
        elif problem:
            state = "ready, yours alone"
        else:
            state = "ready, shared with the team"
        line = (
            f"- **{st.name}** ({state}): {st.purpose or 'no purpose written'} — "
            f"today {len(mine)} run(s), {len(failed)} failed"
        )
        if lent:
            by: dict[str, int] = {}
            for r in lent:
                by[r.used_by] = by.get(r.used_by, 0) + 1
            who = ", ".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1]))
            bad = sum(1 for r in lent if not r.ok)
            line += f"; built by you, it did colleagues' work {len(lent)} time(s) this week ({who})"
            if bad:
                line += f", {bad} failed"
        lines.append(line)
        if failed:
            last = failed[-1]
            lines.append(f"  last failure: {last.error[:160]} (task: {last.description[:60]})")
        lent_failed = [r for r in lent if not r.ok]
        if lent_failed:
            last = lent_failed[-1]
            lines.append(
                f"  last failure for {last.used_by}: {last.error[:160]} "
                f"(task: {last.description[:60]})"
            )
        if st.ready and not rows and idle_hints and _age_s(st.directory / MANIFEST) > 86400:
            idle_hints -= 1
            lines.append(
                "  never run — no task has arrived in the words it listens for "
                f"({'; '.join(st.matches[:4]) or 'none'}). Put the wording your tasks "
                "really arrive in into `matches` in its station.yaml."
            )
        wanted = (demand or {}).get(st.name, 0) if st.name in asked_for else 0
        if st.ready and problem and (wanted or st.shared):
            ask = (
                f"colleagues did work it would take by hand {wanted} time(s) in the last 14 days"
                if wanted
                else "its manifest says `shared: true`"
            )
            # What is wrong with the code, not merely that the flag is off.
            why = _sharing_problem(st, opted_in=True) or problem
            lines.append(
                f"  {ask}, but it cannot run for them: {why}. To put it on the team's floor "
                "under your name, take `agent_dir` from the input and set `shared: true`."
            )
    if theirs:
        lines += ["", "### Stations your colleagues built that take your work", ""]
        for st in theirs[:8]:
            for_me = [r for r in ledger if r.station == st.name and r.author == st.author and r.ok]
            today = sum(1 for r in for_me if r.at >= midnight)
            lines.append(
                f"- **{st.name}** — built by {st.author}: {st.purpose or 'no purpose written'}"
                f" — ran for you {today} time(s) today, {len(for_me)} in all"
            )
        if len(theirs) > 8:
            lines.append(f"- …and {len(theirs) - 8} more")
    for text in told[-4:]:
        lines.append("")
        lines.append(f"Note: {text}")
    lines.append("")
    lines.append(
        "A station that fails is yours to fix — read its last failure, repair the "
        "code, run it again. A step you keep doing by hand is a station to build "
        "(`build_station`). Work that matches a ready station goes through it without "
        "you; your attention belongs on what fell off the line."
    )
    if theirs:
        lines.append(
            "A colleague's station stays theirs: when one fails on your work the task "
            "comes back to you and the failure goes to its builder. Before you build, "
            "look at what is already on the team's floor."
        )
    return "\n".join(lines)


def scaffold_station(
    agent_dir: Path,
    *,
    name: str,
    purpose: str,
    matches: list[str],
    brief: str,
) -> Path | None:
    """Create the station directory, a manifest with ``ready: false`` and a
    STATION.md brief for whoever writes the code. Returns the directory, or
    None when the name is unusable or the station already exists.

    A new station is ``shared: true``: its stub takes the caller from the
    input, and :func:`sharing_problem` re-reads the finished code every time
    a colleague's task looks for a station, so one written against its
    builder's own directory stays private whatever the manifest says."""
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        return None
    d = factory_dir(agent_dir) / name
    if d.exists():
        return None
    d.mkdir(parents=True)
    manifest = {
        "name": name,
        "purpose": purpose.strip(),
        "matches": [m for m in matches if str(m).strip()],
        "run": ["python3", "run.py"],
        "timeout_s": int(DEFAULT_TIMEOUT_S),
        "ready": False,
        "shared": True,
    }
    (d / MANIFEST).write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (d / "STATION.md").write_text(
        f"# Station: {name}\n\n"
        f"**Purpose:** {purpose.strip()}\n\n"
        f"**Takes work matching:** {', '.join(manifest['matches']) or '(none yet)'}\n\n"
        f"## Brief\n\n{brief.strip()}\n\n"
        "## Contract\n\n"
        '`run.py` reads one JSON object on stdin — `{"task_id", "description", '
        '"agent_id", "agent_dir", "station_dir"}` — does the work, and prints one JSON '
        'object as the LAST line of stdout: `{"ok": true|false, "summary": "what '
        'happened", "artifact": "<url if any>"}`. Exit 0. Credentials arrive in the '
        "environment (the same ones your terminal sessions get). Log to stderr. When it "
        "passes your own test against a real task, set `ready: true` in station.yaml — "
        "from then on the fabric routes matching work through it without asking you.\n\n"
        "## Matching\n\n"
        "`matches` is the wording tasks really arrive in, not a description of the "
        "station: phrases of two or more words (every word must be in the task), or a "
        "`/regex/`. A station nobody's task ever matches does nothing.\n\n"
        "## Your station, the team's floor\n\n"
        "This station is yours — your name stays on it and only you change it. With "
        "`shared: true` it also takes colleagues' matching work when they have no "
        "station of their own for it, and your floor report shows who it worked for. "
        "For that it must do the work for whoever the input names:\n\n"
        "- `agent_dir` is the directory of the agent whose task this is (you, or a "
        "colleague). Read and write ledgers, inbox, outbox and workspace THERE.\n"
        "- `agent_id` is who that is; credentials in the environment are theirs.\n"
        "- `station_dir` is this directory — your code and helper files, read only.\n"
        "- Do not use `__file__`, `.parent.parent`, `..` or an absolute path under the "
        "agents' home to find a directory: a station that does is kept private, "
        "because it would do a colleague's work in your files and call it done.\n\n"
        "Set `shared: false` if this step only makes sense for you.\n",
        encoding="utf-8",
    )
    (d / "run.py").write_text(
        "#!/usr/bin/env python3\n"
        '"""Not built yet — see STATION.md."""\n'
        "import json, sys\n"
        "from pathlib import Path\n\n"
        "task = json.load(sys.stdin)\n"
        "# Whose work this is — you or a colleague. Never your own path.\n"
        'agent_dir = Path(task["agent_dir"])\n'
        'print(json.dumps({"ok": False, "summary": "station not built yet", '
        '"error": "not built"}))\n'
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    return d
