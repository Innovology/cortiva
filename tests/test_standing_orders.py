"""Standing orders — durable prohibitions, scope matching, ledger parking."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cortiva.core import commitments as cm
from cortiva.core import standing_orders as so
from cortiva.core.agent_tools import tools_for_agent


def _write_orders(agents_dir: Path, orders: list[dict]) -> None:
    (agents_dir / so.ORDERS_FILE).write_text(json.dumps(orders), encoding="utf-8")


def _order(
    text: str = "All work on MarketMesh is halted — commercial dispute.",
    scope_type: str = "product",
    scope_value: str = "marketmesh",
    order_id: str = "o1",
    status: str = "active",
) -> dict:
    return {
        "order_id": order_id,
        "text": text,
        "kind": "prohibition",
        "scope": {"type": scope_type, "value": scope_value},
        "issued_by": {"name": "Alex", "role": "founder"},
        "issued_at": "2026-06-11T09:00:00+00:00",
        "status": status,
    }


class TestLoad:
    def test_missing_file_is_empty(self, tmp_path):
        assert so.load(tmp_path) == []

    def test_only_active_orders_load(self, tmp_path):
        _write_orders(tmp_path, [_order(), _order(order_id="o2", status="lifted")])
        loaded = so.load(tmp_path)
        assert [o["order_id"] for o in loaded] == ["o1"]

    def test_unreadable_file_is_empty(self, tmp_path):
        (tmp_path / so.ORDERS_FILE).write_text("{not json", encoding="utf-8")
        assert so.load(tmp_path) == []


class TestScopeMatching:
    def test_product_scope_matches_by_word(self):
        orders = [_order()]
        assert so.matching_order(orders, "Fix marketmesh#206 data sync") is not None
        assert so.matching_order(orders, "Grant write access to MarketMesh repo") is not None

    def test_product_scope_needs_word_boundary(self):
        orders = [_order(scope_value="mesh")]
        assert so.matching_order(orders, "work on marketmesh") is None

    def test_repo_scope_matches_full_path_and_bare_name(self):
        orders = [_order(scope_type="repo", scope_value="innovology/marketmesh")]
        assert so.matching_order(orders, "PR on Innovology/marketmesh today") is not None
        assert so.matching_order(orders, "review the marketmesh PR") is not None

    def test_org_scope_never_string_matches(self):
        # Org-wide orders are rules of conduct in the context block, not
        # string matchers — they must not park every commitment.
        orders = [_order(scope_type="org", scope_value="")]
        assert so.matching_order(orders, "literally anything") is None


class TestContextBlock:
    def test_empty_without_orders(self):
        assert so.context_block([]) == ""

    def test_block_carries_order_and_conflict_rule(self):
        block = so.context_block([_order()])
        assert "Standing orders" in block
        assert "MarketMesh is halted" in block
        assert "product: marketmesh" in block
        assert "which stands" in block  # the ask-don't-resolve rule
        assert "never" in block.lower()


class TestLedgerParking:
    def _register(self, agent_dir: Path, what: str, due_hours: float = 24.0) -> cm.Commitment:
        due = (datetime.now(UTC) + timedelta(hours=due_hours)).isoformat()
        return cm.register(agent_dir, to="alex", what=what, due=due, effort_hours=2.0)

    def test_matching_open_commitment_is_parked(self, tmp_path):
        c = self._register(tmp_path, "Ship the marketmesh data-sync fix")
        parked, revived = so.apply_to_ledger(tmp_path, [_order()])
        assert (parked, revived) == (1, 0)
        items = cm.load(tmp_path)
        assert items[0].status == "held"
        assert items[0].held_order_id == "o1"
        assert items[0].id == c.id

    def test_held_commitment_carries_no_pressure(self, tmp_path):
        self._register(tmp_path, "Ship the marketmesh data-sync fix", due_hours=-5)
        so.apply_to_ledger(tmp_path, [_order()])
        summary = cm.summarise(cm.load(tmp_path))
        assert summary.get("overdue", 0) == 0
        assert float(summary.get("pressure", 0.0) or 0.0) == 0.0

    def test_unrelated_commitment_untouched(self, tmp_path):
        self._register(tmp_path, "Publish the sailcoach launch post")
        parked, _ = so.apply_to_ledger(tmp_path, [_order()])
        assert parked == 0
        assert cm.load(tmp_path)[0].status == "open"

    def test_lift_revives_and_gives_runway(self, tmp_path):
        self._register(tmp_path, "Ship the marketmesh data-sync fix", due_hours=-100)
        so.apply_to_ledger(tmp_path, [_order()])
        parked, revived = so.apply_to_ledger(tmp_path, [])  # order lifted
        assert (parked, revived) == (0, 1)
        item = cm.load(tmp_path)[0]
        assert item.status == "open"
        assert item.held_order_id == ""
        due = datetime.fromisoformat(item.due_at)
        assert due > datetime.now(UTC)  # expired deadline got runway, not crisis

    def test_sweep_is_idempotent(self, tmp_path):
        self._register(tmp_path, "Ship the marketmesh data-sync fix")
        assert so.apply_to_ledger(tmp_path, [_order()]) == (1, 0)
        assert so.apply_to_ledger(tmp_path, [_order()]) == (0, 0)


class TestSpool:
    def test_issue_spools_json(self, tmp_path):
        path = so.spool(
            tmp_path,
            action="issue",
            text="Halt X",
            scope_type="product",
            scope_value="x",
            agent_name="ceo",
        )
        spec = json.loads(path.read_text(encoding="utf-8"))
        assert spec["action"] == "issue"
        assert spec["text"] == "Halt X"
        assert path.parent == tmp_path / "outbox" / "standing_orders"


class TestToolGating:
    def test_authorised_agent_gets_standing_order_tools(self):
        tools = tools_for_agent(
            "ceo",
            scheduling_authorised=set(),
            standing_order_authorised={"ceo", "coo"},
        )
        names = {t["function"]["name"] for t in tools}
        assert "issue_standing_order" in names
        assert "lift_standing_order" in names

    def test_unauthorised_agent_not_offered(self):
        tools = tools_for_agent(
            "willow",
            scheduling_authorised=set(),
            standing_order_authorised={"ceo", "coo"},
        )
        names = {t["function"]["name"] for t in tools}
        assert "issue_standing_order" not in names
