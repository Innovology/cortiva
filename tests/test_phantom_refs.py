"""Challenge the source at plan time: a task that promises to land, merge or
close a PR/issue that is not hers — or that exists nowhere she can see — is
struck before it buys a session. innovology.io#48 (a dependabot bump) rode
the CTO's plan for a day as 'Land PR #48'."""

from __future__ import annotations

from pathlib import Path

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.adapters.protocols import ConsciousResponse
from cortiva.core import grounding
from cortiva.core.agent import Task, TaskQueue
from cortiva.core.fabric import Fabric


class _Model:
    async def think(self, agent_id, context, prompt, **kwargs):
        return ConsciousResponse(content="ok", tokens_in=1, tokens_out=1, model="m")

    async def reflect(self, agent_id, context, day_summary):
        return ConsciousResponse(content="ok", tokens_in=1, tokens_out=1, model="m")


def _rig(tmp_path: Path, monkeypatch):
    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model()
    )
    agent = fabric.register_agent("cto")
    (agent.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    monkeypatch.setattr(grounding, "github_login", lambda token, timeout=8.0: "samantha-innovology")
    known = {
        ("Innovology/innovology.io", "48"): {"author": "dependabot[bot]", "title": "Bump rouge"},
        ("Innovology/cortiva", "301"): {
            "author": "samantha-innovology",
            "title": "feat: ai-native",
        },
    }
    reads: list[str] = []

    def _read(ref, token, timeout=8.0):
        key = (f"{ref['owner']}/{ref['repo']}", str(ref["number"]))
        reads.append(f"{key[0]}#{key[1]}")
        hit = known.get(key)
        if not hit:
            return {**ref, "status": "unreadable", "reason": "404"}
        return {**ref, "status": "read", "kind": "pull request", "state": "open", **hit}

    monkeypatch.setattr(grounding, "read_github_ref", _read)
    monkeypatch.setattr(
        grounding,
        "_github_get",
        lambda url, token, timeout: [
            {"full_name": "Innovology/innovology.io"},
            {"full_name": "Innovology/cortiva"},
        ],
    )
    return fabric, agent, reads


def test_a_task_naming_someone_elses_or_a_nonexistent_pr_is_struck(tmp_path, monkeypatch) -> None:
    fabric, agent, reads = _rig(tmp_path, monkeypatch)
    agent.task_queue = TaskQueue(
        tasks=[
            Task(id="a", description="Land PR #48 (feat/ai-native: mandatory AI delegation field)"),
            Task(id="b", description="Land PR #301 after review"),
            Task(id="c", description="Close issue #777 once the fix is in"),
            Task(id="d", description="Review the notes on PR #48 for context"),  # no landing verb
            Task(id="e", description="Merge Innovology/cortiva#301 and tag"),
        ]
    )
    assert fabric._strike_phantom_refs(agent) == 2
    by_id = {t.id: t for t in agent.task_queue.tasks}
    assert by_id["a"].status == "skipped"
    assert "no PR/issue #48 of yours in any repo you can see" in by_id["a"].description
    assert "Innovology/innovology.io#48 is dependabot[bot]'s" in by_id["a"].description
    assert by_id["b"].status == "pending"  # hers
    assert (
        by_id["c"].status == "skipped"
        and "no PR/issue #777 in any repo you can see" in by_id["c"].description
    )
    assert by_id["d"].status == "pending"  # about it, not landing it
    assert by_id["e"].status == "pending"  # explicit repo, hers


def test_references_are_read_once_an_hour(tmp_path, monkeypatch) -> None:
    fabric, agent, reads = _rig(tmp_path, monkeypatch)
    agent.task_queue = TaskQueue(tasks=[Task(id="a", description="Land PR #48")])
    fabric._strike_phantom_refs(agent)
    n = len(reads)
    agent.task_queue = TaskQueue(tasks=[Task(id="b", description="Merge PR #48 today")])
    fabric._strike_phantom_refs(agent)
    assert len(reads) == n  # cached: no second read for the same reference


def test_no_token_means_no_verdict(tmp_path, monkeypatch) -> None:
    fabric, agent, _ = _rig(tmp_path, monkeypatch)
    (agent.directory / "credentials.json").unlink()
    agent.task_queue = TaskQueue(tasks=[Task(id="a", description="Land PR #48")])
    assert fabric._strike_phantom_refs(agent) == 0
    assert agent.task_queue.tasks[0].status == "pending"


def test_a_bare_number_that_is_hers_in_another_repo_is_not_struck(tmp_path, monkeypatch) -> None:
    """#301 exists in innovology.io as someone else's AND in cortiva as hers:
    the bare number means hers — keep the task."""
    fabric, agent, _ = _rig(tmp_path, monkeypatch)
    orig = grounding.read_github_ref

    def _read(ref, token, timeout=8.0):
        if (
            f"{ref['owner']}/{ref['repo']}" == "Innovology/innovology.io"
            and str(ref["number"]) == "301"
        ):
            return {
                **ref,
                "status": "read",
                "kind": "pull request",
                "author": "someone-else",
                "title": "x",
            }
        return orig(ref, token, timeout)

    monkeypatch.setattr(grounding, "read_github_ref", _read)
    agent.task_queue = TaskQueue(tasks=[Task(id="a", description="Land PR #301")])
    assert fabric._strike_phantom_refs(agent) == 0


# ---------------------------------------------------------------------------
# Standing orders strike at plan time; a build_station plan line is the verb
# ---------------------------------------------------------------------------


def _order(text: str, scope_type: str, value: str) -> dict:
    return {
        "order_id": "o1",
        "text": text,
        "kind": "prohibition",
        "scope": {"type": scope_type, "value": value},
        "issued_by": {"name": "Alex Browne", "role": "founder"},
        "issued_at": "2026-11-06T18:40:00+00:00",
        "status": "active",
    }


def test_topic_scope_matches_the_class_of_work_not_a_product() -> None:
    from cortiva.core import standing_orders as so

    orders = [
        _order(
            "Stop building red tape — no process artefacts about how we work.",
            "topic",
            "pr template|prompt block|playbook|protocol|decision record|decision-log|governance|dependency matrix|adoption dashboard",
        )
    ]
    assert (
        so.matching_order(orders, "Roll out PR template + agent prompt block to repos") is not None
    )
    assert (
        so.matching_order(orders, "Publish hardened incident post-mortem agent playbook")
        is not None
    )
    assert so.matching_order(orders, "Hand off decision-log PR scope to Willow") is not None
    assert so.matching_order(orders, "Fix the lead form 500 on innovology.io") is None
    assert "any work that is pr template, prompt block" in so.context_block(orders)


def test_a_plan_item_covered_by_a_standing_order_is_struck(tmp_path, monkeypatch) -> None:
    import json

    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model()
    )
    agent = fabric.register_agent("cto")
    (fabric.agents_dir / ".standing_orders.json").write_text(
        json.dumps(
            [_order("Stop building red tape.", "topic", "pr template|playbook|decision record")]
        )
    )
    agent.task_queue = TaskQueue(
        tasks=[
            Task(id="a", description="Roll out PR template + agent prompt block via GitHub API"),
            Task(id="b", description="Fix the lead form 500 on innovology.io"),
        ]
    )
    assert fabric._strike_under_standing_orders(agent) == 1
    by_id = {t.id: t for t in agent.task_queue.tasks}
    assert (
        by_id["a"].status == "skipped"
        and "standing order from Alex Browne" in by_id["a"].description
    )
    assert "Stop building red tape" in by_id["a"].description
    assert by_id["b"].status == "pending"


def test_a_build_station_plan_line_is_the_verb(tmp_path) -> None:
    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model()
    )
    agent = fabric.register_agent("yuki")
    agent.task_queue = TaskQueue(
        tasks=[
            Task(
                id="a",
                description="build_station: github_feedback_sweep — codify the PR review/CI-verify/merge/close loop into a repeatable station",
            ),
            Task(id="b", description="Triage 11 open GitHub feedback threads"),
        ]
    )
    assert fabric._scaffold_stations_from_plan(agent) == 1
    d = agent.directory / "factory" / "github-feedback-sweep"
    assert (d / "STATION.md").is_file() and "codify the PR review" in (d / "STATION.md").read_text()
    by_id = {t.id: t for t in agent.task_queue.tasks}
    assert by_id["a"].status == "done" and "scaffolded" in by_id["a"].outcome
    assert any("station 'github-feedback-sweep'" in t.description for t in agent.task_queue.tasks)
    assert fabric._scaffold_stations_from_plan(agent) == 0  # idempotent
    # The same line re-emitted on a later pass is marked done, not left pending.
    agent.task_queue.tasks.append(
        Task(
            id="c",
            description="build_station: github_feedback_sweep — codify the loop again",
            status="in_progress",  # mid-session when a restart hit
        )
    )
    assert fabric._scaffold_stations_from_plan(agent) == 0
    c = next(x for x in agent.task_queue.tasks if x.id == "c")
    assert c.status == "done" and "already on the floor" in c.outcome


def test_a_single_word_station_name_is_not_a_match_phrase(tmp_path) -> None:
    """'build_station: Founder Steer: Live CI Dashboard — automate delegation
    tracking' (Maren, 20:42) named the station 'founder'; that word must not
    become a match phrase or every founder task routes through it once ready."""
    import yaml

    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model()
    )
    agent = fabric.register_agent("ceo")
    agent.task_queue = TaskQueue(
        tasks=[
            Task(
                id="a",
                description="build_station: Founder Steer: Live CI Dashboard — Automate delegation, tracking, and outcome reporting",
            )
        ]
    )
    assert fabric._scaffold_stations_from_plan(agent) == 1
    m = yaml.safe_load((agent.directory / "factory" / "founder" / "station.yaml").read_text())
    assert "founder" not in m["matches"]
    assert m["matches"] and all(len(x.split()) >= 2 for x in m["matches"])


def test_lines_that_are_not_tasks_are_struck(tmp_path) -> None:
    """A directory card echoed from context, a placeholder, a stub — none of
    these should buy an ask or a session."""
    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model()
    )
    agent = fabric.register_agent("amara")
    agent.task_queue = TaskQueue(
        tasks=[
            Task(id="a", description="**ar-scheduler**  (AR Scheduler)"),
            Task(id="b", description="**ceo**  (Chief Executive Officer)"),
            Task(
                id="c", description="[Manager Delegation] None currently assigned by Samantha (CTO)"
            ),
            Task(id="d", description="Review"),
            Task(
                id="e",
                description="Fix the lead form 500 on innovology.io and verify the pipeline screen",
            ),
            Task(id="f", description="**[HIGH]** Land PR #301 after review"),
        ]
    )
    assert fabric._strike_non_tasks(agent) == 3
    by = {t.id: t for t in agent.task_queue.tasks}
    assert by["a"].status == by["b"].status == by["c"].status == "skipped"
    assert "directory entry" in by["a"].description and "placeholder" in by["c"].description
    assert by["d"].status == "pending"  # short, but a task
    assert by["e"].status == "pending" and by["f"].status == "pending"


def test_a_struck_line_the_model_echoes_back_stays_struck(tmp_path, monkeypatch) -> None:
    """amara's plan, 2026-11-06 23:25→23:56: a directory card for Jessica was
    struck under the MarketMesh order, echoed back by the model's replan as
    '- [ ] … *(struck: …)* *(skipped)*', parsed as pending, struck again and
    logged again — six markers by midnight. A marked line is settled."""
    import json

    from cortiva.core.agent import _parse_plan, _task_key

    fabric = Fabric(
        agents_dir=tmp_path / "agents", memory=InMemoryAdapter(), consciousness=_Model()
    )
    agent = fabric.register_agent("amara")
    (fabric.agents_dir / ".standing_orders.json").write_text(
        json.dumps([_order("All work on MarketMesh is halted.", "topic", "marketmesh")])
    )
    agent.task_queue = TaskQueue(
        tasks=[Task(id="a", description="**jessica**  (Product Owner — MarketMesh)")]
    )
    assert fabric._strike_under_standing_orders(agent) == 1
    fabric._write_plan(agent)
    rendered = agent.read_identity("plan")
    assert rendered.count("*(struck:") == 1 and "*(skipped)*" in rendered
    # The model echoes the rendered plan back verbatim.
    agent.update_plan(rendered)
    (t,) = agent.task_queue.tasks
    assert t.status == "skipped"
    assert t.description.count("*(struck:") == 1 and "*(skipped)*" not in t.description
    # Every strike helper leaves it alone and counts nothing.
    assert fabric._strike_under_standing_orders(agent) == 0
    assert fabric._strike_non_tasks(agent) == 0
    assert t.description.count("*(struck:") == 1
    fabric._write_plan(agent)
    assert agent.read_identity("plan").count("*(struck:") == 1
    # A task's identity ignores the markers.
    assert _task_key("Fix the form *(struck: x)* *(skipped)*") == _task_key("Fix the form")
    # The bare parser, which update_plan uses, reads a marked line as skipped.
    q = _parse_plan("- [ ] Ship it *(skipped)*\n- [ ] Do it\n")
    assert [x.status for x in q.tasks] == ["skipped", "pending"]
    assert q.tasks[0].description == "Ship it"
