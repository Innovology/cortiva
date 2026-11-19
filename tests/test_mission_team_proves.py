"""A condition the team can do and show for itself is the team's to prove.

A register that read "the founder has signed in" told five people for six
and a half days that nothing was theirs to do (Mini-2, 2026-11-12 to 11-19)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cortiva.core import missions as ms

T0 = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)

_REAL_NETWORK = {
    "test_the_system_signs_in_with_the_credential_the_member_made",
    "test_a_way_in_that_answers_without_the_credential_shows_nothing",
}


@pytest.fixture(autouse=True)
def _nothing_there_until_it_is_done(request, monkeypatch):
    """An address shown without a reader answers 404: the thing is not done yet."""
    if request.node.name not in _REAL_NETWORK:
        monkeypatch.setattr(ms, "_default_fetch", lambda url, timeout, bearer="": (404, ""))


def _mission(base: str = "https://site.test") -> dict:
    return {
        "mission_id": "m-leads",
        "title": "Lead capture live",
        "issued_by": {"name": "Alex"},
        "issued_at": T0.isoformat(),
        "team": ["amara", "yuki", "cto"],
        "status": "active",
        "end_state": [
            {
                "id": "form",
                "describe": "The form is up",
                "check": {"url": f"{base}/contact", "status": 200},
            },
            {
                "id": "first-lead",
                "describe": "A real lead is in the pipeline and a member has seen it",
                "check": {"type": "team"},
            },
        ],
    }


def _write(tmp_path, m: dict) -> list[dict]:
    (tmp_path / ms.MISSIONS_FILE).write_text(json.dumps([m]))
    return ms.load(tmp_path)


def _world(pages: dict):
    def fetch(url: str, timeout: float):
        return pages.get(url, (404, ""))

    return fetch


PAGES = {
    "https://site.test/contact": (200, "form"),
    "https://site.test/api/leads/count": (200, '{"real": 2}'),
}
SAW = (
    "Signed in as myself, opened the pipeline and read the lead from a named person at a real firm."
)


def _lead(state: dict) -> dict:
    return state["m-leads"]["conditions"]["first-lead"]


def test_unproven_it_is_the_teams_and_nobody_waits_on_the_owner(tmp_path):
    missions = _write(tmp_path, _mission())
    assert ms.problems(missions[0]) == []
    state = ms.probe(tmp_path, fetch=_world(PAGES), now=T0)
    assert not _lead(state)["met"] and "the team's to prove" in _lead(state)["observed"]
    assert ms.team_has_work(missions, state)
    assert not ms.waits_on_owner(missions[0], state["m-leads"])
    assert "Nobody waits on Alex for this" in ms.brief(missions, state, now=T0)
    guide = ms.team_guide(missions, state)
    assert "cortiva mission prove" in guide and "`first-lead`" in guide


def test_shown_and_read_still_waits_for_a_second_member(tmp_path):
    missions = _write(tmp_path, _mission())
    ok, _ = ms.prove(
        tmp_path,
        missions,
        "amara",
        condition="first-lead",
        url="https://site.test/api/leads/count",
        path="real",
        now=T0,
    )
    assert ok
    state = ms.probe(tmp_path, fetch=_world(PAGES), now=T0)
    assert not _lead(state)["met"]
    assert "waits for a second member, not amara" in _lead(state)["observed"]


def test_the_one_who_showed_it_cannot_vouch_for_it(tmp_path):
    missions = _write(tmp_path, _mission())
    ms.prove(
        tmp_path,
        missions,
        "amara",
        condition="first-lead",
        url="https://site.test/api/leads/count",
        path="real",
    )
    ok, why = ms.witness(tmp_path, missions, "amara", condition="first-lead", saw=SAW)
    assert not ok and "second member" in why


def test_shown_by_one_and_seen_by_another_is_met(tmp_path):
    missions = _write(tmp_path, _mission())
    ms.prove(
        tmp_path,
        missions,
        "amara",
        condition="first-lead",
        url="https://site.test/api/leads/count",
        path="real",
    )
    ok, _ = ms.witness(tmp_path, missions, "yuki", condition="first-lead", saw=SAW)
    assert ok
    state = ms.probe(tmp_path, fetch=_world(PAGES), now=T0)
    assert _lead(state)["met"] and state["m-leads"]["complete"]
    assert "shown by amara, seen by yuki" in _lead(state)["observed"]
    assert ms.team_guide(missions, state) == ""


def test_a_witness_cannot_stand_in_for_what_the_system_reads(tmp_path):
    missions = _write(tmp_path, _mission())
    ms.prove(
        tmp_path,
        missions,
        "amara",
        condition="first-lead",
        url="https://site.test/api/leads/count",
        path="real",
    )
    ms.witness(tmp_path, missions, "yuki", condition="first-lead", saw=SAW)
    pages = {**PAGES, "https://site.test/api/leads/count": (200, '{"real": 0}')}
    state = ms.probe(tmp_path, fetch=_world(pages), now=T0)
    assert not _lead(state)["met"] and "wanted at least 1" in _lead(state)["observed"]


def test_proof_is_read_from_the_thing_itself_not_a_page_somebody_wrote(tmp_path):
    missions = _write(tmp_path, _mission())
    ok, why = ms.prove(
        tmp_path,
        missions,
        "amara",
        condition="first-lead",
        url="https://gist.githubusercontent.com/someone/abc/raw/count.json",
        path="real",
    )
    assert not ok and "site.test" in why
    assert ms.load_proofs(tmp_path) == {}


def test_a_tick_is_not_an_account_and_nothing_shown_cannot_be_seen(tmp_path):
    missions = _write(tmp_path, _mission())
    ok, why = ms.witness(tmp_path, missions, "yuki", condition="first-lead", saw=SAW)
    assert not ok and "nobody has shown" in why
    ms.prove(
        tmp_path,
        missions,
        "amara",
        condition="first-lead",
        url="https://site.test/api/leads/count",
        path="real",
    )
    ok, why = ms.witness(tmp_path, missions, "yuki", condition="first-lead", saw="looks good")
    assert not ok and "what you did and what you saw" in why


def test_showing_it_again_clears_who_had_seen_the_old_one(tmp_path):
    missions = _write(tmp_path, _mission())
    args = {"condition": "first-lead", "url": "https://site.test/api/leads/count", "path": "real"}
    ms.prove(tmp_path, missions, "amara", **args)
    ms.witness(tmp_path, missions, "yuki", condition="first-lead", saw=SAW)
    ms.prove(tmp_path, missions, "amara", **args)
    assert ms.load_proofs(tmp_path)["m-leads"]["first-lead"]["seen"] == []


def test_only_a_member_and_only_a_team_condition(tmp_path):
    missions = _write(tmp_path, _mission())
    args = {"url": "https://site.test/api/leads/count", "path": "real"}
    ok, why = ms.prove(tmp_path, missions, "andre", condition="first-lead", **args)
    assert not ok and "no such mission" in why
    ok, why = ms.prove(tmp_path, missions, "amara", condition="form", **args)
    assert not ok and "first-lead" in why


def test_the_owners_word_in_the_register_still_settles_it(tmp_path):
    m = _mission()
    m["end_state"][1]["check"]["confirmed_at"] = T0.isoformat()
    _write(tmp_path, m)
    state = ms.probe(tmp_path, fetch=_world(PAGES), now=T0)
    assert _lead(state)["met"] and "mission's owner" in _lead(state)["observed"]


class _Gated(BaseHTTPRequestHandler):
    open_to_all = False

    def do_GET(self):  # noqa: N802
        ok = self.open_to_all or self.headers.get("Authorization") == "Bearer s3cret"
        self.send_response(200 if ok else 302)
        self.send_header("Content-Type", "application/json")
        if not ok:
            self.send_header("Location", "/login")
        self.end_headers()
        if ok:
            self.wfile.write(b'{"real": 1}')

    def log_message(self, *_a):
        pass


def _serve(open_to_all: bool):
    handler = type("H", (_Gated,), {"open_to_all": open_to_all})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _gated_run(tmp_path, open_to_all: bool) -> dict:
    server = _serve(open_to_all)
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        m = _mission(base)
        m["end_state"] = m["end_state"][1:]
        m["route"] = {"url": f"{base}/health"}
        missions = _write(tmp_path, m)
        (tmp_path / "amara").mkdir()
        (tmp_path / "amara" / "leads.token").write_text("s3cret\n")
        ok, why = ms.prove(
            tmp_path,
            missions,
            "amara",
            condition="first-lead",
            url=f"{base}/admin/leads.json",
            path="real",
            token_file="leads.token",
        )
        assert ok, why
        ms.witness(tmp_path, missions, "yuki", condition="first-lead", saw=SAW)
        proofs = ms.load_proofs(tmp_path)
        assert "s3cret" not in json.dumps(proofs)
        met, observed = ms.run_team_check(
            missions[0], missions[0]["end_state"][0], proofs, tmp_path
        )
        return {"met": met, "observed": observed}
    finally:
        server.shutdown()


def test_the_system_signs_in_with_the_credential_the_member_made(tmp_path):
    seen = _gated_run(tmp_path, open_to_all=False)
    assert seen["met"], seen["observed"]


def test_a_way_in_that_answers_without_the_credential_shows_nothing(tmp_path):
    seen = _gated_run(tmp_path, open_to_all=True)
    assert not seen["met"] and "answers the same without" in seen["observed"]


def test_a_credential_file_outside_the_members_own_directory_is_refused(tmp_path):
    missions = _write(tmp_path, _mission())
    (tmp_path / "cto").mkdir()
    (tmp_path / "amara").mkdir()
    (tmp_path / "cto" / "t").write_text("x")
    ok, why = ms.prove(
        tmp_path,
        missions,
        "amara",
        condition="first-lead",
        url="https://site.test/api/leads/count",
        path="real",
        token_file="../cto/t",
    )
    assert not ok and "must be under" in why


def _owner_mission() -> dict:
    m = _mission()
    m["end_state"][1] = {
        "id": "founder-signin",
        "describe": "Alex has signed in to the lead pipeline",
        "check": {"type": "owner"},
    }
    return m


def test_the_registers_owner_makes_a_waiting_condition_the_teams(tmp_path):
    missions = _write(tmp_path, _owner_mission())
    state = ms.probe(tmp_path, fetch=_world(PAGES), now=T0)
    assert "cortiva mission amend" in ms.owner_guide(missions, state)
    ok, _ = ms.amend(
        tmp_path,
        "ceo",
        condition="founder-signin",
        prove_by="team",
        describe="A member of the team has signed in to the lead pipeline",
        now=T0,
    )
    assert ok
    missions = ms.load(tmp_path)
    cond = missions[0]["end_state"][1]
    assert cond["check"]["type"] == "team" and cond["describe"].startswith("A member")
    assert cond["amended"][0]["was"] == "Alex has signed in to the lead pipeline"
    state = ms.probe(tmp_path, fetch=_world(PAGES), now=T0)
    assert ms.team_has_work(missions, state) and ms.owner_guide(missions, state) == ""
    stamp = ms.status_stamp(missions, state)
    assert "changed by ceo" in stamp and "Alex has signed in" in stamp


def test_what_the_system_reads_itself_stays_as_issued(tmp_path):
    _write(tmp_path, _owner_mission())
    ok, why = ms.amend(tmp_path, "ceo", condition="form", describe="The form is somewhere, roughly")
    assert not ok and "stays as it was issued" in why
    ok, why = ms.amend(tmp_path, "ceo", condition="founder-signin", describe="ok")
    assert not ok and "full sentence" in why
    assert "amended" not in json.loads((tmp_path / ms.MISSIONS_FILE).read_text())[0]["end_state"][1]


def test_an_open_address_that_already_read_that_way_proves_nothing(tmp_path):
    """A member was told to show a health page that had said "ok" for a week
    as proof that someone had signed in (Mini-2, 2026-11-19)."""
    missions = _write(tmp_path, _mission())
    pages = dict(PAGES) | {"https://site.test/up": (200, '{"status": "ok"}')}
    ok, _ = ms.prove(
        tmp_path,
        missions,
        "amara",
        condition="first-lead",
        url="https://site.test/up",
        contains="ok",
        fetch=_world(pages),
    )
    assert ok
    ms.witness(tmp_path, missions, "yuki", condition="first-lead", saw=SAW)
    seen = _lead(ms.probe(tmp_path, fetch=_world(pages), now=T0))
    assert not seen["met"]
    assert "already read that way when it was shown" in seen["observed"]


def test_shown_before_it_is_done_and_done_after_is_met(tmp_path):
    missions = _write(tmp_path, _mission())
    before = dict(PAGES) | {"https://site.test/api/leads/count": (200, '{"real": 2}')}
    ms.prove(
        tmp_path,
        missions,
        "amara",
        condition="first-lead",
        url="https://site.test/api/leads/count",
        path="real",
        minimum=3,
        fetch=_world(before),
    )
    ms.witness(tmp_path, missions, "yuki", condition="first-lead", saw=SAW)
    assert not _lead(ms.probe(tmp_path, fetch=_world(before), now=T0))["met"]
    after = dict(PAGES) | {"https://site.test/api/leads/count": (200, '{"real": 3}')}
    assert _lead(ms.probe(tmp_path, fetch=_world(after), now=T0))["met"]


def test_whoever_issued_it_can_say_again_what_it_is_for(tmp_path):
    """Mini-2, 19 November 2026: written down as a form that works, meant as a lead to close."""
    m = _mission()
    m["intent"] = "A visitor can leave their details and the founder is told."
    _write(tmp_path, m)
    assert ms.restate(tmp_path, intent="more leads")[0] is False  # not a sentence
    meant = "A qualified lead captured by the website that the founder can attempt to close."
    ok, mid = ms.restate(tmp_path, intent=meant, by="Alex", now=T0)
    assert ok and mid == "m-leads"
    missions = ms.load(tmp_path)
    assert missions[0]["intent"] == meant
    assert missions[0]["restated"][0]["was"].startswith("A visitor can leave")
    # The conditions are as they were, and the team is told what they were written for.
    assert [c["id"] for c in missions[0]["end_state"]] == ["form", "first-lead"]
    for text in (ms.brief(missions, {}, now=T0), ms.context_block(missions, {}, now=T0)):
        assert meant in text
        assert "Alex restated what this mission is for" in text
        assert 'Before that it read: "A visitor can leave' in text
        assert "none has been reworded since" in text
    # The register's owner rewords a condition: that is stated too.
    said = "A qualified lead that nobody on the team submitted is in the pipeline"
    assert ms.amend(tmp_path, "ceo", condition="first-lead", describe=said)[0]
    assert "1 condition(s) have been reworded since" in ms.brief(ms.load(tmp_path), {}, now=T0)


def test_the_short_context_says_what_the_mission_is_for(tmp_path):
    m = _mission()
    m["intent"] = "A qualified lead the founder can attempt to close."
    text = ms.brief(_write(tmp_path, m), {}, now=T0)
    assert "What it is for, in Alex's words: A qualified lead the founder can attempt" in text
    assert "restated" not in text
