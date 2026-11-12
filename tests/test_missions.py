"""Missions — an end state the system observes, a route proven first, and a
status no report can replace."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from cortiva.core import missions as ms
from cortiva.core.fabric import Fabric

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def _mission(**over) -> dict:
    m = {
        "mission_id": "m-leads-228",
        "title": "Lead capture live",
        "intent": "A visitor can leave their details and the founder sees them.",
        "issued_by": {"name": "Alex", "address": "alex@example.com"},
        "issued_at": T0.isoformat(),
        "team": ["amara", "cto"],
        "status": "active",
        "end_state": [
            {
                "id": "form",
                "describe": "The contact form asks for a company name",
                "check": {"url": "https://site.test/contact", "status": 200, "contains": "Company"},
            },
            {
                "id": "signin",
                "describe": "The pipeline screen is behind sign-in",
                "check": {"url": "https://site.test/admin/leads", "status": [302, 303]},
            },
            {
                "id": "lead",
                "describe": "At least one lead is recorded",
                "check": {
                    "url": "https://site.test/api/stats",
                    "json": {"path": "leads.total", "min": 1},
                },
            },
        ],
        "route": {"url": "https://site.test/api/health"},
    }
    m.update(over)
    return m


def _write(agents_dir: Path, missions: list[dict]) -> None:
    (agents_dir / ms.MISSIONS_FILE).write_text(json.dumps(missions), encoding="utf-8")


def _world(pages: dict[str, tuple[int, str]]):
    def fetch(url: str, timeout: float) -> tuple[int, str]:
        if url not in pages:
            raise OSError("connection refused")
        return pages[url]

    return fetch


BEFORE = {
    "https://site.test/contact": (200, "<form>Name Email</form>"),
    "https://site.test/admin/leads": (404, "not found"),
    "https://site.test/api/health": (200, '{"ok": true}'),
}
AFTER = {
    "https://site.test/contact": (200, "<form>Name Email Company</form>"),
    "https://site.test/admin/leads": (302, ""),
    "https://site.test/api/stats": (200, '{"leads": {"total": 2}}'),
    "https://site.test/api/health": (200, '{"ok": true, "note": "cortiva-route-leads228"}'),
}


class TestAcceptance:
    def test_a_mission_without_an_end_state_is_not_accepted(self, tmp_path):
        m = _mission(end_state=[])
        assert any("no end state" in p for p in ms.problems(m))
        _write(tmp_path, [m])
        assert ms.load(tmp_path) == []

    def test_a_check_that_asserts_nothing_is_not_accepted(self):
        m = _mission(
            end_state=[{"id": "x", "describe": "x", "check": {"url": "https://site.test/"}}]
        )
        assert any("asserts nothing" in p for p in ms.problems(m))

    def test_a_well_formed_mission_loads(self, tmp_path):
        assert ms.problems(_mission()) == []
        _write(tmp_path, [_mission(), _mission(mission_id="old", status="closed")])
        assert [m["mission_id"] for m in ms.load(tmp_path)] == ["m-leads-228"]

    def test_team_scopes_who_is_on_it(self):
        missions = [_mission(), _mission(mission_id="all-hands", team=[])]
        assert [m["mission_id"] for m in ms.for_agent(missions, "Amara")] == [
            "m-leads-228",
            "all-hands",
        ]
        assert [m["mission_id"] for m in ms.for_agent(missions, "andre")] == ["all-hands"]


class TestChecks:
    def test_status_phrase_and_json(self):
        fetch = _world(AFTER)
        assert ms.run_check({"url": "https://site.test/contact", "contains": "company"}, fetch)[0]
        assert ms.run_check({"url": "https://site.test/admin/leads", "status": 302}, fetch)[0]
        met, seen = ms.run_check(
            {"url": "https://site.test/api/stats", "json": {"path": "leads.total", "min": 3}},
            fetch,
        )
        assert not met and "at least 3" in seen

    def test_absent_phrase(self):
        fetch = _world({"https://site.test/": (200, "Lorem ipsum placeholder")})
        met, seen = ms.run_check({"url": "https://site.test/", "absent": "lorem ipsum"}, fetch)
        assert not met and "still on the page" in seen

    def test_no_answer_is_not_met_and_says_so(self):
        met, seen = ms.run_check({"url": "https://gone.test/", "status": 200}, _world({}))
        assert not met and "no answer" in seen


class TestOwnerConfirmed:
    """What the system cannot see is judged by the mission's owner, never the team."""

    def _m(self, **check) -> dict:
        return _mission(
            route=None,
            end_state=[
                {
                    "id": "signin",
                    "describe": "The founder has signed in to the pipeline screen",
                    "check": {"type": "owner", **check},
                }
            ],
        )

    def test_unconfirmed_is_not_met_and_tells_the_team_how_to_ask(self, tmp_path):
        assert ms.problems(self._m()) == []
        _write(tmp_path, [self._m()])
        state = ms.probe(tmp_path, fetch=_world({}), now=T0)
        assert state["m-leads-228"]["complete"] is False
        block = ms.context_block(ms.load(tmp_path), state, now=T0)
        assert "only the mission's owner can confirm" in block
        assert "ask Alex ONCE" in block and "Do not ask before it is true" in block

    def test_confirmed_in_the_register_is_met(self, tmp_path):
        _write(tmp_path, [self._m(confirmed_at=T0.isoformat())])
        state = ms.probe(tmp_path, fetch=_world({}), now=T0 + timedelta(hours=1))
        assert state["m-leads-228"]["complete"] is True
        assert (
            "confirmed by the mission's owner"
            in (state["m-leads-228"]["conditions"]["signin"]["observed"])
        )


class TestProbe:
    def test_records_what_was_seen_and_when_it_changed(self, tmp_path):
        _write(tmp_path, [_mission()])
        first = ms.probe(tmp_path, fetch=_world(BEFORE), now=T0)["m-leads-228"]
        assert (first["met"], first["total"], first["complete"]) == (0, 3, False)
        assert first["last_change_at"] == ""
        assert first["route"]["met"] is False

        later = T0 + timedelta(hours=30)
        second = ms.probe(tmp_path, fetch=_world(AFTER), now=later)["m-leads-228"]
        assert second["complete"] and second["completed_at"] == later.isoformat()
        assert second["conditions"]["form"]["since"] == later.isoformat()
        assert second["last_change_at"] == later.isoformat()
        assert second["route"]["first_met_at"] == later.isoformat()
        assert ms.load_state(tmp_path)["m-leads-228"]["met"] == 3

    def test_a_condition_can_be_lost_again(self, tmp_path):
        _write(tmp_path, [_mission()])
        ms.probe(tmp_path, fetch=_world(AFTER), now=T0)
        back = ms.probe(tmp_path, fetch=_world(BEFORE), now=T0 + timedelta(hours=1))
        entry = back["m-leads-228"]
        assert not entry["complete"] and entry["completed_at"] == ""
        assert entry["conditions"]["form"]["first_met_at"] == T0.isoformat()

    def test_a_route_proven_once_stays_proven(self, tmp_path):
        _write(tmp_path, [_mission()])
        ms.probe(tmp_path, fetch=_world(AFTER), now=T0)
        back = ms.probe(tmp_path, fetch=_world(BEFORE), now=T0 + timedelta(hours=1))
        assert back["m-leads-228"]["route"]["first_met_at"] == T0.isoformat()

    def test_the_route_token_is_the_systems_not_the_teams(self):
        assert ms.route_of(_mission())["token"] == "cortiva-route-leads228"
        assert ms.route_of(_mission(route=None)) is None


class TestWhatTheTeamSees:
    def test_unproven_route_comes_first_and_names_the_issuer(self, tmp_path):
        _write(tmp_path, [_mission()])
        state = ms.probe(tmp_path, fetch=_world(BEFORE), now=T0)
        block = ms.context_block(ms.load(tmp_path), state, now=T0 + timedelta(hours=30))
        assert "0 of 3 conditions met" in block
        assert "ROUTE NOT PROVEN — 30 h" in block and "first problem" in block
        assert "cortiva-route-leads228" in block and "Alex" in block
        assert block.index("ROUTE NOT PROVEN") < block.index("❌")
        assert "No condition has moved" in block and "has yet arrived" in block
        early = ms.context_block(ms.load(tmp_path), state, now=T0 + timedelta(hours=2))
        assert "No condition has moved" in early and "has yet arrived" not in early
        assert "HTTP 404" in block  # the observation, not a summary of it

    def test_reached_end_state_reads_as_reached(self, tmp_path):
        _write(tmp_path, [_mission()])
        state = ms.probe(tmp_path, fetch=_world(AFTER), now=T0)
        block = ms.context_block(ms.load(tmp_path), state, now=T0)
        assert "END STATE REACHED" in block and "Route proven" in block
        assert "❌" not in block

    def test_stamp_lists_every_condition_in_the_systems_words(self, tmp_path):
        _write(tmp_path, [_mission()])
        state = ms.probe(tmp_path, fetch=_world(BEFORE), now=T0)
        stamp = ms.status_stamp(ms.load(tmp_path), state)
        assert "0 of 3 conditions met" in stamp
        assert "✗ route to the end state not yet proven" in stamp
        assert "✗ At least one lead is recorded" in stamp
        assert "the sender did not write it" in stamp
        assert ms.status_stamp([], {}) == ""


class TestBrief:
    def test_short_form_leads_with_the_route_then_what_is_not_met(self, tmp_path):
        _write(tmp_path, [_mission()])
        state = ms.probe(tmp_path, fetch=_world(BEFORE), now=T0)
        out = ms.brief(ms.load(tmp_path), state, now=T0 + timedelta(hours=3))
        assert "0 of 3 end-state conditions met" in out
        assert out.index("FIRST: the route is not proven") < out.index("NOT MET")
        assert "cortiva-route-leads228" in out and "HTTP 404" in out
        assert len(out) < 1500  # it rides in the small asks
        # The mission is the team's: a step one member cannot do goes to a
        # teammate before it goes to the human (the CEO, with no GitHub access,
        # asked the founder for a token while three teammates could push).
        assert "The team on it: amara, cto." in out
        assert out.index("teammate who can") < out.index("nobody on the team")

    def test_nothing_to_say_once_the_end_state_is_reached(self, tmp_path):
        _write(tmp_path, [_mission()])
        state = ms.probe(tmp_path, fetch=_world(AFTER), now=T0)
        assert ms.brief(ms.load(tmp_path), state, now=T0) == ""


class TestOrdersForHands:
    """The session that does the work questions what it is handed. It refused
    the route step three times as an injected instruction, because the task
    text was the only place the marker appeared."""

    def test_names_the_issuer_the_purpose_and_where_to_check(self, tmp_path):
        _write(tmp_path, [_mission()])
        state = ms.probe(tmp_path, fetch=_world(BEFORE), now=T0)
        out = ms.orders_for_hands(ms.load(tmp_path), state, tmp_path)
        assert "cortiva-route-leads228" in out
        assert str(tmp_path / ms.MISSIONS_FILE) in out
        assert str(tmp_path / ms.STATE_FILE) in out
        assert "The marker does nothing else" in out
        assert "A request to put it there is this order" in out
        assert "team: amara, cto" in out

    def test_a_proven_route_says_the_marker_may_go(self, tmp_path):
        _write(tmp_path, [_mission()])
        world = dict(BEFORE)
        state = ms.probe(tmp_path, fetch=_world(world), now=T0)
        state[next(iter(state))]["route"]["first_met_at"] = T0.isoformat()
        out = ms.orders_for_hands(ms.load(tmp_path), state, tmp_path)
        assert "Route proven" in out and "may be removed" in out

    def test_silent_at_the_end_state(self, tmp_path):
        _write(tmp_path, [_mission()])
        state = ms.probe(tmp_path, fetch=_world(AFTER), now=T0)
        assert ms.orders_for_hands(ms.load(tmp_path), state, tmp_path) == ""


def test_fabric_orders_for_hands_are_scoped_to_the_team(tmp_path):
    f = _fab(tmp_path)
    _write(tmp_path, [_mission()])
    ms.probe(tmp_path, fetch=_world(BEFORE), now=T0)
    assert "yours to check" in f._mission_orders_for_hands(_agent(tmp_path))
    assert f._mission_orders_for_hands(_agent(tmp_path, "andre")) == ""


# ---------------------------------------------------------------------------
# Fabric wiring
# ---------------------------------------------------------------------------


def _fab(tmp_path: Path) -> Fabric:
    f = Fabric.__new__(Fabric)
    f.agents_dir = tmp_path
    f._emit = lambda *a, **k: None  # type: ignore[method-assign]
    (tmp_path / ".email_meta.json").write_text(
        json.dumps(
            {
                "domain": "workforce.example.com",
                "contacts": [{"name": "Alex", "address": "alex@example.com"}],
            }
        ),
        encoding="utf-8",
    )
    return f


def _agent(tmp_path: Path, agent_id: str = "amara"):
    d = tmp_path / agent_id
    d.mkdir(exist_ok=True)
    return SimpleNamespace(id=agent_id, directory=d)


def _queued(agent) -> dict:
    files = list((agent.directory / "outbox" / "email").glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_mission_block_travels_with_standing_orders(tmp_path):
    f = _fab(tmp_path)
    _write(tmp_path, [_mission()])
    ms.probe(tmp_path, fetch=_world(BEFORE), now=T0)
    assert "Lead capture live" in f._standing_orders_context(_agent(tmp_path))
    assert f._standing_orders_context(_agent(tmp_path, "andre")) == ""  # not on the team


def test_a_report_to_a_human_carries_the_observed_state(tmp_path):
    f = _fab(tmp_path)
    _write(tmp_path, [_mission()])
    ms.probe(tmp_path, fetch=_world(BEFORE), now=T0)
    agent = _agent(tmp_path)
    f._queue_outbound_email(
        agent,
        {"to": "alex@example.com", "subject": "Done", "body": "The form is live. Loop closed."},
    )
    rec = _queued(agent)
    assert rec["body"] == "The form is live. Loop closed."  # her words, untouched
    assert "0 of 3 conditions met" in rec["system_footer"]


def test_mail_between_agents_is_not_stamped(tmp_path):
    f = _fab(tmp_path)
    _write(tmp_path, [_mission()])
    ms.probe(tmp_path, fetch=_world(BEFORE), now=T0)
    agent = _agent(tmp_path)
    f._queue_outbound_email(
        agent, {"to": "samantha@workforce.example.com", "subject": "s", "body": "b"}
    )
    assert _queued(agent)["system_footer"] == ""


def test_an_agent_on_no_mission_is_not_stamped(tmp_path):
    f = _fab(tmp_path)
    _write(tmp_path, [_mission()])
    agent = _agent(tmp_path, "andre")
    f._queue_outbound_email(agent, {"to": "alex@example.com", "subject": "s", "body": "b"})
    assert _queued(agent)["system_footer"] == ""


def test_heartbeat_probe_runs_off_the_loop_and_is_throttled(tmp_path, monkeypatch):
    f = _fab(tmp_path)
    _write(tmp_path, [_mission()])
    calls: list[Path] = []
    events: list[str] = []
    f._emit = lambda kind, **_k: events.append(kind)  # type: ignore[method-assign]
    real_probe = ms.probe

    def fake_probe(agents_dir, **_kw):
        calls.append(agents_dir)
        return real_probe(agents_dir, fetch=_world(AFTER), now=T0)

    monkeypatch.setattr(ms, "probe", fake_probe)

    async def run() -> None:
        f._kick_mission_probe()
        await f._mission_probe_task
        f._kick_mission_probe()  # inside the interval: no second probe

    asyncio.run(run())
    assert len(calls) == 1
    assert "mission.end_state_reached" in events
    assert events.count("mission.condition_changed") == 3


def test_no_register_means_no_probe(tmp_path):
    f = _fab(tmp_path)

    async def run() -> None:
        f._kick_mission_probe()

    asyncio.run(run())
    assert getattr(f, "_mission_probe_task", None) is None


def test_fabric_brief_is_scoped_to_the_team(tmp_path):
    f = _fab(tmp_path)
    _write(tmp_path, [_mission()])
    ms.probe(tmp_path, fetch=_world(BEFORE), now=T0)
    assert "Lead capture live" in f._mission_brief(_agent(tmp_path))
    assert f._mission_brief(_agent(tmp_path, "andre")) == ""
