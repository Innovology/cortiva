"""Durable GitHub-feedback register (fabric).

GitHub notifications are feedback on the agent's OWN work (PR approved, CI
failed, review comment). The read-once inbox showed each one in a single wake
then archived it, with a ``[:10]`` cap silently dropping the rest — so an
approval ("merge me") evaporated before the agent acted, and feedback loops
never closed. These tests pin the fix: a durable register that stays salient
until the thread merges/closes (or a TTL backstop fires).
"""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from cortiva.core.fabric import Fabric, _github_ref


def _gh(subject: str, text: str = "") -> dict:
    return {"from": "notifications@github.com", "subject": subject, "text": text}


def _shim():
    """A minimal stand-in carrying only what the register methods touch."""
    tmp = Path(tempfile.mkdtemp())
    agent = SimpleNamespace(directory=tmp)
    fab = SimpleNamespace(_GITHUB_FEEDBACK_TTL_DAYS=Fabric._GITHUB_FEEDBACK_TTL_DAYS)
    rec = lambda msgs: Fabric._record_github_feedback(fab, agent, msgs)  # noqa: E731
    opn = lambda: Fabric._open_github_feedback(fab, agent)  # noqa: E731
    return agent, rec, opn


def test_github_ref_folds_a_thread_by_pr_number():
    assert _github_ref("[Innovology/repo] Fix (PR #104)") == "Innovology/repo#104"
    assert _github_ref("Re: [Innovology/repo] Fix (Issue #80)") == "Innovology/repo#80"
    # CI mail with no number is still keyed (by subject), just not thread-folded.
    assert _github_ref("[Innovology/repo] PR run failed: CI (0c00e66)")


def test_feedback_survives_a_wake_with_no_new_mail():
    """The core fix: an approval doesn't evaporate after one wake."""
    _agent, rec, opn = _shim()
    rec([_gh("[Innovology/repo] adr-lint (PR #129)", "samantha approved these changes.")])
    assert len(opn()) == 1
    # Next wake, nothing new arrives — it MUST still be there.
    assert len(opn()) == 1


def test_same_thread_folds_and_counts_no_duplicates():
    _agent, rec, opn = _shim()
    rec([_gh("[Innovology/repo] adr-lint (PR #129)", "approved")])
    rec([_gh("Re: [Innovology/repo] adr-lint (PR #129)", "another comment")])
    items = opn()
    assert len(items) == 1
    assert items[0]["count"] == 2


def test_merge_notification_resolves_the_thread():
    _agent, rec, opn = _shim()
    rec([_gh("[Innovology/repo] adr-lint (PR #129)", "approved")])
    rec([_gh("Re: [Innovology/repo] adr-lint (PR #129)", "samantha merged this pull request into main.")])
    assert opn() == []


def test_close_notification_resolves_the_thread():
    _agent, rec, opn = _shim()
    rec([_gh("[Innovology/repo] silent failure (Issue #102)", "reported")])
    rec([_gh("Re: [Innovology/repo] silent failure (Issue #102)", "alex closed this as completed.")])
    assert opn() == []


def test_ttl_backstop_expires_a_never_merged_item():
    agent, _rec, opn = _shim()
    (agent.directory / "github_feedback.json").write_text(
        json.dumps(
            [
                {
                    "ref": "repo#1",
                    "subject": "stale PR",
                    "opened_at": "2020-01-01T00:00:00+00:00",
                    "last_seen": "2020-01-01T00:00:00+00:00",
                    "count": 1,
                    "status": "open",
                }
            ]
        )
    )
    assert opn() == []


def test_render_block_lists_open_threads_and_states_overflow():
    agent, rec, _opn = _shim()
    rec([_gh(f"[Innovology/repo] item (PR #{n})", "comment") for n in range(20)])
    fab = SimpleNamespace(
        _GITHUB_FEEDBACK_TTL_DAYS=Fabric._GITHUB_FEEDBACK_TTL_DAYS,
        _open_github_feedback=lambda a: Fabric._open_github_feedback(
            SimpleNamespace(_GITHUB_FEEDBACK_TTL_DAYS=Fabric._GITHUB_FEEDBACK_TTL_DAYS), a
        ),
    )
    block = Fabric._github_feedback_context(fab, agent)
    assert "open feedback on YOUR work" in block
    # No silent cap — overflow beyond the rendered 15 is stated, not dropped.
    assert "5 more open" in block
