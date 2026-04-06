"""Curses-based live dashboard for ``cortiva watch --live``."""

from __future__ import annotations

import curses
import time
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
# The fetch function should return a dict shaped like:
# {
#   "agents": {
#       "<name>": {
#           "state": str,
#           "current_task": str,
#           "task_progress": float,       # 0.0 – 1.0
#           "hours": float,
#           "overtime": bool,
#           "budget": str,                # e.g. "45/100"
#       },
#       ...
#   },
#   "capacity": {
#       "cpu": float,          # 0.0 – 1.0
#       "ram": float,          # 0.0 – 1.0
#       "active_agents": int,
#       "contention": float,   # 0.0 – 1.0
#   },
# }

DEFAULT_INTERVAL = 2  # seconds


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_timestamp() -> str:
    """Return a human-readable timestamp string."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def format_pct(value: float) -> str:
    """Format a 0-1 float as a percentage string like '  42%'."""
    return f"{value * 100:4.0f}%"


def format_progress_bar(progress: float, width: int = 10) -> str:
    """Return an ASCII progress bar like '[####------]'."""
    filled = int(round(progress * width))
    filled = max(0, min(filled, width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def format_agent_row(name: str, info: dict[str, Any]) -> str:
    """Format one agent row for the table."""
    state = info.get("state", "unknown")
    task = info.get("current_task", "")
    progress = info.get("task_progress", 0.0)
    hours = info.get("hours", 0.0)
    overtime = "OT" if info.get("overtime") else "  "
    budget = info.get("budget", "—")

    bar = format_progress_bar(progress)
    return (
        f"  {name:<18} {state:<12} {task:<20} {bar} "
        f"{hours:6.1f}h {overtime} {budget:>10}"
    )


def format_header(agent_count: int) -> str:
    """Return the header line with timestamp and agent count."""
    ts = format_timestamp()
    return f"  Cortiva Dashboard — {ts} — {agent_count} agent(s)"


def format_table_header() -> str:
    """Return the column header for the agent table."""
    return (
        f"  {'Agent':<18} {'State':<12} {'Task':<20} {'Progress':<12} "
        f"{'Hours':>6}    {'Budget':>10}"
    )


def format_table_separator() -> str:
    """Return a separator line matching the table width."""
    return "  " + "-" * 94


def format_capacity_footer(capacity: dict[str, Any]) -> str:
    """Return the capacity footer string."""
    cpu = format_pct(capacity.get("cpu", 0.0))
    ram = format_pct(capacity.get("ram", 0.0))
    active = capacity.get("active_agents", 0)
    contention = format_pct(capacity.get("contention", 0.0))
    return f"  CPU:{cpu}  RAM:{ram}  Active: {active}  Contention:{contention}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_frame(
    win: Any,
    data: dict[str, Any],
) -> None:
    """Draw one frame of the dashboard onto the curses window."""
    win.erase()
    max_y, max_x = win.getmaxyx()

    agents: dict[str, dict[str, Any]] = data.get("agents", {})
    capacity: dict[str, Any] = data.get("capacity", {})

    lines: list[str] = []
    lines.append("")
    lines.append(format_header(len(agents)))
    lines.append("")
    lines.append(format_table_header())
    lines.append(format_table_separator())

    for name, info in agents.items():
        lines.append(format_agent_row(name, info))

    lines.append(format_table_separator())
    lines.append("")
    lines.append(format_capacity_footer(capacity))
    lines.append("")
    lines.append("  Press 'q' to exit.")

    for idx, line in enumerate(lines):
        if idx >= max_y:
            break
        try:
            win.addnstr(idx, 0, line, max_x - 1)
        except curses.error:
            pass  # writing to bottom-right corner can raise; ignore

    win.refresh()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _dashboard_loop(
    stdscr: Any,
    fetch_fn: Callable[[], dict[str, Any]],
    interval: int,
) -> None:
    """Inner curses loop. Separated for testability."""
    curses.curs_set(0)
    stdscr.timeout(interval * 1000)

    while True:
        data = fetch_fn()
        render_frame(stdscr, data)

        key = stdscr.getch()
        if key == ord("q"):
            break
        if key == curses.KEY_RESIZE:
            curses.update_lines_cols()


def run_dashboard(
    fetch_fn: Callable[[], dict[str, Any]],
    interval: int = DEFAULT_INTERVAL,
) -> None:
    """Public entry point — starts the curses application.

    *fetch_fn* is called every *interval* seconds and must return a dict
    with ``agents`` and ``capacity`` keys (see module docstring for shape).
    """
    curses.wrapper(lambda stdscr: _dashboard_loop(stdscr, fetch_fn, interval))
