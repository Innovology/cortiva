"""
Rich terminal output for the Cortiva CLI.

Provides styled console output, tables, panels, and status indicators.
All CLI commands should use these helpers instead of raw ``print()``.

When ``rich`` is not installed, falls back to plain text output so the
CLI remains functional without the optional dependency.
"""

from __future__ import annotations

from typing import Any

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme

    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

# Cortiva brand colours
_THEME = Theme({
    "info": "cyan",
    "success": "green",
    "warning": "yellow",
    "error": "red bold",
    "agent.executing": "green",
    "agent.sleeping": "dim",
    "agent.planning": "cyan",
    "agent.reflecting": "magenta",
    "agent.waking": "yellow",
    "agent.onboarding": "blue",
    "header": "bold dark_violet",
    "muted": "dim",
    "highlight": "bold",
}) if _RICH_AVAILABLE else None  # type: ignore[assignment]

console = Console(theme=_THEME) if _RICH_AVAILABLE else None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# State indicators
# ---------------------------------------------------------------------------

_STATE_ICONS = {
    "executing": ("●", "agent.executing"),
    "sleeping": ("○", "agent.sleeping"),
    "planning": ("◐", "agent.planning"),
    "reflecting": ("◑", "agent.reflecting"),
    "waking": ("◒", "agent.waking"),
    "replanning": ("◓", "agent.planning"),
    "onboarding": ("◇", "agent.onboarding"),
}


def state_badge(state: str) -> str | Any:
    """Return a styled state indicator."""
    icon, style = _STATE_ICONS.get(state, ("?", ""))
    if _RICH_AVAILABLE:
        return Text(f"{icon} {state}", style=style)
    return f"{icon} {state}"


# ---------------------------------------------------------------------------
# Core output functions
# ---------------------------------------------------------------------------


def print_header(title: str, subtitle: str = "") -> None:
    """Print a branded header panel."""
    if _RICH_AVAILABLE and console:
        text = f"[header]{title}[/header]"
        if subtitle:
            text += f"\n[muted]{subtitle}[/muted]"
        console.print(Panel(text, border_style="dark_violet", expand=False))
    else:
        print(f"\n{title}")
        if subtitle:
            print(f"  {subtitle}")
        print()


def print_success(message: str) -> None:
    if _RICH_AVAILABLE and console:
        console.print(f"[success]✓[/success] {message}")
    else:
        print(f"✓ {message}")


def print_error(message: str) -> None:
    if _RICH_AVAILABLE and console:
        console.print(f"[error]✗ {message}[/error]")
    else:
        print(f"✗ {message}")


def print_warning(message: str) -> None:
    if _RICH_AVAILABLE and console:
        console.print(f"[warning]⚠ {message}[/warning]")
    else:
        print(f"⚠ {message}")


def print_info(message: str) -> None:
    if _RICH_AVAILABLE and console:
        console.print(f"[info]{message}[/info]")
    else:
        print(message)


def print_muted(message: str) -> None:
    if _RICH_AVAILABLE and console:
        console.print(f"[muted]{message}[/muted]")
    else:
        print(message)


def print_kv(key: str, value: str, indent: int = 2) -> None:
    """Print a key-value pair."""
    pad = " " * indent
    if _RICH_AVAILABLE and console:
        console.print(f"{pad}[highlight]{key}:[/highlight] {value}")
    else:
        print(f"{pad}{key}: {value}")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def create_table(title: str = "", **kwargs: Any) -> Any:
    """Create a styled table.

    Returns a ``rich.Table`` if available, otherwise a list-based
    fallback that mimics the add_column/add_row API.
    """
    if _RICH_AVAILABLE:
        return Table(
            title=title or None,
            border_style="dim",
            header_style="bold",
            show_lines=False,
            pad_edge=True,
            **kwargs,
        )
    return _PlainTable(title)


def print_table(table: Any) -> None:
    """Print a table (rich or plain)."""
    if _RICH_AVAILABLE and console:
        console.print(table)
    elif isinstance(table, _PlainTable):
        table.render()


class _PlainTable:
    """Fallback table renderer when rich is not available."""

    def __init__(self, title: str = "") -> None:
        self.title = title
        self.columns: list[dict[str, Any]] = []
        self.rows: list[list[str]] = []

    def add_column(self, header: str, **kwargs: Any) -> None:
        justify = kwargs.get("justify", "left")
        self.columns.append({"header": header, "justify": justify})

    def add_row(self, *values: Any) -> None:
        self.rows.append([str(v) for v in values])

    def render(self) -> None:
        if self.title:
            print(f"\n{self.title}\n")

        if not self.columns:
            return

        # Compute column widths
        widths = [len(c["header"]) for c in self.columns]
        for row in self.rows:
            for i, val in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], len(val))

        # Header
        header = "  ".join(
            c["header"].ljust(w) for c, w in zip(self.columns, widths)
        )
        print(f"  {header}")
        print(f"  {'  '.join('-' * w for w in widths)}")

        # Rows
        for row in self.rows:
            cells = []
            for i, val in enumerate(row):
                w = widths[i] if i < len(widths) else len(val)
                justify = self.columns[i]["justify"] if i < len(self.columns) else "left"
                if justify == "right":
                    cells.append(val.rjust(w))
                elif justify == "center":
                    cells.append(val.center(w))
                else:
                    cells.append(val.ljust(w))
            print(f"  {'  '.join(cells)}")


# ---------------------------------------------------------------------------
# Progress bars
# ---------------------------------------------------------------------------


def progress_bar(current: float, total: float, width: int = 10) -> str | Any:
    """Render a progress bar like ███░░░ 3/7."""
    if total <= 0:
        return "—"
    pct = min(current / total, 1.0)
    filled = int(pct * width)
    empty = width - filled

    label = f"{int(current)}/{int(total)}"

    if _RICH_AVAILABLE:
        bar = f"[green]{'█' * filled}[/green][dim]{'░' * empty}[/dim] {label}"
        return bar
    return f"{'█' * filled}{'░' * empty} {label}"


def budget_display(used: int, limit: int) -> str | Any:
    """Render a budget display like 12/50."""
    if limit <= 0:
        return "—"
    pct = used / limit
    if _RICH_AVAILABLE:
        if pct >= 0.9:
            return f"[error]{used}/{limit}[/error]"
        if pct >= 0.7:
            return f"[warning]{used}/{limit}[/warning]"
        return f"[success]{used}/{limit}[/success]"
    return f"{used}/{limit}"


def hours_display(hours: float, overtime: float = 0.0) -> str | Any:
    """Render hours with overtime indicator."""
    base = f"{hours:.1f}h"
    if overtime > 0:
        if _RICH_AVAILABLE:
            return f"{base} [warning]+{overtime:.1f}h OT[/warning]"
        return f"{base} +{overtime:.1f}h OT"
    return base
