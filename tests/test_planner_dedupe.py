"""Tests for planner seed-time dedupe (spec: planner-dedupe-spec).

Covers:
- _normalise_intent: priority stripping, markdown, dates/SHAs, 12-word limit
- _filter_duplicate_tasks: intra-queue, done.jsonl, station.yaml
- set_plan / update_plan: duplicates dropped and journal-logged
- complete_task: writes done.jsonl entry
- Replay: a plan containing repeated items collapses to one each
"""

from __future__ import annotations

import json
from pathlib import Path

from cortiva.core.agent import (
    Agent,
    _is_station_ready,
    _load_done_intents,
    _normalise_intent,
    _station_name_from_description,
)

# ---------------------------------------------------------------------------
# _normalise_intent
# ---------------------------------------------------------------------------


class TestNormaliseIntent:
    def test_strips_priority_markers(self) -> None:
        assert _normalise_intent("**[CRITICAL]** Fix CI pipeline") == "fix ci pipeline"
        assert _normalise_intent("**[HIGH]** Deploy hotfix") == "deploy hotfix"

    def test_strips_markdown_links(self) -> None:
        result = _normalise_intent("[Fix CI](https://github.com/org/repo/pull/42)")
        assert result == "fix ci"

    def test_strips_dates(self) -> None:
        result = _normalise_intent("Fix lead-capture-verification 2026-11-17")
        assert "2026" not in result
        assert "11" not in result or "fix" in result  # content words preserved

    def test_strips_git_shas(self) -> None:
        result = _normalise_intent("Fix CI Run Counter workflow failure on main (0f8420f)")
        assert "0f8420f" not in result

    def test_strips_pr_refs(self) -> None:
        result = _normalise_intent("Merge PR #364 to unblock lead notification")
        assert "#364" not in result
        assert "364" not in result

    def test_twelve_word_limit(self) -> None:
        words = " ".join([f"word{i}" for i in range(20)])
        result = _normalise_intent(words)
        assert len(result.split()) == 12

    def test_empty(self) -> None:
        assert _normalise_intent("") == ""
        assert _normalise_intent("   ") == ""

    def test_same_intent_different_priority(self) -> None:
        a = _normalise_intent("**[CRITICAL]** Fix CI Run Counter workflow failure on main")
        b = _normalise_intent("**[HIGH]** Fix CI Run Counter workflow failure on main")
        assert a == b

    def test_same_intent_different_sha(self) -> None:
        a = _normalise_intent("Fix CI Run Counter workflow failure on main (0f8420f)")
        b = _normalise_intent("Fix CI Run Counter workflow failure on main (1a2b3c4)")
        assert a == b


# ---------------------------------------------------------------------------
# _station_name_from_description
# ---------------------------------------------------------------------------


class TestStationName:
    def test_build_x_station(self) -> None:
        assert (
            _station_name_from_description("Build automate-ci-triage station")
            == "automate-ci-triage"
        )

    def test_build_the_x_station(self) -> None:
        assert _station_name_from_description("Build the scope station for delegation") == "scope"

    def test_no_match_no_build(self) -> None:
        assert _station_name_from_description("Fix station config bug") is None

    def test_no_match_no_station(self) -> None:
        assert _station_name_from_description("Build a new feature") is None


# ---------------------------------------------------------------------------
# _is_station_ready
# ---------------------------------------------------------------------------


class TestIsStationReady:
    def test_ready_true(self, tmp_path: Path) -> None:
        (tmp_path / "factory" / "my-station").mkdir(parents=True)
        (tmp_path / "factory" / "my-station" / "station.yaml").write_text("ready: true\n")
        assert _is_station_ready("my-station", tmp_path) is True

    def test_ready_false(self, tmp_path: Path) -> None:
        (tmp_path / "factory" / "my-station").mkdir(parents=True)
        (tmp_path / "factory" / "my-station" / "station.yaml").write_text("ready: false\n")
        assert _is_station_ready("my-station", tmp_path) is False

    def test_missing_yaml(self, tmp_path: Path) -> None:
        assert _is_station_ready("nonexistent-station", tmp_path) is False


# ---------------------------------------------------------------------------
# _load_done_intents
# ---------------------------------------------------------------------------


class TestLoadDoneIntents:
    def test_reads_recent_intents(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        record = json.dumps(
            {
                "intent": "fix ci pipeline",
                "description": "Fix CI pipeline",
                "at": datetime.now(UTC).isoformat(),
            }
        )
        (tmp_path / "done.jsonl").write_text(record + "\n")
        intents = _load_done_intents(tmp_path)
        assert "fix ci pipeline" in intents

    def test_ignores_old_intents(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime, timedelta

        old = datetime.now(UTC) - timedelta(days=10)
        record = json.dumps(
            {
                "intent": "old task intent",
                "at": old.isoformat(),
            }
        )
        (tmp_path / "done.jsonl").write_text(record + "\n")
        intents = _load_done_intents(tmp_path, days=7)
        assert "old task intent" not in intents

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _load_done_intents(tmp_path) == set()


# ---------------------------------------------------------------------------
# Agent._filter_duplicate_tasks
# ---------------------------------------------------------------------------


class TestFilterDuplicateTasks:
    def _make_agent(self, tmp_path: Path) -> Agent:
        return Agent(id="test-agent", directory=tmp_path)

    def test_intra_queue_dedupe(self, tmp_path: Path) -> None:
        agent = self._make_agent(tmp_path)
        plan = (
            "- Fix CI Run Counter workflow failure on main (0f8420f)\n"
            "- Fix CI Run Counter workflow failure on main (1a2b3c4)\n"
            "- Deploy hotfix\n"
        )
        tq = agent.set_plan(plan)
        descriptions = [t.description for t in tq.tasks]
        # Only one "Fix CI Run Counter" should survive
        ci_tasks = [d for d in descriptions if "Fix CI Run Counter" in d]
        assert len(ci_tasks) == 1

    def test_done_jsonl_dedupe(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        agent = self._make_agent(tmp_path)
        intent = _normalise_intent("Fix CI pipeline failures on PR")
        record = json.dumps({"intent": intent, "at": datetime.now(UTC).isoformat()})
        (tmp_path / "done.jsonl").write_text(record + "\n")

        plan = "- Fix CI pipeline failures on PR\n- Deploy hotfix\n"
        tq = agent.set_plan(plan)
        descriptions = [t.description for t in tq.tasks]
        assert "Fix CI pipeline failures on PR" not in descriptions
        assert "Deploy hotfix" in descriptions

    def test_station_ready_dedupe(self, tmp_path: Path) -> None:
        agent = self._make_agent(tmp_path)
        (tmp_path / "factory" / "scope").mkdir(parents=True)
        (tmp_path / "factory" / "scope" / "station.yaml").write_text("ready: true\n")

        plan = "- Build scope station for delegation workflow\n- Deploy hotfix\n"
        tq = agent.set_plan(plan)
        descriptions = [t.description for t in tq.tasks]
        assert not any("scope station" in d for d in descriptions)
        assert "Deploy hotfix" in descriptions

    def test_done_tasks_dedupe_too(self, tmp_path: Path) -> None:
        agent = self._make_agent(tmp_path)
        plan = (
            "- [x] Fix CI Run Counter workflow failure on main (0f8420f)\n"
            "- [x] Fix CI Run Counter workflow failure on main (1a2b3c4)\n"
        )
        tq = agent.set_plan(plan)
        # Both have the same normalised intent; only the first is kept.
        assert len(tq.tasks) == 1
        assert tq.tasks[0].status == "done"

    def test_drops_logged_to_journal(self, tmp_path: Path) -> None:
        agent = self._make_agent(tmp_path)
        plan = "- Fix CI pipeline\n- Fix CI pipeline\n"
        agent.set_plan(plan)
        journal = agent.journal_path()
        assert journal.exists()
        content = journal.read_text()
        assert "dropped" in content
        assert "intra-queue duplicate" in content


# ---------------------------------------------------------------------------
# complete_task writes done.jsonl
# ---------------------------------------------------------------------------


class TestCompleteTaskWritesDone:
    def test_writes_done_jsonl(self, tmp_path: Path) -> None:
        agent = Agent(id="test-agent", directory=tmp_path)
        plan = "- Fix CI pipeline\n"
        tq = agent.set_plan(plan)
        task = tq.tasks[0]
        agent.complete_task(task, "CI green")
        done_path = tmp_path / "done.jsonl"
        assert done_path.exists()
        record = json.loads(done_path.read_text().strip())
        assert record["intent"] == _normalise_intent("Fix CI pipeline")
        assert "at" in record

    def test_acknowledged_does_not_write_done(self, tmp_path: Path) -> None:
        from cortiva.core.agent import Task, TaskQueue

        agent = Agent(id="test-agent", directory=tmp_path)
        parent = Task(id="p1", description="Parent task")
        child = Task(id="c1", description="Child task", status="pending")
        parent.subtasks.append(child)
        agent.task_queue = TaskQueue(tasks=[parent])
        agent.complete_task(parent, "partial")
        assert parent.status == "acknowledged"
        done_path = tmp_path / "done.jsonl"
        assert not done_path.exists()


# ---------------------------------------------------------------------------
# Replay test: amara-style repeated plan collapses duplicates
# ---------------------------------------------------------------------------


class TestReplayDedupe:
    """Replay a plan modelled on the repeated-item pattern seen in amara's
    plan.md (20× 'Fix CI Run Counter', 7× 'Fix CI failures on PR #364').
    After set_plan each intent should appear exactly once.
    """

    REPLAY_PLAN = """\
- [x] **[CRITICAL]** Fix CI Run Counter workflow failure on main (0f8420f)
- [x] **[CRITICAL]** Fix CI Run Counter workflow failure on main (0f8420f)
- [x] **[CRITICAL]** Fix CI Run Counter workflow failure on main (0f8420f)
- **[CRITICAL]** Fix CI Run Counter workflow failure on main (1a2b3c4)
- **[CRITICAL]** Fix CI Run Counter workflow failure on main (2b3c4d5)
- [x] **[CRITICAL]** Fix CI failures on PR #364 to unblock lead notification delivery
- [x] **[CRITICAL]** Fix CI failures on PR #364 to unblock lead notification delivery
- **[CRITICAL]** Fix CI failures on PR #364 to unblock lead notification delivery
- **[CRITICAL]** Fix CI failures on PR #364 to unblock lead notification delivery
- Verify github claims and confirm ownership
- Verify github claims and confirm ownership
- Deploy hotfix for lead capture
"""

    def test_repeated_items_collapse_to_one(self, tmp_path: Path) -> None:
        agent = Agent(id="test-agent", directory=tmp_path)
        tq = agent.set_plan(self.REPLAY_PLAN)

        all_intents = [_normalise_intent(t.description) for t in tq.tasks]

        # Each distinct intent appears at most once across all tasks (done + pending).
        ci_run_counter = [i for i in all_intents if "fix ci run counter" in i]
        ci_failures = [i for i in all_intents if "fix ci failures" in i]
        verify_github = [i for i in all_intents if "verify github" in i]

        assert len(ci_run_counter) == 1, f"Expected 1 'fix ci run counter', got {ci_run_counter}"
        assert len(ci_failures) == 1, f"Expected 1 'fix ci failures', got {ci_failures}"
        assert len(verify_github) == 1, f"Expected 1 'verify github', got {verify_github}"

        # "verify github claims" has no done version → its surviving task is pending.
        surviving_github = [
            t for t in tq.tasks if "verify github" in _normalise_intent(t.description)
        ]
        assert surviving_github[0].status == "pending"

        # "fix ci run counter" had done versions → its surviving task is done.
        surviving_ci = [
            t for t in tq.tasks if "fix ci run counter" in _normalise_intent(t.description)
        ]
        assert surviving_ci[0].status == "done"
