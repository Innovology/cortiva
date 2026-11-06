"""Challenge the source: a claim that reaches an agent is grounded by the
framework or lands in her unverified register; "delivered" is tested against
the agent's own definition of done; a manager reads what the systems say
moved, not what reports say (2026-11-06: "face value of information is
generally weak" — 46 sprints of green status and nothing live)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortiva.core import commitments as cm
from cortiva.core import grounding
from tests.test_fabric_extended import _make_fabric, _write_deploy

URL = "https://github.com/Innovology/innovology.io/issues/228"
PR = "https://github.com/Innovology/innovology.io/pull/48"


# --- grounding -----------------------------------------------------------


def _reader(**fields):
    def _read(ref, token, timeout=8.0):
        return {**ref, "status": "read", "kind": "pull request", "title": "x", **fields}

    return _read


def test_check_artifact_verdicts(monkeypatch) -> None:
    monkeypatch.setattr(grounding, "github_login", lambda token, timeout=8.0: "samantha-innovology")
    # Not a GitHub link → unknown, never a verdict.
    assert grounding.check_artifact("t", "docs/report.md")["ok"] is None
    # 404 with her own token → fails.
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {**ref, "status": "unreadable", "reason": "HTTP 404"},
    )
    out = grounding.check_artifact("t", PR)
    assert out["ok"] is False and "404" in out["reason"]
    # Someone else's PR → fails ("Land PR #48" was a July dependabot bump).
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        _reader(
            author="dependabot[bot]", state="closed", merged=True, created_at="2026-07-18T00:00:00Z"
        ),
    )
    out = grounding.check_artifact("t", PR, promised_at="2026-11-05T09:00:00+00:00")
    assert out["ok"] is False and "dependabot" in out["reason"] and "not you" in out["reason"]
    # Her own, but created before the promise → fails.
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        _reader(
            author="samantha-innovology",
            state="open",
            merged=False,
            created_at="2026-10-01T00:00:00Z",
        ),
    )
    out = grounding.check_artifact("t", PR, promised_at="2026-11-05T09:00:00+00:00")
    assert out["ok"] is False and "before this was promised" in out["reason"]
    # Opened earlier the SAME day (the promise was extracted from her text
    # after the work started) → holds. A day's grace; the false receipt this
    # guards against is months old.
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        _reader(
            author="samantha-innovology",
            state="open",
            merged=False,
            created_at="2026-11-05T02:00:00Z",
        ),
    )
    out = grounding.check_artifact("t", PR, promised_at="2026-11-05T21:00:00+00:00")
    assert out["ok"] is True, out
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        _reader(
            author="samantha-innovology",
            state="open",
            merged=False,
            created_at="2026-11-03T02:00:00Z",
        ),
    )
    out = grounding.check_artifact("t", PR, promised_at="2026-11-05T09:00:00+00:00")
    assert out["ok"] is False and "before this was promised" in out["reason"]
    # Her own, after the promise, merged → holds.
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        _reader(
            author="samantha-innovology",
            state="closed",
            merged=True,
            created_at="2026-11-05T22:00:00Z",
        ),
    )
    out = grounding.check_artifact("t", PR, promised_at="2026-11-05T09:00:00+00:00")
    assert out["ok"] is True and "merged" in out["reason"]


def test_prs_merged_since_is_output_not_activity(monkeypatch) -> None:
    def _get(url, token, timeout):
        if url.endswith("/user"):
            return {"login": "samantha-innovology"}
        assert "is%3Amerged" in url and "merged%3A%3E%3D2026-10-30" in url
        return {"total_count": 0}

    monkeypatch.setattr(grounding, "_github_get", _get)
    assert grounding.prs_merged_since("t", "2026-10-30") == 0
    assert grounding.prs_merged_since("", "2026-10-30") is None


# --- commitments: done means, and a checked artifact -----------------------


def test_done_means_is_the_agents_own_gate(tmp_path: Path) -> None:
    c = cm.register(
        tmp_path,
        to="alex@innovology.io",
        what="Lead form live",
        due="2026-11-12",
        done_means="live at innovology.io/contact and a test lead shows in the pipeline",
    )
    assert "live at" in cm.load(tmp_path)[0].done_means
    # Re-stating the same promise keeps the id and fills a missing definition.
    cm.register(tmp_path, to="alex@innovology.io", what="Lead form live", due="2026-11-13")
    assert [x.id for x in cm.load(tmp_path)] == [c.id]


def test_a_rejected_artifact_reopens_the_claim(tmp_path: Path) -> None:
    c = cm.register(tmp_path, to="maren@x", what="Land PR #48", due="2026-11-06")
    cm.update(tmp_path, commitment_id=c.id, delivered=True, artifact=PR)
    assert cm.load(tmp_path)[0].status == "delivered"
    cm.record_artifact_check(
        tmp_path, c.id, ok=False, detail=f"{PR} — authored by dependabot[bot], not you"
    )
    got = cm.load(tmp_path)[0]
    assert got.status == "open" and got.claimed_delivered_at and "not you" in got.artifact_check
    assert got.verification.startswith("artifact rejected")
    cm.record_artifact_check(
        tmp_path, c.id, ok=True, detail=f"{PR} — pull request merged, by samantha"
    )
    assert cm.load(tmp_path)[0].verification.startswith("artifact checked")


# --- fabric ------------------------------------------------------------------


def _org(tmp_path: Path):
    fabric = _make_fabric(tmp_path)
    agents_dir = tmp_path / "agents"
    _write_deploy(agents_dir, "ceo", name="Maren", department="exec", reports_to="human-founder")
    _write_deploy(agents_dir, "cto", name="Samantha", department="eng", reports_to="ceo")
    fabric.discover_agents()
    return fabric


@pytest.mark.asyncio
async def test_a_false_receipt_is_reopened_and_the_manager_sees_why(
    tmp_path: Path, monkeypatch
) -> None:
    fabric = _org(tmp_path)
    cto = fabric.agents["cto"]
    c = cm.register(cto.directory, to="maren@x", what="Land PR #48", due="2026-11-06")
    cm.update(cto.directory, commitment_id=c.id, delivered=True, artifact=PR)
    monkeypatch.setattr(
        grounding,
        "check_artifact",
        lambda token, url, promised_at="", timeout=8.0: {
            "ok": False,
            "reason": "authored by dependabot[bot], not you",
        },
    )
    await fabric._verify_artifact(cto, c.id, PR, c.created_at)
    got = cm.load(cto.directory)[0]
    assert got.status == "open" and "not you" in got.artifact_check
    block = fabric._reports_commitment_context(fabric.agents["ceo"])
    assert "NOT proven" in block and "not you" in block and "challenge" in block


@pytest.mark.asyncio
async def test_inbound_claims_carry_the_real_state(tmp_path: Path, monkeypatch) -> None:
    # The exact brief: "Issue #228 — Homeschool product line retirement".
    fabric = _org(tmp_path)
    cto = fabric.agents["cto"]
    inbox = cto.directory / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "m1.json").write_text(
        json.dumps(
            {
                "from": "maren@workforce.innovology.io",
                "subject": "PRIORITY NUMBER 1 — Issue #228 Homeschool Product Line Retirement",
                "text": f"Founder directive. Execute {URL}: retire all 9 Homeschool repos.",
            }
        )
    )
    (cto.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {
            **ref,
            "status": "read",
            "kind": "issue",
            "state": "open",
            "author": "alexrbrowne",
            "title": "Lead form + lead management pipeline",
            "excerpt": "",
        },
    )
    await fabric._ground_inbox(cto)
    block = fabric._email_inbox_context(cto)
    assert "Homeschool" in block
    assert (
        "📎 innovology.io#228 (issue, open, by alexrbrowne): Lead form + lead management pipeline"
        in block
    )
    assert "Believe the real state over the message" in block
    assert fabric._unverified_context(cto) == ""


@pytest.mark.asyncio
async def test_an_unreadable_claim_joins_the_unverified_register(
    tmp_path: Path, monkeypatch
) -> None:
    fabric = _org(tmp_path)
    ceo = fabric.agents["ceo"]  # no credentials → cannot read
    inbox = ceo.directory / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "m1.json").write_text(
        json.dumps(
            {
                "from": "astrid@workforce.innovology.io",
                "subject": "Done: #228",
                "text": f"Shipped {URL}",
            }
        )
    )
    await fabric._ground_inbox(ceo)
    block = fabric._email_inbox_context(ceo)
    assert "⛔ innovology.io#228 — you could NOT read this" in block
    drive = fabric._unverified_context(ceo)
    assert "Claims you could not verify" in drive
    assert "astrid@workforce.innovology.io" in drive and "Done: #228" in drive
    assert "**build it**" in drive
    # Access granted later → the link reads and the claim leaves the register.
    (ceo.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    (inbox / "m2.json").write_text(json.dumps({"from": "astrid@x", "subject": "re", "text": URL}))
    monkeypatch.setattr(
        grounding,
        "read_github_ref",
        lambda ref, token, timeout=8.0: {
            **ref,
            "status": "read",
            "kind": "issue",
            "state": "open",
            "title": "Lead form",
        },
    )
    await fabric._ground_inbox(ceo)
    assert fabric._unverified_context(ceo) == ""


@pytest.mark.asyncio
async def test_the_manager_board_reads_the_systems_not_the_reports(
    tmp_path: Path, monkeypatch
) -> None:
    fabric = _org(tmp_path)
    ceo, cto = fabric.agents["ceo"], fabric.agents["cto"]
    (cto.directory / "credentials.json").write_text('{"GH_TOKEN": "t"}')
    monkeypatch.setattr(
        grounding,
        "prs_opened_since",
        lambda token, since, timeout=8.0: [
            {
                "repo": "Innovology/innovology.io",
                "number": 229,
                "title": "ci",
                "state": "open",
                "url": "",
            }
        ],
    )
    monkeypatch.setattr(grounding, "prs_merged_since", lambda token, since, timeout=8.0: 0)
    c = cm.register(cto.directory, to="maren@x", what="Land PR #48", due="2026-11-06")
    cm.update(
        cto.directory, commitment_id=c.id, delivered=True, require_evidence=True
    )  # said done, no artifact
    await fabric._refresh_team_truth(ceo)
    board = fabric._team_truth_context(ceo)
    assert "What actually moved" in board
    assert (
        "**cto** — PRs opened today: 1 (Innovology/innovology.io#229) · merged last 7 days: 0"
        in board
    )
    assert "1 said-done-but-unproven" in board and 'unproven: "Land PR #48"' in board
    assert "ask for the artifact" in board
    # A report with no access reads as unknown, never zero.
    assert fabric._team_truth_context(cto) == ""  # no reports → no board
    (cto.directory / "credentials.json").unlink()
    fabric._shipped_today.clear()
    fabric._merged_week.clear()
    monkeypatch.setattr(grounding, "prs_opened_since", lambda token, since, timeout=8.0: None)
    monkeypatch.setattr(grounding, "prs_merged_since", lambda token, since, timeout=8.0: None)
    await fabric._refresh_team_truth(ceo)
    assert (
        "PRs today: unknown (no GitHub access) · merged last 7 days: unknown"
        in fabric._team_truth_context(ceo)
    )


def test_register_commitment_tool_asks_what_done_means() -> None:
    from cortiva.core.agent_tools import REGISTER_COMMITMENT_TOOL

    props = REGISTER_COMMITMENT_TOOL["function"]["parameters"]["properties"]
    assert "done_means" in props and "never 'I said I did it'" in props["done_means"]["description"]
