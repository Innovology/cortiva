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
    assert (
        by_id["a"].status == "skipped"
        and "dependabot[bot]'s 'Bump rouge', not yours" in by_id["a"].description
    )
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
