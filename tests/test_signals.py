"""A small model answers beside the large one; both are recorded; nothing
acts on the small one."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cortiva.core import signals as sg
from tests.test_wake_recovery import _fabric, _Model


def test_with_no_model_named_for_a_job_nothing_is_asked_or_written(tmp_path: Path) -> None:
    sg.shadow(tmp_path, "triage", "abe", "sys", "user", "QUEUE: x", 5.0)
    assert not (tmp_path / sg.SHADOW).exists()


def test_both_answers_and_both_times_are_recorded(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / sg.CONFIG).write_text(
        json.dumps({"triage": {"base_url": "http://h:1/v1", "model": "small-one"}})
    )
    monkeypatch.setattr(sg, "ask", lambda entry, system, user, **kw: ("NONE", 0.2))
    sg.shadow(tmp_path, "triage", "abe", "sys", "user", "QUEUE: Review the change", 5.0)
    row = json.loads((tmp_path / sg.SHADOW).read_text().splitlines()[0])
    assert (row["main"], row["small"]) == ("QUEUE: Review the change", "NONE")
    assert (row["main_s"], row["small_s"], row["model"]) == (5.0, 0.2, "small-one")


def test_a_small_model_that_cannot_be_reached_is_recorded_and_nothing_breaks(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / sg.CONFIG).write_text(
        json.dumps({"triage": {"base_url": "http://h:1/v1", "model": "small-one"}})
    )

    def _down(*a, **kw):
        raise OSError("refused")

    monkeypatch.setattr(sg, "ask", _down)
    sg.shadow(tmp_path, "triage", "abe", "sys", "user", "NONE", 5.0)
    row = json.loads((tmp_path / sg.SHADOW).read_text().splitlines()[0])
    assert "refused" in row["error"] and "small" not in row


@pytest.mark.asyncio
async def test_an_arrival_is_shadowed_and_the_large_models_answer_is_what_is_acted_on(
    tmp_path: Path, monkeypatch
) -> None:
    model = _Model()
    f = _fabric(tmp_path, model)
    agent = f.register_agent("cto")
    await f.wake("cto")
    (f.agents_dir / sg.CONFIG).write_text(
        json.dumps({"triage": {"base_url": "http://h:1/v1", "model": "small-one"}})
    )
    monkeypatch.setattr(sg, "ask", lambda entry, system, user, **kw: ("NONE", 0.1))

    async def _think(agent_id, context, prompt, **kw):
        r = await _Model().think(agent_id, context, prompt, **kw)
        r.content = "QUEUE: Review the small change"
        return r

    model.think = _think
    await f._replan(agent, ["a colleague asks for a review"])
    await asyncio.gather(*f._shadow_tasks)
    row = json.loads((f.agents_dir / sg.SHADOW).read_text().splitlines()[0])
    assert row["job"] == "triage" and row["small"] == "NONE" and row["agent"] == "cto"
    assert agent.task_queue.tasks[-1].description == "Review the small change"
