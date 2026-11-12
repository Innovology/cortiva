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


# ---------------------------------------------------------------------------
# The team floor. What a week on Mini-2 (5-12 November 2026, five agents)
# showed: 56 stations, each private to its builder; one agent's station took
# "Track to delivery; mark the commitment delivered" while four colleagues
# did the identical step by hand ~350 times; one agent scaffolded fourteen
# variants of one station; a ready station never ran because it listened for
# its own description. The strings below are the node's.
# ---------------------------------------------------------------------------

import json  # noqa: E402

# A station that takes whose work it is from its input — safe for a colleague.
SHARED_BODY = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "t = json.load(sys.stdin)\n"
    'agent_dir = Path(t["agent_dir"])\n'
    '(agent_dir / "delivered.txt").write_text(t["agent_id"] + ":" + t["description"])\n'
    'Path("scratch.txt").write_text("relative write")\n'
    'print(json.dumps({"ok": True, "summary": "tracked for " + t["agent_id"]}))\n'
)
# A station written the way all 33 ready ones on the node were: against its
# builder's own directory.
BOUND_BODY = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "AGENT_DIR = Path(__file__).parent.parent.parent  # .../agents/amara\n"
    "t = json.load(sys.stdin)\n"
    '(AGENT_DIR / "delivered.txt").write_text("done")\n'
    'print(json.dumps({"ok": True, "summary": "tracked"}))\n'
)
SHARED_FAIL_BODY = (
    "import json, sys\n"
    "t = json.load(sys.stdin)\n"
    't["agent_dir"]\n'
    "print(json.dumps({'ok': False, 'error': 'ledger unreadable'}))\n"
)
TRACK = "Track to delivery; mark the commitment delivered"


def _share(station_dir: Path, shared: bool = True) -> None:
    m = yaml.safe_load((station_dir / "station.yaml").read_text())
    m["shared"] = shared
    (station_dir / "station.yaml").write_text(yaml.safe_dump(m))


def _team(tmp_path: Path, *ids: str, model=None):
    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=model or _Model()
    )
    agents = []
    for aid in ids:
        a = fabric.register_agent(aid, consciousness_budget=100)
        a.transition(AgentState.WAKING)
        a.transition(AgentState.PLANNING)
        a.transition(AgentState.EXECUTING)
        a.task_queue = TaskQueue(tasks=[])
        agents.append(a)
    return fabric, agents


def _done_rows(agent, description: str, n: int, outcome: str = "did it by hand") -> None:
    import time as _time

    with (agent.directory / "done.jsonl").open("a") as fh:
        for _ in range(n):
            fh.write(
                json.dumps(
                    {
                        "at": _time.time(),
                        "stem": Fabric._stem(description),
                        "description": description,
                        "outcome": outcome,
                    }
                )
                + "\n"
            )


# -- matching that meets the real wording ------------------------------------


def test_a_station_named_for_automating_a_step_takes_the_step(tmp_path: Path) -> None:
    """cto's 'automate-ci-sweep-and-report' was ready for five days and never
    ran: it listened for its own description. The work arrived as 'Live CI
    Sweep & Status Report — …'. A PR triage is a different step and stays
    out — so does anything that merely sweeps and reports."""
    _station(
        tmp_path,
        "automate-ci-sweep-and-report",
        OK_BODY,
        matches=["collect status innovology", "automate ci sweep and report"],
    )
    st = fx.stations(tmp_path, ready_only=True)
    for task in (
        "Live CI Sweep & Status Report — innovology.io, sailcoach, cortiva-hq → maren@workforce.innovology.io",
        "**Deliver BetterAuth & CI Status Report to Maren** — Run the live sweep, verify PRs #190",
    ):
        assert fx.match(st, task) is not None, task
    for task in (
        "Execute triage on the 39 open GitHub PR threads",
        "Sweep the office and report back",
        "Write the weekly report",
    ):
        assert fx.match(st, task) is None, task


def test_reply_to_originator_reaches_the_station_built_for_it(tmp_path: Path) -> None:
    """yuki's 'automate-reply-to-originator' (matches: 'automate week manual',
    its own name) never ran; she did 'Reply to <originator> with the outcome'
    by hand 11 times."""
    _station(
        tmp_path,
        "automate-reply-to-originator",
        OK_BODY,
        matches=["automate week manual", "automate reply to originator"],
    )
    st = fx.stations(tmp_path, ready_only=True)
    assert fx.match(st, "Reply to <originator> with the outcome") is not None
    assert fx.match(st, "Reply to the board") is None


def test_one_word_is_a_whole_word_for_the_builder_and_nothing_for_a_colleague(
    tmp_path: Path,
) -> None:
    """amara's 'escalation-triage' lists 'blocked' and 'stall'. As substrings
    they took 'Unblocked: PR #257 access granted — proceed with review'."""
    _station(tmp_path, "escalation-triage", OK_BODY, matches=["blocked", "stall", "/^halt/"])
    (st,) = fx.stations(tmp_path, ready_only=True)
    assert st.matches_task("[Blocked] CI gate on PR #240")
    assert not st.matches_task("Unblocked: PR #257 access granted — proceed with review")
    assert not st.matches_task("Install the new linter")
    assert st.matches_task("Halt the rollout")
    # For a colleague's task neither a single word nor a regex counts.
    assert not st.matches_task("[Blocked] CI gate on PR #240", strict=True)
    assert not st.matches_task("Halt the rollout", strict=True)


def test_two_short_words_match_as_written_not_scattered(tmp_path: Path) -> None:
    """'merge pr' is a phrase; 'merge' and 'PR' somewhere in a CI fix is not."""
    _station(tmp_path, "delegation", OK_BODY, matches=["merge pr", "track to delivery"])
    (st,) = fx.stations(tmp_path, ready_only=True)
    assert st.matches_task("Merge PR #264 once green")
    assert not st.matches_task("Fix CI failures on PR #240 and merge to unblock the homepage")
    assert st.matches_task(TRACK) and st.matches_task(TRACK, strict=True)


def test_work_on_a_station_is_never_work_for_a_station(tmp_path: Path) -> None:
    """A build or repair task names the station and repeats its purpose."""
    _station(tmp_path, "ci-sweep-report", OK_BODY, matches=["ci sweep report"])
    _station(tmp_path, "pr-merge-sweep", OK_BODY, matches=["pr merge sweep"])
    st = fx.stations(tmp_path, ready_only=True)
    assert (
        fx.match(
            st,
            "Write and test the code for station 'ci-sweep-report' in factory/ci-sweep-report/"
            "run.py (read STATION.md there first). Purpose: CI sweep and report.",
        )
        is None
    )
    assert fx.match(st, "Fix the failing ci-sweep-report station") is None
    assert fx.match(st, "build_station: ci-sweep-report — the CI sweep and report") is None
    assert fx.match(st, "Run pr-merge-sweep: 12 open threads").name == "pr-merge-sweep"


# -- names -------------------------------------------------------------------


def test_a_long_name_is_cut_at_a_word_and_the_purpose_stays_whole(tmp_path: Path) -> None:
    """cto: 'build_station: automate-directive-scope-delegate-and-track — Code
    the …' became station '…-and-tr' with purpose 'ack — Code the …'; another
    got the purpose 'cking' and with it the match phrase 'cking'."""
    assert fx.slug("Automate_Directive Scope") == "automate-directive-scope"
    long = "automate-directive-scope-delegate-and-track-to-delivery-and-reply-with-the-outcome"
    cut = fx.slug(long)
    assert len(cut) <= fx.MAX_NAME_LEN and long.startswith(cut + "-")
    assert not cut.endswith(("-and", "-the", "-with"))

    fabric, (cto,) = _team(tmp_path, "cto")
    cto.task_queue.tasks.append(
        Task(
            id="a",
            description=(
                "build_station: automate-directive-reply-and-outcome-tracking — Code the "
                "reply and outcome tracking steps so the fabric runs them"
            ),
        )
    )
    assert fabric._scaffold_stations_from_plan(cto) == 1
    d = cto.directory / "factory" / "automate-directive-reply-and-outcome-tracking"
    m = yaml.safe_load((d / "station.yaml").read_text())
    assert m["purpose"].startswith("Code the reply and outcome tracking")
    assert all(len(fx.content_words(p)) >= 2 for p in m["matches"])


# -- the sharing contract ----------------------------------------------------


def test_only_a_station_that_takes_the_caller_from_its_input_is_shared(tmp_path: Path) -> None:
    def problem(name: str, body: str, *, shared: bool | None = True) -> str:
        d = _station(tmp_path / "agents" / "amara", name, body, matches=["track to delivery"])
        if shared is not None:
            _share(d, shared)
        return fx.sharing_problem(fx._load_manifest(d))

    assert problem("ok", SHARED_BODY) == ""
    # Every station written before this has no `shared` field: private.
    assert "shared: true" in problem("legacy", SHARED_BODY, shared=None)
    assert "shared: true" in problem("opted-out", SHARED_BODY, shared=False)
    # Opting in is not enough: the code is read.
    assert "__file__" in problem("bound", BOUND_BODY)
    assert "walks up" in problem(
        "walker", "from pathlib import Path\nA = Path.cwd().parent.parent\nagent_dir = A\n"
    )
    assert "home" in problem(
        "named", 'from pathlib import Path\nagent_dir = Path("/Users/x/.cortiva/agents/amara")\n'
    )
    assert "builder's directory" in problem(
        "sibling", 'from pathlib import Path\nagent_dir = Path("agents/amara/inbox")\n'
    )
    assert "agent_dir" in problem("deaf", OK_BODY)


def test_a_new_station_is_scaffolded_shareable_and_teaches_the_contract(tmp_path: Path) -> None:
    d = fx.scaffold_station(
        tmp_path, name="weekly-digest", purpose="Send it", matches=["weekly digest"], brief="b"
    )
    st = fx._load_manifest(d)
    assert st.shared and not st.ready and fx.sharing_problem(st) == ""
    doc = (d / "STATION.md").read_text()
    assert "agent_dir" in doc and "__file__" in doc and "shared: false" in doc


@pytest.mark.asyncio
async def test_a_colleagues_task_goes_through_her_station_for_him(tmp_path: Path) -> None:
    model = _Model()
    fabric, (amara, yuki) = _team(tmp_path, "amara", "yuki", model=model)
    d = _station(amara.directory, "automate-delegation-and-scoping", SHARED_BODY, matches=[TRACK])
    _share(d)
    events: list[dict] = []
    fabric.on_event(lambda kind, e: events.append(e))
    yuki.task_queue.tasks.append(Task(id="t1", description=TRACK))
    await fabric.cycle("yuki")

    task = yuki.task_queue.tasks[0]
    assert task.status == "done" and model.calls == 0
    assert task.outcome == "[station automate-delegation-and-scoping by amara] tracked for yuki"
    # The work was done in HIS directory, with his id; nothing landed in hers.
    assert (yuki.directory / "delivered.txt").read_text() == f"yuki:{TRACK}"
    assert not (amara.directory / "delivered.txt").exists()
    assert sorted(p.name for p in d.iterdir()) == ["run.py", "station.yaml"]
    work = yuki.directory / "factory" / ".shared" / "amara" / "automate-delegation-and-scoping"
    assert (work / "scratch.txt").read_text() == "relative write"
    # Ledgered on both floors; counted as done work of his, not hand work.
    (his,) = fx.runs(yuki.directory)
    (hers,) = fx.runs(amara.directory)
    assert his.ok and his.author == "amara" and his.used_by == ""
    assert hers.ok and hers.used_by == "yuki" and hers.author == ""
    assert yuki.tasks_completed_today == 1
    assert fabric._recurring_steps_context(yuki) == ""
    # The credit event names the builder.
    credit = [e for e in events if e["type"] == "station.used_by_colleague"]
    assert len(credit) == 1
    assert credit[0]["agent_id"] == "amara" and credit[0]["used_by"] == "yuki"
    assert credit[0]["trace_id"] == "t1" and credit[0]["station"] == d.name
    ran = next(e for e in events if e["type"] == "station.ran")
    assert ran["agent_id"] == "yuki" and ran["author"] == "amara"

    # Authorship is visible from both sides.
    hers_view = fabric._factory_context(amara)
    assert "(ready, shared with the team)" in hers_view
    assert "built by you, it did colleagues' work 1 time(s) this week (yuki 1)" in hers_view
    assert "today 0 run(s), 0 failed" in hers_view  # his work is not her throughput
    his_view = fabric._factory_context(yuki)
    assert "**automate-delegation-and-scoping** — built by amara" in his_view
    assert "ran for you 1 time(s) today" in his_view


@pytest.mark.asyncio
async def test_his_own_station_comes_first(tmp_path: Path) -> None:
    fabric, (amara, yuki) = _team(tmp_path, "amara", "yuki")
    _share(_station(amara.directory, "hers", SHARED_BODY, matches=[TRACK]))
    _station(yuki.directory, "his", OK_BODY, matches=[TRACK])
    yuki.task_queue.tasks.append(Task(id="t1", description=TRACK))
    await fabric.cycle("yuki")
    assert yuki.task_queue.tasks[0].outcome.startswith("[station his] ")
    assert fx.runs(amara.directory) == []


@pytest.mark.asyncio
async def test_a_station_bound_to_its_builder_is_not_run_for_a_colleague(tmp_path: Path) -> None:
    """The node as it stands: amara's station writes to her own directory by
    path. Run for yuki it would mark his commitment delivered in her ledger."""
    model = _Model()
    fabric, (amara, yuki) = _team(tmp_path, "amara", "yuki", model=model)
    _share(_station(amara.directory, "bound", BOUND_BODY, matches=[TRACK]))
    _station(amara.directory, "legacy", SHARED_BODY, matches=[TRACK])  # no `shared` field
    # A shareable station whose only phrase is one word takes nothing of his.
    _share(_station(amara.directory, "greedy", SHARED_BODY, matches=["delivery"]))
    yuki.task_queue.tasks.append(Task(id="t1", description=TRACK))
    await fabric.cycle("yuki")
    assert fx.runs(yuki.directory) == [] and fx.runs(amara.directory) == []
    assert not (amara.directory / "delivered.txt").exists()
    assert model.calls == 1  # his own head, as before
    assert "built by amara" not in fabric._factory_context(yuki)


@pytest.mark.asyncio
async def test_a_colleagues_station_that_fails_gives_the_task_back(tmp_path: Path) -> None:
    model = _Model()
    fabric, (amara, yuki) = _team(tmp_path, "amara", "yuki", model=model)
    _share(_station(amara.directory, "tracker", SHARED_FAIL_BODY, matches=[TRACK]))
    yuki.task_queue.tasks.append(Task(id="t1", description=TRACK))
    await fabric.cycle("yuki")
    task = yuki.task_queue.tasks[0]
    assert task.status != "exception" and not task.error  # not his to fix
    assert model.calls == 1  # he did it himself
    (hers,) = fx.runs(amara.directory)
    assert not hers.ok and hers.used_by == "yuki"
    assert "last failure for yuki: ledger unreadable" in fabric._factory_context(amara)


@pytest.mark.asyncio
async def test_a_shared_station_that_keeps_failing_is_paused_until_it_is_touched(
    tmp_path: Path,
) -> None:
    import os
    import time as _time

    fabric, (amara, yuki) = _team(tmp_path, "amara", "yuki")
    d = _station(amara.directory, "tracker", SHARED_FAIL_BODY, matches=[TRACK])
    _share(d)
    for i in range(4):
        yuki.task_queue.tasks.append(Task(id=f"t{i}", description=TRACK))
        await fabric.cycle("yuki")
    # Three failures for a colleague, then it is no longer offered to him.
    assert len(fx.runs(amara.directory)) == 3
    assert "paused for them until its code changes" in fabric._factory_context(amara)
    # She repairs it: it takes his work again.
    (d / "run.py").write_text(SHARED_BODY)
    os.utime(d / "run.py", (_time.time() + 5, _time.time() + 5))
    yuki.task_queue.tasks.append(Task(id="t9", description=TRACK))
    await fabric.cycle("yuki")
    assert yuki.task_queue.tasks[-1].outcome.startswith("[station tracker by amara]")


def test_the_builder_is_shown_the_demand_for_a_station_that_is_hers_alone(tmp_path: Path) -> None:
    fabric, (amara, yuki, andre) = _team(tmp_path, "amara", "yuki", "andre")
    _station(amara.directory, "automate-delegation-and-scoping", BOUND_BODY, matches=[TRACK])
    _done_rows(yuki, TRACK, 3)
    _done_rows(andre, TRACK, 2)
    _done_rows(andre, TRACK, 4, outcome="[station x] already on a floor")
    rep = fabric._factory_context(amara)
    assert "(ready, yours alone)" in rep
    assert "colleagues did work it would take by hand 5 time(s) in the last 14 days" in rep
    # She is told what in the code keeps it hers, not just that a flag is off.
    assert "it cannot run for them: it finds its files from `__file__`" in rep
    assert "take `agent_dir` from the input and set `shared: true`" in rep
    # Ready today: no word yet about tasks not reaching it.
    assert "never run" not in rep
    # Ready for a day with no task ever reaching it: it says so, with its phrases.
    import os
    import time as _time

    manifest = amara.directory / "factory" / "automate-delegation-and-scoping" / "station.yaml"
    os.utime(manifest, (_time.time() - 2 * 86400,) * 2)
    rep = fabric._factory_context(amara)
    assert f"never run — no task has arrived in the words it listens for ({TRACK})" in rep


# -- check before building ---------------------------------------------------


def test_equivalence_is_hard_to_trip() -> None:
    def st(name: str, purpose: str = "", matches: list[str] | None = None) -> fx.Station:
        return fx.Station(name, purpose, matches or [], ["python3", "run.py"], Path("/x") / name)

    def twin(name: str, purpose: str, matches: list[str], *others: fx.Station):
        hit = fx.find_equivalent(
            name=name, purpose=purpose, matches=matches, candidates=list(others)
        )
        return hit.name if hit else None

    boiler = "Stops the 4× repeat, protects deep-writing blocks, and routes the output to the shared workspace without manual handoffs."
    # The cto's variants.
    assert twin(
        "automate-directive-scope-delegate-and-track",
        "x",
        [],
        st("automate-directive-scope-and-delegate"),
    )
    assert twin(
        "automate-directive-scoping-and-ownership",
        "x",
        [],
        st("automate-directive-scope-and-ownership"),
    )
    assert twin("pr-triage-and-merge-sweep", "x", [], st("pr-merge-sweep"))
    # The same sentence under another name; the same phrase.
    same = "template the manual sweep of outbound commitments, ops routing confirmations, and stale item clearance into a repeatable checklist"
    assert twin("weekly-commitment-closure", same, [], st("dispatch-seam-reconciliation", same))
    assert twin("github", "y", ["claim verifier automate"], st("cto", "z", ["claim verifier"]))
    # Different steps that share a subject, a week, or boilerplate.
    assert not twin("narrative-asset-routing", "x", [], st("narrative-asset-drafting-automation"))
    assert not twin(
        "w48-drift-band-audit-automation",
        "automate the manual sweep of Lara's ±5% Drift Band Audit Template & Tranche-Gating SOP. "
        + boiler,
        ["automate manual sweep", "w48 drift band audit automation"],
        st(
            "w48-sync-readout-automation",
            "automate the manual sweep of the W48 Finance/Growth Sync Pre-Read Pack & Decision-Ready Readout. "
            + boiler,
            ["automate manual sweep", "w48 sync readout automation"],
        ),
    )
    assert not twin("automate-ci-sweep-and-report", "x", [], st("automate-ci-triage-and-fix"))


def test_a_variant_of_her_own_station_is_not_scaffolded(tmp_path: Path) -> None:
    fabric, (cto,) = _team(tmp_path, "cto")
    cto.task_queue.tasks += [
        Task(id="a", description="build_station: automate-directive-scope-and-delegate — code it"),
        Task(
            id="b",
            description="build_station: automate-directive-scope-delegate-and-track — Code the repetitive scoping, delegation, and tracking steps",
        ),
    ]
    assert fabric._scaffold_stations_from_plan(cto) == 1
    floor = cto.directory / "factory"
    assert sorted(p.name for p in floor.iterdir() if p.is_dir()) == [
        "automate-directive-scope-and-delegate"
    ]
    b = next(t for t in cto.task_queue.tasks if t.id == "b")
    assert b.status == "done"
    assert "your station 'automate-directive-scope-and-delegate' (still being built)" in b.outcome
    # One build on the checklist for the station that exists — not two.
    builds = [t for t in cto.task_queue.tasks if "Write and test the code" in t.description]
    assert len(builds) == 1
    assert "was not scaffolded" in fabric._factory_context(cto)
    # The tool path is checked the same way.
    refused = fabric._handle_build_station(
        cto,
        {
            "name": "directive-scope-delegate",
            "purpose": "p",
            "matches": ["scope delegate"],
            "brief": "b",
        },
    )
    assert "already does this" in refused and len(list(floor.glob("*/station.yaml"))) == 1


@pytest.mark.asyncio
async def test_a_colleagues_shared_station_is_adopted_not_rebuilt(tmp_path: Path) -> None:
    model = _Model()
    fabric, (amara, yuki) = _team(tmp_path, "amara", "yuki", model=model)
    d = _station(amara.directory, "automate-reply-to-originator", SHARED_BODY, matches=["x y"])
    _share(d)
    events: list[dict] = []
    fabric.on_event(lambda kind, e: events.append(e))
    refused = fabric._handle_build_station(
        yuki,
        {
            "name": "reply-to-originator",
            "purpose": "Send the outcome back to whoever asked",
            "matches": ["close the loop with the requester"],
            "brief": "b",
        },
    )
    assert "'automate-reply-to-originator', built by amara, already does this" in refused
    assert "Your matching tasks now go through it" in refused
    assert not (yuki.directory / "factory" / "reply-to-originator").exists()
    assert not any("Write and test" in t.description for t in yuki.task_queue.tasks)
    assert [e["existing"] for e in events if e["type"] == "station.duplicate"] == [d.name]
    assert "built by amara, already does this" in fabric._factory_context(yuki)
    # What he was told is true: his own wording now reaches her station,
    # without a word of her manifest changing.
    yuki.task_queue.tasks.append(
        Task(id="t9", description="Close the loop with the requester on PR 12")
    )
    await fabric.cycle("yuki")
    assert yuki.task_queue.tasks[-1].outcome.startswith(
        "[station automate-reply-to-originator by amara]"
    )
    assert yaml.safe_load((d / "station.yaml").read_text())["matches"] == ["x y"]
    assert model.calls == 0


def test_a_colleagues_private_station_is_a_starting_point_not_a_veto(tmp_path: Path) -> None:
    fabric, (amara, yuki) = _team(tmp_path, "amara", "yuki")
    _station(amara.directory, "automate-reply-to-originator", BOUND_BODY, matches=["x y"])
    assert (
        fabric._handle_build_station(
            yuki,
            {
                "name": "reply-to-originator",
                "purpose": "Send the outcome back",
                "matches": ["reply with outcome"],
                "brief": "Draft and queue the reply.",
            },
        )
        == ""
    )
    doc = (yuki.directory / "factory" / "reply-to-originator" / "STATION.md").read_text()
    assert "amara already built a station for this step" in doc
    assert "cannot run for you; read it before writing yours" in doc


# -- match phrases learned from the done ledger -------------------------------


def test_match_phrases_are_learned_from_what_she_did_by_hand(tmp_path: Path) -> None:
    """The cto's ledger held 'Live CI Sweep & Status Report — …' twice when
    she built the station; its manifest got 'collect status innovology'."""
    fabric, (cto,) = _team(tmp_path, "cto")
    live = "Live CI Sweep & Status Report — innovology.io, sailcoach, cortiva-hq → maren@workforce.innovology.io"
    _done_rows(cto, live, 2)
    _done_rows(cto, "Reply to <originator> with the outcome", 15)
    _done_rows(cto, "Scope what's being asked + who owns the work", 35)
    _done_rows(cto, live, 3, outcome="[station x] already on the floor")
    cto.task_queue.tasks.append(
        Task(
            id="a",
            description=(
                "build_station: automate-ci-sweep-and-report — collect CI status from "
                "innovology.io, sailcoach, cortiva-hq; compile into a concise status report; "
                "queue for delivery to Maren and the founder."
            ),
        )
    )
    assert fabric._scaffold_stations_from_plan(cto) == 1
    d = cto.directory / "factory" / "automate-ci-sweep-and-report"
    m = yaml.safe_load((d / "station.yaml").read_text())
    assert m["matches"] == [
        "automate ci sweep and report",
        "live sweep status report innovology sailcoach",
    ]
    assert "live sweep status report innovology sailcoach" in (d / "STATION.md").read_text()
    st = fx._load_manifest(d)
    assert st.matches_task(live.replace("Live", "[HIGH] Live"))


def test_a_step_the_station_only_resembles_is_not_learned(tmp_path: Path) -> None:
    """yuki's PR sweep 'replies to comments and logs outcomes' — two thirds of
    'reply originator outcome', and not that step. Once is not a recurrence."""
    fabric, (yuki,) = _team(tmp_path, "yuki")
    _done_rows(yuki, "Reply to <originator> with the outcome", 11)
    _done_rows(yuki, "Review open PR issue threads and merge when green", 1)
    purpose = (
        "automate review of open PR/issue threads on my repos: fetch status, merge if "
        "approved+green, reply to comments, close validation loops, log outcomes"
    )
    assert fabric._learned_match_phrases(yuki, "github-feedback-sweep", purpose) == []
