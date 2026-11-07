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
DEFAULT_TIMEOUT_S = 300.0
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")


@dataclass
class Station:
    name: str
    purpose: str
    matches: list[str]
    run: list[str]
    directory: Path
    timeout_s: float = DEFAULT_TIMEOUT_S
    ready: bool = False

    def matches_task(self, description: str) -> bool:
        """A /regex/ matches as written. A phrase matches as a substring, or —
        for a multi-word phrase — when all of its content words appear in the
        task in any order: Yuki's station said "github feedback sweep" and her
        tasks say "Triage 11 open GitHub feedback threads: delegate merge/sweep"
        (2026-11-06 21:48)."""
        text = (description or "").strip()
        low = text.lower()
        words = set(re.findall(r"[a-z0-9]+", low))
        # Named outright: "Run github-feedback-sweep: process 23 open threads"
        # (yuki, 2026-11-07 00:34 — it went to an 11-tool Claude session on a
        # node whose floor only had the ordered phrases). A multi-word slug
        # only; a single word like "founder" is not a name match.
        name = str(getattr(self, "name", "") or "").strip().lower()
        if "-" in name and name in low:
            return True
        for pat in self.matches:
            p = str(pat or "").strip()
            if not p:
                continue
            if len(p) > 2 and p.startswith("/") and p.endswith("/"):
                try:
                    if re.search(p[1:-1], text, re.IGNORECASE):
                        return True
                except re.error:
                    continue
                continue
            if p.lower() in low:
                return True
            content = [w for w in re.findall(r"[a-z0-9]+", p.lower()) if len(w) > 2]
            if len(content) >= 2 and all(w in words for w in content):
                return True
        return False


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
) -> StationRun:
    """Run one task through a station. Never raises: a failure of any kind
    is a StationRun with ok=False and the reason in ``error``."""
    payload = json.dumps({"task_id": task_id, "description": description, "agent_id": agent_id})
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *station.run,
            cwd=str(station.directory),
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


def _midnight() -> float:
    return datetime.combine(date.today(), datetime.min.time()).timestamp()


def floor_report(agent_dir: Path) -> str:
    """One block for context: every station, today's runs and failures, the
    last exception verbatim. The agent's job is to read this and act on it."""
    all_stations = stations(agent_dir)
    if not all_stations:
        return ""
    today = runs(agent_dir, since=_midnight())
    lines = ["## 🏭 Your factory floor", ""]
    for st in all_stations:
        mine = [r for r in today if r.station == st.name]
        failed = [r for r in mine if not r.ok]
        state = "ready" if st.ready else "BEING BUILT — not yet taking work"
        lines.append(
            f"- **{st.name}** ({state}): {st.purpose or 'no purpose written'} — "
            f"today {len(mine)} run(s), {len(failed)} failed"
        )
        if failed:
            last = failed[-1]
            lines.append(f"  last failure: {last.error[:160]} (task: {last.description[:60]})")
    lines.append("")
    lines.append(
        "A station that fails is yours to fix — read its last failure, repair the "
        "code, run it again. A step you keep doing by hand is a station to build "
        "(`build_station`). Work that matches a ready station goes through it without "
        "you; your attention belongs on what fell off the line."
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
    None when the name is unusable or the station already exists."""
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
    }
    (d / MANIFEST).write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (d / "STATION.md").write_text(
        f"# Station: {name}\n\n"
        f"**Purpose:** {purpose.strip()}\n\n"
        f"**Takes work matching:** {', '.join(manifest['matches']) or '(none yet)'}\n\n"
        f"## Brief\n\n{brief.strip()}\n\n"
        "## Contract\n\n"
        '`run.py` reads one JSON object on stdin — `{"task_id", "description", '
        '"agent_id"}` — does the work, and prints one JSON object as the LAST line '
        'of stdout: `{"ok": true|false, "summary": "what happened", "artifact": '
        '"<url if any>"}`. Exit 0. Credentials arrive in the environment (the same '
        "ones your terminal sessions get). Log to stderr. When it passes your own test "
        "against a real task, set `ready: true` in station.yaml — from then on the "
        "fabric routes matching work through it without asking you.\n",
        encoding="utf-8",
    )
    (d / "run.py").write_text(
        "#!/usr/bin/env python3\n"
        '"""Not built yet — see STATION.md."""\n'
        "import json, sys\n"
        "task = json.load(sys.stdin)\n"
        'print(json.dumps({"ok": False, "summary": "station not built yet", '
        '"error": "not built"}))\n'
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    return d
