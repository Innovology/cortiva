"""The factory floor: stations the agent wrote as code, work routed through
them with no model ask, failures on her pile, the floor in her context."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core import factory as fx
from cortiva.core.agent import AgentState, Task, TaskQueue
from cortiva.core.fabric import Fabric
from cortiva.core.reflection import ReflectionSuffix

PY = sys.executable


def _station(agent_dir: Path, name: str, body: str, *, matches=None, ready=True, timeout=30):
    d = agent_dir / "factory" / name
    d.mkdir(parents=True)
    (d / "run.py").write_text(body)
    (d / "station.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "purpose": f"{name} purpose",
                "matches": matches or [name],
                "run": [PY, "run.py"],
                "timeout_s": timeout,
                "ready": ready,
            }
        )
    )
    return d


OK_BODY = (
    "import json, sys\n"
    "t = json.load(sys.stdin)\n"
    "print('log line to ignore')\n"
    'print(json.dumps({"ok": True, "summary": "digest sent for " + t["description"], '
    '"artifact": "https://x/digest/1"}))\n'
)
FAIL_BODY = "import json, sys\nprint(json.dumps({'ok': False, 'error': 'SMTP refused'}))\n"
CRASH_BODY = "import sys\nsys.stderr.write('Traceback: boom\\n'); sys.exit(1)\n"
HANG_BODY = "import time\ntime.sleep(30)\n"


def test_manifest_match_and_readiness(tmp_path: Path) -> None:
    _station(tmp_path, "weekly-digest", OK_BODY, matches=["weekly digest", "/^send .*digest/"])
    _station(tmp_path, "sweep", OK_BODY, ready=False)
    assert [s.name for s in fx.stations(tmp_path)] == ["sweep", "weekly-digest"]
    ready = fx.stations(tmp_path, ready_only=True)
    assert [s.name for s in ready] == ["weekly-digest"]
    assert fx.match(ready, "Compile the Weekly Digest for Monday").name == "weekly-digest"
    assert fx.match(ready, "Send everyone the digest").name == "weekly-digest"
    assert fx.match(ready, "Run the sweep") is None  # not ready
    assert fx.match(ready, "Write the board paper") is None


def test_runs_follow_the_contract_and_every_failure_is_an_exception(tmp_path: Path) -> None:
    ok = _station(tmp_path, "ok", OK_BODY)
    fail = _station(tmp_path, "fail", FAIL_BODY)
    crash = _station(tmp_path, "crash", CRASH_BODY)
    hang = _station(tmp_path, "hang", HANG_BODY, timeout=5)
    st = {s.name: s for s in fx.stations(tmp_path)}
    st["hang"].timeout_s = 1.0

    async def go(name):
        return await fx.run_station(st[name], task_id="t1", description="x", agent_id="a")

    r = asyncio.run(go("ok"))
    assert r.ok and r.summary == "digest sent for x" and r.artifact == "https://x/digest/1"
    r = asyncio.run(go("fail"))
    assert not r.ok and r.error == "SMTP refused"
    r = asyncio.run(go("crash"))
    assert not r.ok and r.error.startswith("exit 1") and "boom" in r.error
    r = asyncio.run(go("hang"))
    assert not r.ok and "timed out" in r.error
    for run in (r,):
        fx.record_run(tmp_path, run)
    assert fx.runs(tmp_path)[0].station == "hang"
    assert ok.is_dir() and fail.is_dir() and crash.is_dir() and hang.is_dir()


def test_floor_report_shows_throughput_failures_and_build_state(tmp_path: Path) -> None:
    assert fx.floor_report(tmp_path) == ""  # no floor yet — nothing to nag about
    _station(tmp_path, "digest", OK_BODY)
    _station(tmp_path, "sweep", OK_BODY, ready=False)
    fx.record_run(tmp_path, fx.StationRun("digest", "t1", "Weekly digest", True, "sent"))
    fx.record_run(
        tmp_path, fx.StationRun("digest", "t2", "Weekly digest", False, error="SMTP refused")
    )
    rep = fx.floor_report(tmp_path)
    assert "**digest** (ready): digest purpose — today 2 run(s), 1 failed" in rep
    assert "last failure: SMTP refused" in rep
    assert "**sweep** (BEING BUILT — not yet taking work)" in rep
    assert "yours to fix" in rep


def test_scaffold_writes_manifest_brief_and_a_stub_that_is_not_ready(tmp_path: Path) -> None:
    d = fx.scaffold_station(
        tmp_path, name="Weekly-Digest", purpose="Send it", matches=["weekly digest"], brief="Do X"
    )
    assert d is not None and d.name == "weekly-digest"
    m = yaml.safe_load((d / "station.yaml").read_text())
    assert m["ready"] is False and m["matches"] == ["weekly digest"]
    assert "Do X" in (d / "STATION.md").read_text()
    assert fx.stations(tmp_path, ready_only=True) == []
    assert (
        fx.scaffold_station(tmp_path, name="weekly-digest", purpose="p", matches=["x"], brief="b")
        is None
    )
    assert (
        fx.scaffold_station(tmp_path, name="Bad Name!", purpose="p", matches=["x"], brief="b")
        is None
    )


class _Model:
    def __init__(self) -> None:
        self.calls = 0

    async def think(self, agent_id, context, prompt, **kwargs):
        self.calls += 1
        return ConsciousResponse(content="thought", tokens_in=1, tokens_out=1, model="m")

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(content="ok", tokens_in=1, tokens_out=1, model="m")


def _executing(fabric: Fabric, aid: str, description: str):
    agent = fabric.register_agent(aid, consciousness_budget=100)
    agent.transition(AgentState.WAKING)
    agent.transition(AgentState.PLANNING)
    agent.transition(AgentState.EXECUTING)
    agent.task_queue = TaskQueue(tasks=[Task(id="t1", description=description)])
    return agent


@pytest.mark.asyncio
async def test_matching_work_goes_through_the_station_without_a_model_ask(tmp_path: Path) -> None:
    model = _Model()
    fabric = Fabric(agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=model)
    agent = _executing(fabric, "amara", "Send the weekly digest")
    _station(agent.directory, "digest", OK_BODY, matches=["weekly digest"])
    await fabric.cycle("amara")
    task = agent.task_queue.tasks[0]
    assert task.status == "done"
    assert task.outcome.startswith("[station digest] digest sent for")
    assert "https://x/digest/1" in task.outcome
    assert model.calls == 0
    assert agent.tasks_completed_today == 1
    assert fx.runs(agent.directory)[0].ok
    assert "today 1 run(s), 0 failed" in fabric._factory_context(agent)


@pytest.mark.asyncio
async def test_a_failing_station_is_an_exception_on_her_pile_not_a_think(tmp_path: Path) -> None:
    model = _Model()
    fabric = Fabric(agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=model)
    agent = _executing(fabric, "amara", "Send the weekly digest")
    _station(agent.directory, "digest", FAIL_BODY, matches=["weekly digest"])
    await fabric.cycle("amara")
    task = agent.task_queue.tasks[0]
    assert task.status == "exception" and task.error == "[station digest] SMTP refused"
    assert model.calls == 0
    assert "last failure: SMTP refused" in fabric._factory_context(agent)


@pytest.mark.asyncio
async def test_build_station_scaffolds_and_queues_the_build_as_hands_on_work(
    tmp_path: Path,
) -> None:
    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model()
    )
    agent = _executing(fabric, "amara", "Something else")
    fabric._handle_build_station(
        agent,
        {
            "name": "weekly-digest",
            "purpose": "Send Monday's digest",
            "matches": ["weekly digest"],
            "brief": "Read merged PRs, write the digest, send via the mail tool.",
        },
    )
    assert (agent.directory / "factory" / "weekly-digest" / "STATION.md").is_file()
    build = [t for t in agent.task_queue.tasks if "station 'weekly-digest'" in t.description]
    assert len(build) == 1 and build[0].priority == 1
    assert fabric._is_terminal_task(build[0].description)  # hands-on, not a think
    assert "BEING BUILT" in fabric._factory_context(agent)
    # The suffix field reaches the handler.
    assert "build_station" in ReflectionSuffix.__dataclass_fields__
    # Not ready → a matching task is NOT routed through the stub.
    assert fx.match(fx.stations(agent.directory, ready_only=True), "weekly digest") is None


def test_a_multi_word_phrase_matches_when_all_its_content_words_appear(tmp_path: Path) -> None:
    """Yuki's station: 'github feedback sweep' vs her task 'Triage 11 open
    GitHub feedback threads: delegate merge/sweep'."""
    _station(tmp_path, "sweep", OK_BODY, matches=["github feedback sweep", "automate review open"])
    st = fx.stations(tmp_path, ready_only=True)
    assert fx.match(st, "Triage 11 open GitHub feedback threads: delegate merge/sweep") is not None
    assert fx.match(st, "Sweep the office") is None  # one word of three is not a match
    assert fx.match(st, "Write the board paper") is None


def test_a_task_that_names_a_multi_word_station_matches_it(tmp_path: Path) -> None:
    """yuki, 2026-11-07 00:34: '**Run github-feedback-sweep**: Process 23 open
    threads' went to an 11-tool Claude session. Naming the station is the
    match; a single-word name ('founder') is not."""
    _station(tmp_path, "github-feedback-sweep", OK_BODY, matches=["nothing in particular"])
    _station(tmp_path, "founder", OK_BODY, matches=["steer live dashboard"])
    st = fx.stations(tmp_path, ready_only=True)
    hit = fx.match(st, "**Run github-feedback-sweep**: Process 23 open threads")
    assert hit is not None and hit.name == "github-feedback-sweep"
    assert fx.match(st, "Email the founder about the office move") is None
