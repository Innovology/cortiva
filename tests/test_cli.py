"""Tests for cortiva.cli.main and cortiva.cli.output."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _mock_ipc_client(*, is_running: bool = True, resp: dict | None = None) -> MagicMock:
    """Create a mock FabricClient with preset return values."""
    client = MagicMock()
    client.is_daemon_running.return_value = is_running
    client.send_sync.return_value = resp
    return client


@contextmanager
def _plain_output() -> Iterator[None]:
    """Force the plain-text (no-rich) output path."""
    with patch("cortiva.cli.output._RICH_AVAILABLE", False):
        with patch("cortiva.cli.output.console", None):
            yield


# ---------------------------------------------------------------------------
# Output helpers (cortiva.cli.output)
# ---------------------------------------------------------------------------


class TestStateBadge:
    def test_known_state(self) -> None:
        from cortiva.cli.output import state_badge

        result = state_badge("executing")
        text = str(result)
        assert "executing" in text

    def test_unknown_state(self) -> None:
        from cortiva.cli.output import state_badge

        result = state_badge("bogus")
        text = str(result)
        assert "?" in text
        assert "bogus" in text


class TestProgressBar:
    def test_basic(self) -> None:
        from cortiva.cli.output import progress_bar

        result = str(progress_bar(3, 7, width=10))
        assert "3/7" in result

    def test_zero_total(self) -> None:
        from cortiva.cli.output import progress_bar

        assert progress_bar(0, 0) == "\u2014"

    def test_full(self) -> None:
        from cortiva.cli.output import progress_bar

        result = str(progress_bar(10, 10, width=5))
        assert "10/10" in result


class TestBudgetDisplay:
    def test_low_usage(self) -> None:
        from cortiva.cli.output import budget_display

        result = str(budget_display(5, 50))
        assert "5/50" in result

    def test_high_usage(self) -> None:
        from cortiva.cli.output import budget_display

        result = str(budget_display(45, 50))
        assert "45/50" in result

    def test_critical_usage(self) -> None:
        from cortiva.cli.output import budget_display

        result = str(budget_display(48, 50))
        assert "48/50" in result

    def test_zero_limit(self) -> None:
        from cortiva.cli.output import budget_display

        assert budget_display(0, 0) == "\u2014"


class TestHoursDisplay:
    def test_no_overtime(self) -> None:
        from cortiva.cli.output import hours_display

        assert hours_display(6.5) == "6.5h"

    def test_with_overtime(self) -> None:
        from cortiva.cli.output import hours_display

        result = str(hours_display(8.0, 1.5))
        assert "8.0h" in result
        assert "1.5h OT" in result


class TestPlainTable:
    def test_render(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import _PlainTable

        table = _PlainTable("Test Title")
        table.add_column("Name")
        table.add_column("Count", justify="right")
        table.add_row("alice", "10")
        table.add_row("bob", "200")
        table.render()

        out = capsys.readouterr().out
        assert "Test Title" in out
        assert "Name" in out
        assert "Count" in out
        assert "alice" in out
        assert "200" in out

    def test_empty_columns(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import _PlainTable

        table = _PlainTable()
        table.render()
        out = capsys.readouterr().out
        # No columns means no output aside from maybe empty title
        assert "Name" not in out

    def test_center_justify(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import _PlainTable

        table = _PlainTable()
        table.add_column("X", justify="center")
        table.add_row("hi")
        table.render()
        out = capsys.readouterr().out
        assert "hi" in out


class TestCreateAndPrintTable:
    def test_create_table_returns_object(self) -> None:
        from cortiva.cli.output import create_table

        table = create_table("My Table")
        # Should have add_column and add_row
        assert hasattr(table, "add_column")
        assert hasattr(table, "add_row")

    def test_print_table(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import _PlainTable, print_table

        table = _PlainTable("T")
        table.add_column("A")
        table.add_row("v")
        # Force plain path by passing PlainTable directly
        with _plain_output():
            print_table(table)
        out = capsys.readouterr().out
        assert "v" in out


class TestPrintHelpers:
    """Test the plain-text fallback path for all print helpers."""

    def test_print_header(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import print_header

        with _plain_output():
            print_header("Hello", "sub")
        out = capsys.readouterr().out
        assert "Hello" in out
        assert "sub" in out

    def test_print_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import print_success

        with _plain_output():
            print_success("done")
        out = capsys.readouterr().out
        assert "done" in out

    def test_print_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import print_error

        with _plain_output():
            print_error("fail")
        out = capsys.readouterr().out
        assert "fail" in out

    def test_print_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import print_warning

        with _plain_output():
            print_warning("warn")
        out = capsys.readouterr().out
        assert "warn" in out

    def test_print_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import print_info

        with _plain_output():
            print_info("info msg")
        out = capsys.readouterr().out
        assert "info msg" in out

    def test_print_muted(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import print_muted

        with _plain_output():
            print_muted("dim")
        out = capsys.readouterr().out
        assert "dim" in out

    def test_print_kv(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cortiva.cli.output import print_kv

        with _plain_output():
            print_kv("key", "val")
        out = capsys.readouterr().out
        assert "key:" in out
        assert "val" in out


# ---------------------------------------------------------------------------
# CLI commands (cortiva.cli.main)
# ---------------------------------------------------------------------------


def _ns(**kwargs: object) -> argparse.Namespace:
    """Build an argparse.Namespace from keyword args."""
    return argparse.Namespace(**kwargs)


class TestCmdInit:
    def test_creates_workspace(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        from cortiva.cli.main import cmd_init

        cmd_init(_ns(name="myws"))

        ws = tmp_path / "myws"
        assert ws.is_dir()
        assert (ws / "agents").is_dir()
        assert (ws / "cortiva.yaml").exists()

        config = yaml.safe_load((ws / "cortiva.yaml").read_text())
        assert config["fabric"]["name"] == "myws"

        out = capsys.readouterr().out
        assert "Initialised" in out
        assert "myws" in out

    def test_existing_directory_exits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "existing").mkdir()
        from cortiva.cli.main import cmd_init

        with pytest.raises(SystemExit):
            cmd_init(_ns(name="existing"))


class TestCmdStatus:
    def test_ipc_live_path(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")

        live_data = {
            "ok": True,
            "running": True,
            "agents": {
                "agent-1": {
                    "state": "executing",
                    "consciousness_used": 10,
                    "consciousness_remaining": 40,
                    "tasks_today": 3,
                },
            },
        }
        with patch("cortiva.cli.main._try_ipc_status", return_value=live_data):
            from cortiva.cli.main import cmd_status
            cmd_status(_ns())

        out = capsys.readouterr().out
        assert "agent-1" in out

    def test_filesystem_fallback(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_dir = agents_dir / "alpha"
        agent_dir.mkdir()
        (agent_dir / "identity").mkdir()
        (agent_dir / "identity" / "identity.md").write_text("# alpha")
        (agent_dir / "today").mkdir()
        (agent_dir / "today" / "plan.md").write_text("# plan")

        with patch("cortiva.cli.main._try_ipc_status", return_value=None):
            from cortiva.cli.main import cmd_status
            cmd_status(_ns())

        out = capsys.readouterr().out
        assert "alpha" in out

    def test_no_config_exits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        from cortiva.cli.main import cmd_status

        with pytest.raises(SystemExit):
            cmd_status(_ns())

    def test_no_agents_dir(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")

        with patch("cortiva.cli.main._try_ipc_status", return_value=None):
            from cortiva.cli.main import cmd_status
            cmd_status(_ns())

        out = capsys.readouterr().out
        assert "No agents" in out

    def test_empty_agents_dir(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")
        (tmp_path / "agents").mkdir()

        with patch("cortiva.cli.main._try_ipc_status", return_value=None):
            from cortiva.cli.main import cmd_status
            cmd_status(_ns())

        out = capsys.readouterr().out
        assert "No agents registered" in out


class TestCmdAgentCreate:
    def test_create_agent_no_template(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")
        (tmp_path / "agents").mkdir()

        from cortiva.cli.main import cmd_agent_create

        cmd_agent_create(_ns(id="bot-1", template=None))

        agent_dir = tmp_path / "agents" / "bot-1"
        assert agent_dir.is_dir()
        assert (agent_dir / "identity" / "identity.md").exists()
        assert (agent_dir / "identity" / "soul.md").exists()
        assert (agent_dir / "today" / "plan.md").exists()

        out = capsys.readouterr().out
        assert "Created agent: bot-1" in out

    def test_create_agent_already_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")
        (tmp_path / "agents" / "bot-1").mkdir(parents=True)

        from cortiva.cli.main import cmd_agent_create

        with pytest.raises(SystemExit):
            cmd_agent_create(_ns(id="bot-1", template=None))

    def test_no_workspace_exits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        from cortiva.cli.main import cmd_agent_create

        with pytest.raises(SystemExit):
            cmd_agent_create(_ns(id="bot-1", template=None))

    def test_create_agent_with_template(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")
        (tmp_path / "agents").mkdir()

        with patch("cortiva.templates.apply_template", return_value=["identity.md", "soul.md"]):
            from cortiva.cli.main import cmd_agent_create
            cmd_agent_create(_ns(id="bot-tpl", template="my-template"))

        out = capsys.readouterr().out
        assert "bot-tpl" in out
        assert "my-template" in out


class TestCmdWatch:
    def test_watch_success(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        resp = {
            "ok": True,
            "agents": {
                "dev-01": {
                    "state": "executing",
                    "current_task": "Writing tests",
                    "hours_today": 3.5,
                    "overtime_hours": 0,
                    "consciousness_used": 15,
                    "consciousness_limit": 50,
                    "task_progress": "3/7",
                },
            },
        }
        client = _mock_ipc_client(resp=resp)
        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_watch
            cmd_watch(_ns())

        out = capsys.readouterr().out
        assert "dev-01" in out

    def test_watch_no_daemon(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        client = _mock_ipc_client(is_running=False)
        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_watch
            with pytest.raises(SystemExit):
                cmd_watch(_ns())

    def test_watch_error_response(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        client = _mock_ipc_client(resp={"ok": False, "error": "boom"})
        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_watch
            with pytest.raises(SystemExit):
                cmd_watch(_ns())

    def test_watch_no_agents(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        client = _mock_ipc_client(resp={"ok": True, "agents": {}})
        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_watch
            cmd_watch(_ns())

        out = capsys.readouterr().out
        assert "No agents" in out

    def test_watch_with_capacity(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        resp = {
            "ok": True,
            "agents": {
                "a1": {
                    "state": "sleeping",
                    "current_task": None,
                    "hours_today": 0,
                    "overtime_hours": 0,
                    "consciousness_used": 0,
                    "consciousness_limit": 50,
                    "task_progress": "",
                },
            },
            "capacity": {
                "node": {"cpu_cores": 8, "ram_available_gb": 16, "ram_percent": 50},
                "contention": {"avg_queue_wait_s": 1.5, "avg_consciousness_wait_s": 0.5, "heartbeat_utilisation_pct": 30},
                "agents": {"active": 1, "total": 2, "max_concurrent": 4},
            },
        }
        client = _mock_ipc_client(resp=resp)
        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_watch
            cmd_watch(_ns())

        out = capsys.readouterr().out
        assert "a1" in out


class TestCmdAgentActivity:
    def test_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        resp = {
            "ok": True,
            "agent_id": "dev-01",
            "state": "executing",
            "timesheet": {"date": "2026-03-29", "total_hours": 4.0, "overtime_hours": 0.5, "scheduled_hours": 8},
            "current_task": {"description": "Writing unit tests"},
            "completed_tasks": [{"description": "Setup project"}],
            "pending_tasks": [{"description": "Deploy"}],
            "session_turns": [{"call_type": "assistant", "content": "Working on tests now"}],
        }
        client = _mock_ipc_client(resp=resp)

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_agent_activity
            cmd_agent_activity(_ns(id="dev-01"))

        out = capsys.readouterr().out
        assert "dev-01" in out
        assert "executing" in out
        assert "Writing unit tests" in out
        assert "Setup project" in out
        assert "Deploy" in out
        assert "Overtime" in out

    def test_no_daemon(self) -> None:
        client = _mock_ipc_client(is_running=False)
        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_agent_activity
            with pytest.raises(SystemExit):
                cmd_agent_activity(_ns(id="x"))


class TestCmdAgentHours:
    def test_ipc_path_today(self, capsys: pytest.CaptureFixture[str]) -> None:
        resp = {
            "ok": True,
            "agent_id": "dev-01",
            "period": "today",
            "date": "2026-03-29",
            "total_hours": 5.5,
            "scheduled_hours": 8,
            "overtime_hours": 0,
            "tasks_completed": 3,
            "tasks_escalated": 1,
            "entries": [],
        }
        client = _mock_ipc_client(resp=resp)

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_agent_hours
            cmd_agent_hours(_ns(id="dev-01", week=False))

        out = capsys.readouterr().out
        assert "dev-01" in out
        assert "5.5h" in out

    def test_ipc_path_week(self, capsys: pytest.CaptureFixture[str]) -> None:
        resp = {
            "ok": True,
            "agent_id": "dev-01",
            "period": "week",
            "total_hours": 35.0,
            "total_overtime": 2.0,
            "days": [
                {"date": "2026-03-24", "total_hours": 8.0, "scheduled_hours": 8, "overtime_hours": 0, "tasks_completed": 4},
                {"date": "2026-03-25", "total_hours": 9.0, "scheduled_hours": 8, "overtime_hours": 1.0, "tasks_completed": 5},
            ],
        }
        client = _mock_ipc_client(resp=resp)

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_agent_hours
            cmd_agent_hours(_ns(id="dev-01", week=True))

        out = capsys.readouterr().out
        assert "This Week" in out
        assert "35.0h" in out

    def test_filesystem_fallback_today(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        agent_dir = tmp_path / "agents" / "dev-01"
        agent_dir.mkdir(parents=True)

        client = _mock_ipc_client(is_running=False)

        mock_day = MagicMock()
        mock_day.to_dict.return_value = {
            "date": "2026-03-29",
            "total_hours": 2.0,
            "scheduled_hours": 8,
            "overtime_hours": 0,
            "tasks_completed": 1,
            "tasks_escalated": 0,
            "entries": [],
        }

        mock_ts = MagicMock()
        mock_ts.today.return_value = mock_day

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            with patch("cortiva.core.timesheet.Timesheet", return_value=mock_ts):
                from cortiva.cli.main import cmd_agent_hours
                cmd_agent_hours(_ns(id="dev-01", week=False))

        out = capsys.readouterr().out
        assert "dev-01" in out

    def test_agent_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        client = _mock_ipc_client(is_running=False)

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_agent_hours
            with pytest.raises(SystemExit):
                cmd_agent_hours(_ns(id="nonexistent", week=False))


class TestCmdSkillList:
    def test_show_categories(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_registry = MagicMock()
        mock_registry.count = 10
        mock_registry.categories.return_value = {"devtools": 5, "data": 3, "comms": 2}
        mock_registry.all_skills.return_value = ["s1", "s2"]

        with patch("cortiva.core.skills.SkillRegistry", return_value=mock_registry):
            from cortiva.cli.main import cmd_skill_list
            cmd_skill_list(_ns(agent=None, category=None, query=""))

        out = capsys.readouterr().out
        assert "10 skills" in out
        assert "devtools" in out

    def test_no_skills(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_registry = MagicMock()
        mock_registry.all_skills.return_value = []
        mock_registry.search.return_value = []

        with patch("cortiva.core.skills.SkillRegistry", return_value=mock_registry):
            from cortiva.cli.main import cmd_skill_list
            cmd_skill_list(_ns(agent=None, category="missing", query=""))

        out = capsys.readouterr().out
        assert "No skills found" in out


class TestCmdSkillSearch:
    def test_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_skill = MagicMock()
        mock_skill.name = "linear"
        mock_skill.category = "project-management"
        mock_skill.description = "Linear integration"

        mock_registry = MagicMock()
        mock_registry.search.return_value = [mock_skill]

        with patch("cortiva.core.skills.SkillRegistry", return_value=mock_registry):
            from cortiva.cli.main import cmd_skill_search
            cmd_skill_search(_ns(query="linear"))

        out = capsys.readouterr().out
        assert "linear" in out
        assert "1 skills" in out

    def test_not_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_registry = MagicMock()
        mock_registry.search.return_value = []

        with patch("cortiva.core.skills.SkillRegistry", return_value=mock_registry):
            from cortiva.cli.main import cmd_skill_search
            cmd_skill_search(_ns(query="zzz"))

        out = capsys.readouterr().out
        assert "No skills matching" in out


class TestCmdSkillInfo:
    def test_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_skill = MagicMock()
        mock_skill.name = "linear"
        mock_skill.description = "Linear integration"
        mock_skill.category = "pm"
        mock_skill.version = "1.0"
        mock_skill.tags = ["issues"]
        mock_skill.mcp = MagicMock(package="@linear/mcp", command="npx @linear/mcp", env=["LINEAR_API_KEY"])
        mock_skill.procedures = "## Use Linear\nDo stuff."

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_skill

        with patch("cortiva.core.skills.SkillRegistry", return_value=mock_registry):
            from cortiva.cli.main import cmd_skill_info
            cmd_skill_info(_ns(name="linear"))

        out = capsys.readouterr().out
        assert "linear" in out
        assert "Linear integration" in out
        assert "issues" in out
        assert "MCP Server" in out

    def test_not_found(self) -> None:
        mock_registry = MagicMock()
        mock_registry.get.return_value = None

        with patch("cortiva.core.skills.SkillRegistry", return_value=mock_registry):
            from cortiva.cli.main import cmd_skill_info
            with pytest.raises(SystemExit):
                cmd_skill_info(_ns(name="nope"))


class TestCmdOrgStatus:
    def test_org_with_departments(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")

        mock_dept = MagicMock()
        mock_dept.lead = "alice"
        mock_dept.members = ["alice", "bob"]

        mock_org = MagicMock()
        mock_org.name = "Test Corp"
        mock_org.departments = {"engineering": mock_dept}
        mock_org.reporting = {"bob": "alice"}
        mock_org.manager_of.side_effect = lambda m: "alice" if m == "bob" else None

        with patch("cortiva.core.config.load_config", return_value={"org": {}}):
            with patch("cortiva.core.org.parse_org_config", return_value=mock_org):
                from cortiva.cli.main import cmd_org_status
                cmd_org_status(_ns())

        out = capsys.readouterr().out
        assert "Test Corp" in out
        assert "engineering" in out
        assert "alice" in out

    def test_no_org_section(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")

        with patch("cortiva.core.config.load_config", return_value={}):
            with patch("cortiva.core.org.parse_org_config", return_value=None):
                from cortiva.cli.main import cmd_org_status
                cmd_org_status(_ns())

        out = capsys.readouterr().out
        assert "No org section" in out


class TestCmdApproveList:
    def test_pending(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "agents" / ".approvals").mkdir(parents=True)

        mock_req = MagicMock()
        mock_req.id = "req-1"
        mock_req.agent_id = "dev-01"
        mock_req.approver_id = "pm-01"
        mock_req.task_description = "Deploy to production"

        mock_queue = MagicMock()
        mock_queue.all_pending.return_value = [mock_req]

        with patch("cortiva.core.approval.ApprovalQueue", return_value=mock_queue):
            from cortiva.cli.main import cmd_approve_list
            cmd_approve_list(_ns())

        out = capsys.readouterr().out
        assert "req-1" in out
        assert "dev-01" in out

    def test_no_pending(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        mock_queue = MagicMock()
        mock_queue.all_pending.return_value = []

        with patch("cortiva.core.approval.ApprovalQueue", return_value=mock_queue):
            from cortiva.cli.main import cmd_approve_list
            cmd_approve_list(_ns())

        out = capsys.readouterr().out
        assert "No pending" in out


class TestCmdApproveAccept:
    def test_success(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        mock_result = MagicMock()
        mock_result.agent_id = "dev-01"
        mock_result.task_description = "Deploy"

        mock_queue = MagicMock()
        mock_queue.approve.return_value = mock_result

        with patch("cortiva.core.approval.ApprovalQueue", return_value=mock_queue):
            from cortiva.cli.main import cmd_approve_accept
            cmd_approve_accept(_ns(id="req-1"))

        out = capsys.readouterr().out
        assert "Approved" in out

    def test_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        mock_queue = MagicMock()
        mock_queue.approve.return_value = None

        with patch("cortiva.core.approval.ApprovalQueue", return_value=mock_queue):
            from cortiva.cli.main import cmd_approve_accept
            with pytest.raises(SystemExit):
                cmd_approve_accept(_ns(id="nope"))


class TestCmdApproveReject:
    def test_success(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        mock_result = MagicMock()
        mock_result.task_description = "Deploy"

        mock_queue = MagicMock()
        mock_queue.reject.return_value = mock_result

        with patch("cortiva.core.approval.ApprovalQueue", return_value=mock_queue):
            from cortiva.cli.main import cmd_approve_reject
            cmd_approve_reject(_ns(id="req-1", reason="too risky"))

        out = capsys.readouterr().out
        assert "Rejected" in out
        assert "too risky" in out

    def test_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        mock_queue = MagicMock()
        mock_queue.reject.return_value = None

        with patch("cortiva.core.approval.ApprovalQueue", return_value=mock_queue):
            from cortiva.cli.main import cmd_approve_reject
            with pytest.raises(SystemExit):
                cmd_approve_reject(_ns(id="nope", reason=""))


class TestCmdDelegate:
    def test_success(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")
        (tmp_path / "agents" / ".delegation").mkdir(parents=True)

        mock_assignment = MagicMock()
        mock_assignment.id = "asgn-1"
        mock_assignment.from_agent = "pm-01"
        mock_assignment.to_agent = "dev-01"
        mock_assignment.description = "Fix the bug"

        mock_mgr = MagicMock()
        mock_mgr.create_assignment.return_value = mock_assignment

        with patch("cortiva.core.config.load_config", return_value={}):
            with patch("cortiva.core.org.parse_org_config", return_value=None):
                with patch("cortiva.core.delegation.DelegationManager", return_value=mock_mgr):
                    from cortiva.cli.main import cmd_delegate
                    cmd_delegate(_ns(from_agent="pm-01", to_agent="dev-01", description="Fix the bug", priority=1))

        out = capsys.readouterr().out
        assert "asgn-1" in out
        assert "pm-01" in out
        assert "dev-01" in out

    def test_permission_denied(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")

        mock_mgr = MagicMock()
        mock_mgr.create_assignment.side_effect = PermissionError("not allowed")

        with patch("cortiva.core.config.load_config", return_value={}):
            with patch("cortiva.core.org.parse_org_config", return_value=None):
                with patch("cortiva.core.delegation.DelegationManager", return_value=mock_mgr):
                    from cortiva.cli.main import cmd_delegate
                    with pytest.raises(SystemExit):
                        cmd_delegate(_ns(from_agent="a", to_agent="b", description="x", priority=1))


class TestCmdCapacity:
    def test_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        resp = {
            "ok": True,
            "node": {"cpu_cores": 8, "ram_total_gb": 32, "ram_available_gb": 16, "ram_percent": 50, "disk_free_gb": 100},
            "agents": {"active": 2, "total": 3, "max_concurrent": 6, "max_concurrent_basis": "cpu"},
            "contention": {
                "avg_queue_wait_s": 0.5,
                "avg_execution_s": 10.0,
                "avg_consciousness_wait_s": 1.0,
                "avg_heartbeat_s": 30.0,
                "avg_heartbeat_idle_s": 20.0,
                "heartbeat_utilisation_pct": 33,
            },
            "agent_share_pct": {"dev-01": 60.0, "qa-01": 40.0},
            "recent_tasks": [
                {"agent_id": "dev-01", "task_id": "t1", "queue_wait_s": 0.1, "execution_s": 5.0, "consciousness_wait_s": 0.3},
            ],
        }
        client = _mock_ipc_client(resp=resp)

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_capacity
            cmd_capacity(_ns())

        out = capsys.readouterr().out
        assert "CPU cores" in out
        assert "8" in out
        assert "dev-01" in out

    def test_no_daemon(self) -> None:
        client = _mock_ipc_client(is_running=False)
        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_capacity
            with pytest.raises(SystemExit):
                cmd_capacity(_ns())


class TestCmdAgentChat:
    def test_quit_exits(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        client = _mock_ipc_client()

        # Simulate user typing "quit"
        monkeypatch.setattr("builtins.input", lambda prompt: "quit")

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_agent_chat
            cmd_agent_chat(_ns(id="dev-01"))

        out = capsys.readouterr().out
        assert "Chat ended" in out

    def test_send_message(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        client = _mock_ipc_client(resp={"ok": True, "response": "Hello human!"})

        inputs = iter(["hello", "exit"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_agent_chat
            cmd_agent_chat(_ns(id="dev-01"))

        out = capsys.readouterr().out
        assert "Hello human!" in out

    def test_eof_exits(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        client = _mock_ipc_client()

        def raise_eof(prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_agent_chat
            cmd_agent_chat(_ns(id="dev-01"))

        out = capsys.readouterr().out
        assert "Chat ended" in out

    def test_no_daemon(self) -> None:
        client = _mock_ipc_client(is_running=False)
        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_agent_chat
            with pytest.raises(SystemExit):
                cmd_agent_chat(_ns(id="dev-01"))


class TestCmdAgentLogs:
    def test_ipc_path(self, capsys: pytest.CaptureFixture[str]) -> None:
        resp = {
            "ok": True,
            "state": "executing",
            "identity": "# dev-01\nTest agent identity that is useful for testing purposes.",
            "task_queue": {
                "tasks": [{"id": "t1", "status": "done", "description": "Setup"}],
                "summary": {"done": 1, "pending": 0, "exceptions": 0},
            },
            "exceptions": [],
            "recent_journals": [{"date": "2026-03-29", "preview": "Worked on tests"}],
            "recent_memories": [{"importance": 8, "content": "Learned testing", "tags": ["testing"]}],
            "familiarity": [{"strength": "high", "task": "unit tests"}],
        }
        client = _mock_ipc_client(resp=resp)

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            from cortiva.cli.main import cmd_agent_logs
            cmd_agent_logs(_ns(id="dev-01"))

        out = capsys.readouterr().out
        assert "dev-01" in out
        assert "Tasks:" in out

    def test_no_daemon_filesystem_fallback(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        agent_dir = tmp_path / "agents" / "dev-01"
        agent_dir.mkdir(parents=True)

        client = _mock_ipc_client(is_running=False)
        mock_logs = {
            "state": "sleeping",
            "identity": "# dev-01",
            "task_queue": None,
            "exceptions": [],
            "recent_journals": [],
            "recent_memories": [],
            "familiarity": [],
        }

        with patch("cortiva.core.ipc.FabricClient", return_value=client):
            with patch("cortiva.core.agent.Agent") as mock_agent_cls:
                with patch("cortiva.adapters.memory.inmemory.InMemoryAdapter"):
                    with patch("cortiva.core.chat.get_agent_logs", return_value=mock_logs):
                        import asyncio as _real_asyncio
                        with patch.object(_real_asyncio, "run", return_value=mock_logs):
                            from cortiva.cli.main import cmd_agent_logs
                            cmd_agent_logs(_ns(id="dev-01"))

        out = capsys.readouterr().out
        assert "dev-01" in out


class TestCmdTemplateList:
    def test_templates_available(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("cortiva.templates.list_templates", return_value=["dev-cortiva", "qa-cortiva"]):
            from cortiva.cli.main import cmd_template_list
            cmd_template_list(_ns())

        out = capsys.readouterr().out
        assert "dev-cortiva" in out
        assert "qa-cortiva" in out
        assert "2" in out

    def test_no_templates(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("cortiva.templates.list_templates", return_value=[]):
            from cortiva.cli.main import cmd_template_list
            cmd_template_list(_ns())

        out = capsys.readouterr().out
        assert "No templates" in out


class TestCmdDiscover:
    def test_discover(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        mock_terminal = MagicMock()
        mock_terminal.name = "claude-code"
        mock_terminal.available = True
        mock_terminal.auth_ok = True
        mock_terminal.version = "1.0"

        mock_model = MagicMock()
        mock_model.name = "qwen3.5:35b"
        mock_model.parameter_size = "35B"
        mock_model.size_bytes = 20 * (1024 ** 3)

        mock_resources = MagicMock()
        mock_resources.cpu_cores = 10
        mock_resources.ram_available_gb = 32.0
        mock_resources.ram_total_gb = 64.0
        mock_resources.disk_free_gb = 500
        mock_resources.disk_total_gb = 1000
        mock_resources.platform = "darwin"
        mock_resources.python_version = "3.13.0"

        mock_caps = MagicMock()
        mock_caps.node_id = "test-node-1"
        mock_caps.terminal_agents = [mock_terminal]
        mock_caps.local_models = [mock_model]
        mock_caps.custom_endpoints = []
        mock_caps.resources = mock_resources

        async def fake_discover(node_id: str, custom_endpoints: object = None) -> object:
            return mock_caps

        with patch("cortiva.core.discovery.NodeCapabilities") as mock_nc_cls:
            mock_nc_cls.discover = fake_discover
            with patch("cortiva.cli.main.yaml") as mock_yaml:
                mock_yaml.safe_load.return_value = {}
                from cortiva.cli.main import cmd_discover
                cmd_discover(_ns())

        out = capsys.readouterr().out
        assert "test-node-1" in out
        assert "claude-code" in out
        assert "qwen3.5:35b" in out
        assert "10" in out  # cpu cores


class TestCmdBudget:
    def test_overview(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\nconsciousness:\n  budget:\n    daily_limit: 100\n")
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "dev-01").mkdir()

        mock_status = MagicMock()
        mock_status.exhausted = False
        mock_status.total_calls = 10
        mock_status.total_tokens = 5000
        mock_status.escalation_ratio = 0.1

        mock_mgr = MagicMock()
        mock_mgr.all_status.return_value = {"dev-01": mock_status}

        with patch("cortiva.core.config.load_config", return_value={"agents": {"directory": "./agents"}, "consciousness": {"budget": {}}}):
            with patch("cortiva.core.config._build_budget_manager", return_value=mock_mgr):
                from cortiva.cli.main import cmd_budget
                cmd_budget(_ns(agent=None))

        out = capsys.readouterr().out
        assert "dev-01" in out
        assert "OK" in out

    def test_agent_detail(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")

        mock_agent_status = MagicMock()
        mock_agent_status.backends = {
            "anthropic": {
                "is_exhausted": False,
                "calls_used": 5,
                "calls_limit": 50,
                "tokens_used": 2000,
                "tokens_limit": 100000,
            },
        }
        mock_agent_status.task_attempts = 10
        mock_agent_status.consciousness_calls = 5
        mock_agent_status.escalation_ratio = 0.1
        mock_agent_status.priority_counts = {}

        mock_mgr = MagicMock()
        mock_mgr.agent_status.return_value = mock_agent_status

        with patch("cortiva.core.config.load_config", return_value={"consciousness": {"budget": {}}}):
            with patch("cortiva.core.config._build_budget_manager", return_value=mock_mgr):
                from cortiva.cli.main import cmd_budget
                cmd_budget(_ns(agent="dev-01"))

        out = capsys.readouterr().out
        assert "dev-01" in out
        assert "anthropic" in out

    def test_no_budget_manager(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cortiva.yaml").write_text("fabric:\n  name: test\n")

        with patch("cortiva.core.config.load_config", return_value={}):
            with patch("cortiva.core.config._build_budget_manager", return_value=None):
                from cortiva.cli.main import cmd_budget
                cmd_budget(_ns(agent=None))

        out = capsys.readouterr().out
        assert "No budget manager" in out


class TestBuildParser:
    def test_parser_builds(self) -> None:
        from cortiva.cli.main import build_parser

        parser = build_parser()
        assert parser is not None
        assert parser.prog == "cortiva"
