"""Tests for the OKR / Goals system."""

import uuid
from pathlib import Path

import pytest

from cortiva.core.goals import GoalManager, KeyResult, Objective


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kr(desc: str = "Ship feature", target: float = 10.0, **kw) -> KeyResult:
    """Shorthand to build a KeyResult with a generated id."""
    return KeyResult(
        id=kw.pop("id", uuid.uuid4().hex[:8]),
        description=desc,
        target_value=target,
        **kw,
    )


# ---------------------------------------------------------------------------
# KeyResult / Objective serialisation
# ---------------------------------------------------------------------------


class TestKeyResultSerde:
    def test_round_trip(self):
        kr = KeyResult(
            id="kr1",
            description="Close 5 deals",
            target_value=5.0,
            current_value=2.0,
            unit="deals",
            agent_id="sales-01",
        )
        assert KeyResult.from_dict(kr.to_dict()) == kr

    def test_defaults(self):
        kr = KeyResult.from_dict({"id": "x", "description": "d", "target_value": 1.0})
        assert kr.current_value == 0.0
        assert kr.unit == ""
        assert kr.agent_id is None


class TestObjectiveSerde:
    def test_round_trip(self):
        obj = Objective(
            id="obj1",
            title="Grow revenue",
            description="Increase ARR",
            key_results=[
                KeyResult(
                    id="kr1",
                    description="Close deals",
                    target_value=10.0,
                    current_value=3.0,
                    unit="deals",
                    agent_id="sales-01",
                ),
            ],
            department="sales",
            owner="sales-lead",
            quarter="2026-Q1",
            status="active",
        )
        assert Objective.from_dict(obj.to_dict()) == obj

    def test_defaults(self):
        obj = Objective.from_dict({"id": "x", "title": "T"})
        assert obj.description == ""
        assert obj.key_results == []
        assert obj.department is None
        assert obj.owner == ""
        assert obj.quarter == ""
        assert obj.status == "active"


# ---------------------------------------------------------------------------
# GoalManager basics
# ---------------------------------------------------------------------------


class TestGoalManagerCreate:
    def test_create_objective(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        obj = gm.create_objective(
            title="Launch v2",
            description="Ship the next major version",
            key_results=[_kr("Feature A", 1.0), _kr("Feature B", 1.0)],
            owner="eng-lead",
            department="engineering",
            quarter="2026-Q2",
        )
        assert obj.title == "Launch v2"
        assert obj.owner == "eng-lead"
        assert len(obj.key_results) == 2
        assert obj.status == "active"

    def test_create_persists(self, tmp_path: Path):
        data_dir = tmp_path / ".goals"
        gm = GoalManager(data_dir)
        gm.create_objective(
            title="O1",
            description="D1",
            key_results=[],
            owner="a1",
            quarter="2026-Q1",
        )
        # Reload from disk
        gm2 = GoalManager(data_dir)
        objs = gm2.get_objectives()
        assert len(objs) == 1
        assert objs[0].title == "O1"


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class TestGetObjectives:
    @pytest.fixture()
    def gm(self, tmp_path: Path) -> GoalManager:
        gm = GoalManager(tmp_path / ".goals")
        gm.create_objective(
            title="Eng goal",
            description="",
            key_results=[_kr("KR", 5.0, agent_id="dev-01")],
            owner="eng-lead",
            department="engineering",
            quarter="2026-Q1",
        )
        gm.create_objective(
            title="Sales goal",
            description="",
            key_results=[_kr("KR", 10.0, agent_id="sales-01")],
            owner="sales-lead",
            department="sales",
            quarter="2026-Q1",
        )
        gm.create_objective(
            title="Q2 goal",
            description="",
            key_results=[],
            owner="eng-lead",
            department="engineering",
            quarter="2026-Q2",
        )
        return gm

    def test_no_filter(self, gm: GoalManager):
        assert len(gm.get_objectives()) == 3

    def test_filter_by_quarter(self, gm: GoalManager):
        assert len(gm.get_objectives(quarter="2026-Q1")) == 2

    def test_filter_by_department(self, gm: GoalManager):
        assert len(gm.get_objectives(department="sales")) == 1

    def test_filter_by_agent_owner(self, gm: GoalManager):
        objs = gm.get_objectives(agent_id="eng-lead")
        assert len(objs) == 2  # owns Eng goal + Q2 goal

    def test_filter_by_agent_kr(self, gm: GoalManager):
        objs = gm.get_objectives(agent_id="dev-01")
        assert len(objs) == 1
        assert objs[0].title == "Eng goal"

    def test_combined_filters(self, gm: GoalManager):
        objs = gm.get_objectives(quarter="2026-Q1", department="engineering")
        assert len(objs) == 1
        assert objs[0].title == "Eng goal"


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


class TestProgress:
    def test_no_key_results(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        obj = gm.create_objective(
            title="Empty", description="", key_results=[], owner="a"
        )
        assert gm.progress(obj.id) == 0.0

    def test_partial_progress(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        obj = gm.create_objective(
            title="P",
            description="",
            key_results=[
                _kr("A", 10.0, current_value=5.0),
                _kr("B", 20.0, current_value=20.0),
            ],
            owner="a",
        )
        # (5/10 + 20/20) / 2 = (0.5 + 1.0) / 2 = 0.75
        assert gm.progress(obj.id) == pytest.approx(0.75)

    def test_progress_capped_at_one(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        obj = gm.create_objective(
            title="Over",
            description="",
            key_results=[_kr("A", 5.0, current_value=100.0)],
            owner="a",
        )
        assert gm.progress(obj.id) == pytest.approx(1.0)

    def test_zero_target(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        obj = gm.create_objective(
            title="Zero",
            description="",
            key_results=[_kr("A", 0.0, current_value=0.0)],
            owner="a",
        )
        # target_value == 0 => ratio 1.0 (treated as already met)
        assert gm.progress(obj.id) == pytest.approx(1.0)

    def test_unknown_objective_raises(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        with pytest.raises(KeyError):
            gm.progress("nonexistent")


# ---------------------------------------------------------------------------
# Update key result
# ---------------------------------------------------------------------------


class TestUpdateKeyResult:
    def test_update_value(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        kr = _kr("Ship it", 10.0, id="kr-fixed")
        obj = gm.create_objective(
            title="T", description="", key_results=[kr], owner="a"
        )
        gm.update_key_result(obj.id, "kr-fixed", 7.0)
        updated = gm.get_objectives()[0].key_results[0]
        assert updated.current_value == 7.0

    def test_update_persists(self, tmp_path: Path):
        data_dir = tmp_path / ".goals"
        gm = GoalManager(data_dir)
        kr = _kr("Ship it", 10.0, id="kr-fixed")
        obj = gm.create_objective(
            title="T", description="", key_results=[kr], owner="a"
        )
        gm.update_key_result(obj.id, "kr-fixed", 7.0)
        gm2 = GoalManager(data_dir)
        assert gm2.get_objectives()[0].key_results[0].current_value == 7.0

    def test_bad_objective_raises(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        with pytest.raises(KeyError, match="Objective"):
            gm.update_key_result("bad", "kr", 1.0)

    def test_bad_kr_raises(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        obj = gm.create_objective(
            title="T", description="", key_results=[_kr("A", 1.0)], owner="a"
        )
        with pytest.raises(KeyError, match="KeyResult"):
            gm.update_key_result(obj.id, "nonexistent-kr", 1.0)


# ---------------------------------------------------------------------------
# agent_goals_context
# ---------------------------------------------------------------------------


class TestAgentGoalsContext:
    def test_empty_when_no_goals(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        assert gm.agent_goals_context("nobody") == ""

    def test_renders_markdown(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        gm.create_objective(
            title="Improve uptime",
            description="Reach 99.9% SLA",
            key_results=[
                _kr("Reduce P1 incidents", 5.0, current_value=2.0, unit="incidents",
                     agent_id="sre-01"),
            ],
            owner="sre-01",
            quarter="2026-Q1",
        )
        md = gm.agent_goals_context("sre-01")
        assert "## My OKR Goals" in md
        assert "Improve uptime" in md
        assert "2026-Q1" in md
        assert "2.0/5.0 incidents" in md
        assert "@sre-01" in md

    def test_excludes_other_agents(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        gm.create_objective(
            title="Other team goal",
            description="",
            key_results=[_kr("X", 1.0, agent_id="other")],
            owner="other",
            quarter="2026-Q1",
        )
        assert gm.agent_goals_context("sre-01") == ""


# ---------------------------------------------------------------------------
# Persistence edge cases
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_load_empty_dir(self, tmp_path: Path):
        gm = GoalManager(tmp_path / ".goals")
        assert gm.get_objectives() == []

    def test_creates_data_dir(self, tmp_path: Path):
        data_dir = tmp_path / "deep" / "nested" / ".goals"
        gm = GoalManager(data_dir)
        gm.create_objective(title="T", description="", key_results=[], owner="a")
        assert data_dir.exists()
        assert (data_dir / "objectives.json").exists()

    def test_multiple_objectives_persist(self, tmp_path: Path):
        data_dir = tmp_path / ".goals"
        gm = GoalManager(data_dir)
        for i in range(5):
            gm.create_objective(
                title=f"Obj {i}", description="", key_results=[], owner="a"
            )
        gm2 = GoalManager(data_dir)
        assert len(gm2.get_objectives()) == 5
