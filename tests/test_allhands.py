"""The all-hands: one position, heard and answered by everyone working, each
from their own role and memory, and ending in work each person chose."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortiva.core import allhands as ah
from tests.test_wake_recovery import _fabric, _Model


def test_the_speaking_order_turns_each_day() -> None:
    floor = ["dee", "abe", "cal"]
    one, two = ah.order(floor, "2026-03-02"), ah.order(floor, "2026-03-03")
    assert sorted(one) == sorted(two) == ["abe", "cal", "dee"]
    assert two == one[1:] + one[:1]


def test_what_someone_takes_is_read_from_one_line_each() -> None:
    assert ah.takes("TAKE: Build the counter\n- **TAKE**: Write it up\nTAKE: a third") == [
        "Build the counter",
        "Write it up",
    ]
    assert ah.takes("NONE — nothing of mine") == []
    assert ah.takes("TAKE: NONE") == []


def test_one_piece_of_work_has_one_owner() -> None:
    taken = [{"agent": "abe", "item": "Provision the stock counter template update by Thursday"}]
    assert ah.already_taken("Implement the stock counter template update", taken) == "abe"
    assert ah.already_taken("Write to the three largest customers", taken) == ""


def test_it_is_due_once_its_time_has_passed_and_only_once_a_day(tmp_path: Path) -> None:
    assert ah.due(tmp_path) is False  # no time set
    assert ah.schedule(tmp_path, "9am")[0] is False
    assert ah.schedule(tmp_path, "09:30")[0]
    early = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    late = datetime(2026, 3, 2, 9, 31, tzinfo=UTC)
    assert ah.due(tmp_path, early) is False and ah.due(tmp_path, late) is True
    assert ah.next_id(tmp_path, "2026-03-02") == "2026-03-02"
    ah._write(ah.record_path(tmp_path, "2026-03-02"), {"id": "2026-03-02"})
    assert ah.due(tmp_path, late) is False
    assert ah.next_id(tmp_path, "2026-03-02") == "2026-03-02-2"


@pytest.mark.asyncio
async def test_each_person_thinks_from_their_own_and_hears_those_before(tmp_path: Path) -> None:
    model = _Model()
    f = _fabric(tmp_path, model)
    for aid in ("head", "abe", "cal"):
        agent = f.register_agent(aid)
        (agent.directory / "identity").mkdir(parents=True, exist_ok=True)
        (agent.directory / "identity" / "responsibilities.md").write_text(f"ROLE-OF-{aid}")
        await f.wake(aid)
    f._load_directory_cards = lambda: [
        {"id": "head", "name": "Hana", "role": "Head"},
        {"id": "abe", "name": "Abe", "role": "Stores", "reports_to": "head"},
        {"id": "cal", "name": "Cal", "role": "Desk", "reports_to": "abe"},
    ]
    assert f._allhands_people() == ("head", ["abe", "cal"])
    calls: list[tuple[str, str, str, dict]] = []

    async def _think(agent_id, context, prompt, **kw):
        calls.append((agent_id, context, prompt, kw))
        r = await _Model().think(agent_id, context, prompt, **kw)
        if prompt.startswith("You are opening"):
            r.content = "OPENING: we are behind on orders."
        elif "has opened the all-hands" in prompt:
            r.content = f"CHALLENGE-FROM-{agent_id}"
        elif "has spoken" in prompt:
            r.content = "ANSWER: accepted."
        elif prompt.startswith("The floor has finished"):
            r.content = "CLOSING: we fix the counter first."
        else:
            r.content = "NONE" if agent_id == "head" else f"TAKE: Job for {agent_id}"
        return r

    model.think = _think
    stored: list[tuple[str, list[str]]] = []
    orig_store = f.memory.store

    async def _store(**kw):
        stored.append((kw["agent_id"], kw["tags"]))
        return await orig_store(**kw)

    f.memory.store = _store
    mid = "2026-03-02"
    await f._run_allhands(mid)

    record = ah.load(f.agents_dir, mid)
    speakers = record["floor"]
    assert record["convener"] == "head" and sorted(speakers) == ["abe", "cal"]
    assert record["opening"].startswith("OPENING") and record["closing"].startswith("CLOSING")
    assert [t["said"] for t in record["turns"]] == [f"CHALLENGE-FROM-{a}" for a in speakers]
    assert all(t["answer"] == "ANSWER: accepted." for t in record["turns"])

    floor_calls = {c[0]: c for c in calls if "has opened the all-hands" in c[2]}
    first, second = speakers
    # Each thinks from their own role, with deliberation and room to answer.
    for aid in speakers:
        assert f"ROLE-OF-{aid}" in floor_calls[aid][1]
        assert "ROLE-OF-head" not in floor_calls[aid][1]
        assert floor_calls[aid][3]["metadata"]["quick"] is False
        assert floor_calls[aid][3]["max_tokens"] > 4096
    # The second speaker has heard the first, and the answer the first got.
    assert f"CHALLENGE-FROM-{first}" in floor_calls[second][1]
    assert "ANSWER: accepted." in floor_calls[second][1]
    assert f"CHALLENGE-FROM-{second}" not in floor_calls[first][1]

    # Everyone records the opening, each turn and the close.
    for aid in ("head", "abe", "cal"):
        kinds = [tags[1] for who, tags in stored if who == aid and tags[0] == "allhands"]
        assert kinds == ["briefing", "floor", "floor", "closing"]

    # What leaves the room is each person's own call, placed on their list.
    assert record["taken"] == [{"agent": a, "item": f"Job for {a}"} for a in speakers]
    for aid in speakers:
        last = f.agents[aid].task_queue.tasks[-1]
        assert last.description == f"Job for {aid}" and last.status == "pending"
    assert "ended_at" in record
    assert "Abe (Stores) said" in (f.agents_dir / ".allhands" / f"{mid}.md").read_text()


@pytest.mark.asyncio
async def test_someone_who_says_nothing_gets_no_answer_and_the_meeting_goes_on(
    tmp_path: Path,
) -> None:
    model = _Model()
    f = _fabric(tmp_path, model)
    for aid in ("head", "abe"):
        f.register_agent(aid)
        await f.wake(aid)
    f._load_directory_cards = lambda: [{"id": "head"}, {"id": "abe", "reports_to": "head"}]
    asked: list[str] = []

    async def _think(agent_id, context, prompt, **kw):
        asked.append(prompt[:20])
        r = await _Model().think(agent_id, context, prompt, **kw)
        r.content = "" if "has opened the all-hands" in prompt else "Something said."
        return r

    model.think = _think
    await f._run_allhands("2026-03-02")
    record = ah.load(f.agents_dir, "2026-03-02")
    assert record["turns"] == [{"agent": "abe", "said": "", "recalled": 0}]
    assert record["closing"] == "Something said." and "ended_at" in record
    assert not any("has spoken" in a for a in asked)
