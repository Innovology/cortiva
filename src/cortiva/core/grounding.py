"""Ground a directive in what it actually points at.

A directive that names a resource the agent cannot read is the most dangerous
input an agent gets: the ask is top-of-mind, the content is missing, and the
model fills the gap from memory. On 2026-11-05 the CEO turned an unreadable
"focus all attention on innovology.io#228" into a nine-repo retirement and
briefed two people on it. She had no GitHub credentials at all.

So references are resolved when a directive is recorded: a readable issue or
PR contributes its real title and body to the directive; an unreadable one is
recorded as unreadable, with the reason, so the agent is told to get the
content rather than guess it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

GITHUB_REF_RE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:issues|pull)/(?P<number>\d+)"
)

MAX_REFS_PER_DIRECTIVE = 5
EXCERPT_CHARS = 1500


def extract_github_refs(text: str) -> list[dict[str, Any]]:
    """Issue/PR links in *text*, deduplicated, in order of appearance."""
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in GITHUB_REF_RE.finditer(text or ""):
        url = m.group(0)
        if url in seen:
            continue
        seen.add(url)
        refs.append(
            {
                "url": url,
                "owner": m.group("owner"),
                "repo": m.group("repo"),
                "number": int(m.group("number")),
                "status": "pending",
            }
        )
        if len(refs) >= MAX_REFS_PER_DIRECTIVE:
            break
    return refs


def agent_github_token(agent_dir: Path) -> str:
    """The agent's own delegated GitHub token, if the management layer gave one."""
    try:
        creds = json.loads((agent_dir / "credentials.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(creds, dict):
        return ""
    return str(creds.get("GH_TOKEN") or creds.get("GITHUB_TOKEN") or "")


def read_github_ref(ref: dict[str, Any], token: str, timeout: float = 8.0) -> dict[str, Any]:
    """Resolve one reference. Returns the ref with ``status`` set to ``read``
    (plus title/state/excerpt) or ``unreadable`` (plus reason). Never raises."""
    import urllib.error
    import urllib.request

    out = dict(ref)
    if not token:
        out.update(status="unreadable", reason="this agent has no GitHub credentials")
        return out
    api = f"https://api.github.com/repos/{ref['owner']}/{ref['repo']}/issues/{ref['number']}"
    req = urllib.request.Request(
        api,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "cortiva-fabric",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        reason = {
            401: "GitHub rejected this agent's credentials (HTTP 401)",
            403: "this agent's credentials are not allowed to read it (HTTP 403)",
            404: "not found with this agent's access (HTTP 404) — private, or no access",
        }.get(e.code, f"GitHub returned HTTP {e.code}")
        out.update(status="unreadable", reason=reason)
        return out
    except Exception as e:  # network, timeout, bad JSON
        out.update(status="unreadable", reason=f"could not reach GitHub ({type(e).__name__})")
        return out
    body = str(data.get("body") or "").strip()
    pr = data.get("pull_request") or {}
    out.update(
        status="read",
        title=str(data.get("title") or ""),
        state=str(data.get("state") or ""),
        excerpt=body[:EXCERPT_CHARS],
        kind="pull request" if data.get("pull_request") else "issue",
        author=str((data.get("user") or {}).get("login") or ""),
        created_at=str(data.get("created_at") or ""),
        merged=bool(pr.get("merged_at")) if isinstance(pr, dict) else False,
    )
    return out


def _github_get(url: str, token: str, timeout: float) -> Any:
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "cortiva-fabric",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def prs_opened_since(token: str, since: str, timeout: float = 8.0) -> list[dict[str, Any]] | None:
    """Pull requests the token's own user opened on or after ``since``
    (YYYY-MM-DD), newest first. ``None`` when it cannot be known (no token,
    GitHub unreachable) — which is different from "none opened"."""
    import urllib.parse

    if not token:
        return None
    try:
        login = str(_github_get("https://api.github.com/user", token, timeout).get("login") or "")
        if not login:
            return None
        q = urllib.parse.quote(f"author:{login} type:pr created:>={since}")
        data = _github_get(
            f"https://api.github.com/search/issues?q={q}&sort=created&order=desc&per_page=20",
            token,
            timeout,
        )
    except Exception:
        return None
    out: list[dict[str, Any]] = []
    for item in data.get("items") or []:
        repo = "/".join(str(item.get("repository_url") or "").split("/")[-2:])
        out.append(
            {
                "repo": repo,
                "number": int(item.get("number") or 0),
                "title": str(item.get("title") or ""),
                "state": str(item.get("state") or ""),
                "url": str(item.get("html_url") or ""),
            }
        )
    return out


def github_login(token: str, timeout: float = 8.0) -> str:
    """The login the token belongs to; '' when it cannot be known."""
    if not token:
        return ""
    try:
        return str(_github_get("https://api.github.com/user", token, timeout).get("login") or "")
    except Exception:
        return ""


def prs_merged_since(token: str, since: str, timeout: float = 8.0) -> int | None:
    """How many pull requests the token's own user has had MERGED on or after
    ``since`` (YYYY-MM-DD). ``None`` when it cannot be known. This is output,
    not activity: opened PRs are a claim, merged ones landed."""
    import urllib.parse

    login = github_login(token, timeout)
    if not login:
        return None
    try:
        q = urllib.parse.quote(f"author:{login} type:pr is:merged merged:>={since}")
        data = _github_get(f"https://api.github.com/search/issues?q={q}&per_page=1", token, timeout)
    except Exception:
        return None
    try:
        return int(data.get("total_count") or 0)
    except (TypeError, ValueError):
        return None


def _day(iso: str) -> Any:
    from datetime import date

    try:
        return date.fromisoformat(str(iso)[:10])
    except ValueError:
        return None


def check_artifact(
    token: str, url: str, promised_at: str = "", timeout: float = 8.0
) -> dict[str, Any]:
    """Test a 'delivered' artifact against GitHub. Returns ``{"ok": bool,
    "reason": str, ...ref fields}``. Only GitHub issue/PR URLs are checkable
    here; anything else returns ``ok=None`` (unknown), never a verdict.

    A claim fails when the link does not resolve with the claimant's own
    token, when the thing is not theirs, or when it predates the promise
    (2026-11-05: "Land PR #48" — innovology.io#48 was a July dependabot
    bump) — the three ways a real-looking locator has been a false receipt.
    """
    refs = extract_github_refs(url or "")
    if not refs:
        return {"ok": None, "reason": "not a GitHub issue/PR link — not checkable here"}
    ref = read_github_ref(refs[0], token, timeout=timeout)
    if ref.get("status") != "read":
        return {"ok": False, "reason": f"could not read it ({ref.get('reason')})", **ref}
    login = github_login(token, timeout)
    if login and ref.get("author") and ref["author"].lower() != login.lower():
        return {"ok": False, "reason": f"authored by {ref['author']}, not you", **ref}
    created, promised = str(ref.get("created_at") or ""), str(promised_at or "")
    # "Before the promise" is judged by the day, with a day's grace: a
    # commitment is often extracted from the agent's own text hours after the
    # PR it describes was opened (2026-11-06 15:47: amara's #235, opened and
    # promised the same day, rejected as pre-existing). The false receipt this
    # guards against is months old, not hours.
    if created and promised and _day(created) is not None and _day(promised) is not None:
        if (_day(promised) - _day(created)).days > 1:  # type: ignore[operator]
            return {
                "ok": False,
                "reason": f"created {created[:10]}, before this was promised ({promised[:10]})",
                **ref,
            }
    verdict = "merged" if ref.get("merged") else ref.get("state", "")
    return {"ok": True, "reason": f"{ref.get('kind')} {verdict}, by {ref.get('author')}", **ref}
