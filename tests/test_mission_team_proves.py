"""A condition the team can do and show for itself is the team's to prove.

A register that read "the founder has signed in" told five people for six
and a half days that nothing was theirs to do (Mini-2, 2026-11-12 to 11-19)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from cortiva.core import missions as ms

T0 = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)


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
