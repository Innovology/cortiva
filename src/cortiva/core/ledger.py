"""What on GitHub waits on one person, read from GitHub with their own token.

A pull request moves when the person it waits on acts. Mail tells them once;
after that the state sits on GitHub and nothing reads it. Five people stopped
for four and a half hours with three pull requests open, each waiting on one
of them (Mini-2, 2026-11-18). This reads the state and names the step owed.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from cortiva.core import grounding

API = "https://api.github.com"
# Reviews that decide something; a comment leaves the earlier decision standing.
_DECIDING = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
# Closest to landing first: a merge, then what unblocks a colleague, then one's own repairs.
_ORDER = ("merge", "review_again", "review", "conflicts", "changes", "checks")


def _search(query: str, token: str, timeout: float, limit: int) -> list[tuple[str, int]]:
    q = urllib.parse.quote(query)
    data = grounding._github_get(
        f"{API}/search/issues?q={q}&sort=updated&order=desc&per_page={limit}", token, timeout
    )
    out: list[tuple[str, int]] = []
    for item in data.get("items") or []:
        repo = "/".join(str(item.get("repository_url") or "").split("/")[-2:])
        number = int(item.get("number") or 0)
        if repo and number:
            out.append((repo, number))
    return out


def read_pull(repo: str, number: int, token: str, timeout: float = 8.0) -> dict[str, Any]:
    """One pull request as it stands: who wrote it, each reviewer's standing
    decision, when its head was pushed, and which checks fail on that head."""
    base = f"{API}/repos/{repo}"
    pull = grounding._github_get(f"{base}/pulls/{number}", token, timeout)
    sha = str((pull.get("head") or {}).get("sha") or "")
    decisions: dict[str, tuple[str, str]] = {}
    for r in grounding._github_get(f"{base}/pulls/{number}/reviews?per_page=100", token, timeout):
        state = str(r.get("state") or "")
        who = str((r.get("user") or {}).get("login") or "")
        if who and state in _DECIDING:
            decisions[who] = (state, str(r.get("submitted_at") or ""))
    pushed = ""
    failing: list[str] = []
    running = False
    if sha:
        commit = grounding._github_get(f"{base}/commits/{sha}", token, timeout)
        pushed = str(((commit.get("commit") or {}).get("committer") or {}).get("date") or "")
        runs = grounding._github_get(
            f"{base}/commits/{sha}/check-runs?per_page=100", token, timeout
        )
        for c in runs.get("check_runs") or []:
            if c.get("status") != "completed":
                running = True
            elif c.get("conclusion") in ("failure", "timed_out", "cancelled"):
                name = str(c.get("name") or "")
                if name and name not in failing:
                    failing.append(name)
    return {
        "repo": repo,
        "number": number,
        "title": " ".join(str(pull.get("title") or "").split())[:70],
        "author": str((pull.get("user") or {}).get("login") or ""),
        "draft": bool(pull.get("draft")),
        "requested": [str(u.get("login") or "") for u in pull.get("requested_reviewers") or []],
        "mergeable_state": str(pull.get("mergeable_state") or ""),
        "decisions": decisions,
        "pushed": pushed,
        "failing": failing,
        "running": running,
    }


def owed(pull: dict[str, Any], login: str) -> tuple[str, str] | None:
    """The step this pull request waits on from ``login``, as (kind, item),
    or ``None`` when it waits on someone else."""
    me = login.lower()
    ref = f"{pull['repo']}#{pull['number']}"
    author = str(pull["author"])
    decisions = {k.lower(): v for k, v in pull["decisions"].items()}
    if pull["draft"]:
        return None
    if author.lower() != me:
        # A reviewer waits for a head that can land: the author's repair comes first.
        if pull["failing"] or pull["running"] or pull["mergeable_state"] == "dirty":
            return None
        mine = decisions.get(me)
        asked = me in {r.lower() for r in pull["requested"]}
        whose = f"({author}'s: {pull['title']})"
        again = (
            "review_again",
            f"{ref} {whose} changed since your review: review it again on GitHub and "
            "approve it if your points are met",
        )
        # Their own request for changes still stands against an older head:
        # it blocks the merge until they look again, whoever else approves.
        if mine is not None and mine[0] == "CHANGES_REQUESTED" and pull["pushed"] > mine[1]:
            return again
        # One review of the head as it stands is enough. A second person asked
        # to review what a colleague has already decided on is a queue, not a check.
        decided = any(
            who != me and state in ("APPROVED", "CHANGES_REQUESTED") and at >= pull["pushed"]
            for who, (state, at) in decisions.items()
        )
        if not asked or decided:
            return None
        if mine is not None:
            return again
        return (
            "review",
            f"{ref} {whose} waits for your review: review it on GitHub and approve it "
            "or ask for changes",
        )
    if pull["mergeable_state"] == "dirty":
        return (
            "conflicts",
            f"{ref} is yours and conflicts with the main branch on GitHub: bring it up to "
            "date, resolve the conflicts and push",
        )
    if pull["failing"]:
        names = "; ".join(pull["failing"][:4])
        return (
            "checks",
            f"{ref} is yours and cannot land while its checks fail on GitHub ({names}): "
            "repair them and push",
        )
    standing = [
        (who, at)
        for who, (state, at) in pull["decisions"].items()
        if state == "CHANGES_REQUESTED" and at >= pull["pushed"]
    ]
    if standing:
        who = standing[0][0]
        return (
            "changes",
            f"{ref} is yours and {who} asked for changes: make them, push, and ask {who} "
            "on GitHub to review again",
        )
    approved = any(state == "APPROVED" for state, _ in pull["decisions"].values())
    refused = any(state == "CHANGES_REQUESTED" for state, _ in pull["decisions"].values())
    if approved and not refused and not pull["running"] and pull["mergeable_state"] == "clean":
        return ("merge", f"{ref} is yours, approved and green: merge it on GitHub")
    return None


def waiting_on(token: str, *, limit: int = 8, timeout: float = 8.0) -> list[dict[str, Any]] | None:
    """Every open pull request that waits on the token's own user, nearest to
    landing first. Each is ``{key, kind, repo, number, item}``. ``None`` when
    it cannot be known (no token, GitHub unreachable), which is different
    from nothing waiting."""
    login = grounding.github_login(token, timeout)
    if not login:
        return None
    try:
        refs: list[tuple[str, int]] = []
        for query in (
            f"is:open is:pr archived:false review-requested:{login}",
            f"is:open is:pr archived:false author:{login}",
            f"is:open is:pr archived:false reviewed-by:{login} -author:{login}",
        ):
            for ref in _search(query, token, timeout, limit):
                if ref not in refs:
                    refs.append(ref)
        out: list[dict[str, Any]] = []
        for repo, number in refs[: limit * 2]:
            step = owed(read_pull(repo, number, token, timeout), login)
            if step:
                out.append(
                    {
                        "key": f"{repo}#{number}:{step[0]}",
                        "kind": step[0],
                        "repo": repo,
                        "number": number,
                        "item": step[1],
                    }
                )
    except Exception:
        return None
    out.sort(key=lambda x: _ORDER.index(x["kind"]))
    return out


def involved(repo: str, number: int | str, login: str, token: str, timeout: float = 8.0) -> bool:
    """True when ``login`` is asked to review this pull request or has
    reviewed it. Such a person may be the one who lands it."""
    try:
        base = f"{API}/repos/{repo}/pulls/{number}"
        pull = grounding._github_get(base, token, timeout)
        people = [str(u.get("login") or "") for u in pull.get("requested_reviewers") or []]
        people += [str(u.get("login") or "") for u in pull.get("assignees") or []]
        for r in grounding._github_get(f"{base}/reviews?per_page=100", token, timeout):
            people.append(str((r.get("user") or {}).get("login") or ""))
    except Exception:
        return False
    return login.lower() in {p.lower() for p in people if p}
