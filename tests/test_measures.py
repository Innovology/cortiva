"""Numbers a company is held to: declared by its agents, read by the system."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from cortiva.core import measures as me
from tests.test_teamchat import _fab


def _fetch(values: dict[str, object]):
    def fetch(url: str, timeout: float) -> tuple[int, str]:
        if url not in values:
            return 404, ""
        return 200, json.dumps({"count": values[url]})

    return fetch


def _funnel(d: Path) -> None:
    """A support desk: one driver at each level. Declared outcome first."""
    assert me.declare(d, "head", name="Tickets solved")[0]
    assert me.declare(
        d,
        "head",
        name="Tickets answered",
        url="https://x/answered",
        path="count",
        drives="Tickets solved",
    )[0]
    assert me.declare(
        d,
        "head",
        name="Tickets opened",
        url="https://x/opened",
        path="count",
        drives="tickets answered",
    )[0]


def test_a_number_is_read_by_the_system_and_its_movement_is_seen(tmp_path):
    _funnel(tmp_path)
    assert me.probe(tmp_path, _fetch({"https://x/opened": 40, "https://x/answered": 10})) == []
    moved = me.probe(tmp_path, _fetch({"https://x/opened": 50, "https://x/answered": 10}))
    assert moved == [("tickets-opened", 40.0, 50.0)]
    state = me.load_state(tmp_path)
    assert state["tickets-answered"]["value"] == 10.0
    assert "no source yet" in state["tickets-solved"]["note"]


def test_a_funnel_shows_how_much_came_through_each_step(tmp_path):
    _funnel(tmp_path)
    me.probe(tmp_path, _fetch({"https://x/opened": 40, "https://x/answered": 10}))
    block = me.context_block(me.load(tmp_path), me.load_state(tmp_path), "head")
    lines = block.splitlines()
    assert "- **Tickets solved** (yours): no source yet: nothing exposes this number" in lines
    assert any(
        ln.startswith("  - **Tickets answered** (yours): 10") and "25.0% of Tickets opened" in ln
        for ln in lines
    )
    assert any(ln.startswith("    - **Tickets opened** (yours): 40") for ln in lines)
    assert "Nothing to show yet, furthest down: **Tickets solved**" in block
    assert "(head)" in me.context_block(me.load(tmp_path), me.load_state(tmp_path), "other")


def test_an_outcome_with_several_drivers_is_a_tree_and_lower_can_be_better(tmp_path):
    d = tmp_path
    assert me.declare(d, "cfo", name="Profit", url="https://x/p", path="count")[0]
    assert me.declare(d, "sales", name="Revenue", url="https://x/r", path="count", drives="Profit")[
        0
    ]
    assert me.declare(
        d,
        "ops",
        name="Cost of goods",
        url="https://x/c",
        path="count",
        drives="Profit",
        better="down",
    )[0]
    assert me.declare(d, "sales", name="Orders", drives="Revenue")[0]
    me.probe(d, _fetch({"https://x/p": 30, "https://x/r": 100, "https://x/c": 0}))
    lines = me.context_block(me.load(d), me.load_state(d), "cfo").splitlines()
    assert any(ln.startswith("- **Profit** (yours): 30") and "%" not in ln for ln in lines)
    assert any(ln.startswith("  - **Revenue** (sales): 100") for ln in lines)
    assert any(ln.startswith("  - **Cost of goods** (ops, lower is better): 0") for ln in lines)
    assert any(ln.startswith("    - **Orders** (sales): no source yet") for ln in lines)
    # A cost at zero is not a gap; the driver nobody can read is.
    assert lines[-1].startswith("Nothing to show yet, furthest down: **Orders**.")


def test_a_source_that_cannot_be_read_says_why_and_keeps_the_last_value(tmp_path):
    _funnel(tmp_path)
    me.probe(tmp_path, _fetch({"https://x/opened": 40, "https://x/answered": 10}))
    me.probe(tmp_path, _fetch({"https://x/answered": 10}))
    entry = me.load_state(tmp_path)["tickets-opened"]
    assert entry["value"] == 40.0 and "HTTP 404" in entry["note"]


def test_declaring_is_refused_when_it_makes_no_sense(tmp_path):
    _funnel(tmp_path)
    assert me.declare(tmp_path, "head", name="")[0] is False
    assert me.declare(tmp_path, "head", name="X", url="file:///etc/passwd")[0] is False
    assert me.declare(tmp_path, "head", name="X", drives="nothing declared")[0] is False
    assert me.declare(tmp_path, "head", name="X", better="sideways")[0] is False
    ok, why = me.declare(tmp_path, "someone-else", name="Tickets opened")
    assert ok is False and "head's number" in why
    ok, why = me.declare(tmp_path, "head", name="Tickets solved", drives="Tickets opened")
    assert ok is False and "circle" in why
    assert len(me.load(tmp_path)) == 3


def test_a_session_declares_for_its_own_agent_only(tmp_path):
    f = _fab(tmp_path)
    task = SimpleNamespace(id="task-1", description="Work out the number")
    tok = f._chat_session_env(f.agents["amara"], task)["CORTIVA_CHAT_TOKEN"]
    refused = asyncio.run(f._ipc_measure_declare(token="nope", name="Orders"))
    assert refused["ok"] is False
    done = asyncio.run(f._ipc_measure_declare(token=tok, name="Orders shipped"))
    assert done["ok"] and "no source yet" in done["text"]
    assert me.load(f.agents_dir)[0]["owner"] == "amara"
    listed = asyncio.run(f._ipc_measure_list(token=tok))
    assert "Orders shipped" in listed["text"]


def test_only_someone_who_leads_others_and_holds_no_number_is_asked_and_once_a_day(tmp_path):
    f = _fab(tmp_path)
    f._load_directory_cards = lambda: [
        {"id": "amara", "reports_to": "cto"},
        {"id": "cto", "reports_to": "ceo"},
    ]
    lead, report = SimpleNamespace(id="cto"), SimpleNamespace(id="amara")
    assert f._owes_a_number(lead) is True
    assert f._owes_a_number(report) is False
    f._number_was_asked(lead)
    assert f._owes_a_number(lead) is False
    me.declare(f.agents_dir, "ceo", name="Orders shipped")
    assert f._owes_a_number(SimpleNamespace(id="ceo")) is False
