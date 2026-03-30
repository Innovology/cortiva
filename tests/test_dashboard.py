"""Tests for cortiva.cli.dashboard — curses live dashboard module."""

from __future__ import annotations

import curses
from unittest.mock import MagicMock, patch

from cortiva.cli.dashboard import (
    _dashboard_loop,
    format_agent_row,
    format_capacity_footer,
    format_header,
    format_pct,
    format_progress_bar,
    format_table_header,
    format_table_separator,
    render_frame,
    run_dashboard,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DATA: dict = {
    "agents": {
        "alice": {
            "state": "working",
            "current_task": "write report",
            "task_progress": 0.6,
            "hours": 3.5,
            "overtime": False,
            "budget": "45/100",
        },
        "bob": {
            "state": "idle",
            "current_task": "",
            "task_progress": 0.0,
            "hours": 8.0,
            "overtime": True,
            "budget": "90/100",
        },
    },
    "capacity": {
        "cpu": 0.42,
        "ram": 0.73,
        "active_agents": 2,
        "contention": 0.05,
    },
}


def _mock_window(max_y: int = 40, max_x: int = 120) -> MagicMock:
    """Return a mock curses window."""
    win = MagicMock()
    win.getmaxyx.return_value = (max_y, max_x)
    win.getch.return_value = -1  # no key pressed
    return win


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


class TestFormatPct:
    def test_zero(self):
        assert format_pct(0.0) == "   0%"

    def test_one(self):
        assert format_pct(1.0) == " 100%"

    def test_fractional(self):
        assert format_pct(0.42) == "  42%"


class TestFormatProgressBar:
    def test_empty(self):
        assert format_progress_bar(0.0) == "[----------]"

    def test_full(self):
        assert format_progress_bar(1.0) == "[##########]"

    def test_half(self):
        assert format_progress_bar(0.5) == "[#####-----]"

    def test_custom_width(self):
        assert format_progress_bar(0.5, width=4) == "[##--]"

    def test_clamps_above_one(self):
        assert format_progress_bar(1.5) == "[##########]"

    def test_clamps_below_zero(self):
        assert format_progress_bar(-0.5) == "[----------]"


class TestFormatAgentRow:
    def test_working_agent(self):
        row = format_agent_row("alice", SAMPLE_DATA["agents"]["alice"])
        assert "alice" in row
        assert "working" in row
        assert "write report" in row
        assert "3.5h" in row
        assert "45/100" in row
        # Not overtime
        assert "OT" not in row

    def test_overtime_agent(self):
        row = format_agent_row("bob", SAMPLE_DATA["agents"]["bob"])
        assert "bob" in row
        assert "idle" in row
        assert "8.0h" in row
        assert "OT" in row

    def test_missing_fields(self):
        row = format_agent_row("x", {})
        assert "x" in row
        assert "unknown" in row


class TestFormatHeader:
    def test_contains_count(self):
        header = format_header(3)
        assert "3 agent(s)" in header
        assert "Cortiva Dashboard" in header


class TestFormatTableHeader:
    def test_columns_present(self):
        hdr = format_table_header()
        for col in ("Agent", "State", "Task", "Progress", "Hours", "Budget"):
            assert col in hdr


class TestFormatTableSeparator:
    def test_is_dashes(self):
        sep = format_table_separator()
        assert "---" in sep


class TestFormatCapacityFooter:
    def test_all_fields(self):
        footer = format_capacity_footer(SAMPLE_DATA["capacity"])
        assert "CPU:" in footer
        assert "RAM:" in footer
        assert "Active: 2" in footer
        assert "Contention:" in footer

    def test_empty_capacity(self):
        footer = format_capacity_footer({})
        assert "CPU:" in footer
        assert "Active: 0" in footer


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRenderFrame:
    def test_draws_lines(self):
        win = _mock_window()
        render_frame(win, SAMPLE_DATA)
        assert win.erase.called
        assert win.refresh.called
        # Should have drawn multiple lines via addnstr
        assert win.addnstr.call_count > 5

    def test_agent_names_rendered(self):
        win = _mock_window()
        render_frame(win, SAMPLE_DATA)
        rendered = " ".join(
            str(call.args[2]) for call in win.addnstr.call_args_list
        )
        assert "alice" in rendered
        assert "bob" in rendered

    def test_small_terminal(self):
        """Gracefully handles a very small terminal — just renders what fits."""
        win = _mock_window(max_y=3, max_x=40)
        render_frame(win, SAMPLE_DATA)
        assert win.addnstr.call_count == 3

    def test_addnstr_error_ignored(self):
        """curses.error from addnstr (bottom-right corner) is silently caught."""
        win = _mock_window()
        win.addnstr.side_effect = curses.error("write error")
        render_frame(win, SAMPLE_DATA)  # should not raise

    def test_empty_data(self):
        win = _mock_window()
        render_frame(win, {"agents": {}, "capacity": {}})
        assert win.refresh.called


# ---------------------------------------------------------------------------
# Dashboard loop
# ---------------------------------------------------------------------------


@patch("cortiva.cli.dashboard.curses")
class TestDashboardLoop:
    def _prep_mock_curses(self, mock_curses):
        """Set constants that _dashboard_loop reads from the patched module."""
        mock_curses.KEY_RESIZE = curses.KEY_RESIZE
        mock_curses.error = curses.error

    def test_quit_on_q(self, mock_curses):
        """Loop exits when 'q' is pressed."""
        self._prep_mock_curses(mock_curses)
        win = _mock_window()
        win.getch.return_value = ord("q")
        fetch = MagicMock(return_value=SAMPLE_DATA)

        _dashboard_loop(win, fetch, interval=2)

        fetch.assert_called_once()
        win.timeout.assert_called_once_with(2000)

    def test_multiple_iterations(self, mock_curses):
        """Loop runs multiple times before quit."""
        self._prep_mock_curses(mock_curses)
        win = _mock_window()
        win.getch.side_effect = [-1, -1, ord("q")]
        fetch = MagicMock(return_value=SAMPLE_DATA)

        _dashboard_loop(win, fetch, interval=1)

        assert fetch.call_count == 3

    def test_resize_handling(self, mock_curses):
        """KEY_RESIZE triggers update_lines_cols and continues the loop."""
        self._prep_mock_curses(mock_curses)

        win = _mock_window()
        win.getch.side_effect = [curses.KEY_RESIZE, ord("q")]
        fetch = MagicMock(return_value=SAMPLE_DATA)

        _dashboard_loop(win, fetch, interval=1)

        assert fetch.call_count == 2
        mock_curses.update_lines_cols.assert_called_once()


class TestRunDashboard:
    @patch("cortiva.cli.dashboard.curses")
    def test_wraps_curses(self, mock_curses):
        """run_dashboard delegates to curses.wrapper."""
        fetch = MagicMock(return_value=SAMPLE_DATA)
        run_dashboard(fetch, interval=1)
        mock_curses.wrapper.assert_called_once()
