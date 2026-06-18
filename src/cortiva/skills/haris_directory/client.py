"""HARIS directory client — the node-side REST wrapper agents use.

HARIS (Human-Agent-Resource-Information-System, ``haris.innovology.io``) is the
org's own identity hub: the system of record for every person and AI agent.
This module is the thin, dependency-free client an agent uses on its node to
read and maintain that directory.

Auth + delivery: the HQ ``haris`` connector delivers the agent's own
``x-api-key`` and the API origin as the env vars ``HARIS_API_KEY`` /
``HARIS_BASE_URL`` (see ``cortiva_hq.integrations.connectors.haris``). Each agent
carries its OWN key, so every call is attributable to that identity and HARIS
RBAC (admin > hr > manager > member > viewer) applies per-agent — a viewer key
can list but not provision.

API contract (HARIS ``packages/api``, Hono + BetterAuth, all under ``/api/v1``):
  GET    /api/v1/agents?q=&page=&pageSize=&status=&department=  -> {data, pagination}
  POST   /api/v1/agents            (manager+)  agentInputSchema  -> 201 agent
  GET    /api/v1/humans?q=&...                                   -> {data, pagination}
  POST   /api/v1/humans            (hr+)       humanInputSchema  -> 201 human
  PATCH  /api/v1/humans/:id        (manager+)  partial           -> human

Stdlib ``urllib`` only — no third-party HTTP dependency on the node.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_DEFAULT_BASE = "https://haris.innovology.io"
_TIMEOUT_S = 15.0
# Cloudflare fronts HARIS and 1010-blocks the default urllib User-Agent (the
# same gotcha that bit outbound email via Resend), so we send a real one.
_USER_AGENT = "cortiva-agent/1.0 (+haris-directory-skill)"

# Valid agent kinds (HARIS agentKindSchema) — surfaced so callers fail fast
# rather than eating a 400 from the API.
AGENT_KINDS = ("assistant", "autonomous", "workflow", "copilot", "service", "other")


class HarisError(RuntimeError):
    """A HARIS call failed. ``status`` is the HTTP code when there was one.

    ``status == 401`` means the key is missing/invalid; ``403`` means the key's
    role is too low for the operation (e.g. a viewer trying to provision);
    ``409`` means a uniqueness clash (duplicate slug/email).
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HarisClient:
    """Per-agent HARIS REST client. Reads creds from the environment by default
    so an agent just constructs ``HarisClient()`` and goes."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._key = (api_key or os.environ.get("HARIS_API_KEY", "")).strip()
        base = (base_url or os.environ.get("HARIS_BASE_URL", "") or _DEFAULT_BASE).strip()
        self._base = base.rstrip("/")

    @property
    def configured(self) -> bool:
        """True iff an API key is present — agents check this before calling so
        an unconfigured HARIS surfaces as 'not set up', not a 401."""
        return bool(self._key)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if not self._key:
            raise HarisError(
                "No HARIS_API_KEY in the environment — this agent has no HARIS "
                "key yet (the directory owner mints one per agent).",
            )
        url = f"{self._base}/api/v1{path}"
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url=url, method=method, data=data)
        req.add_header("x-api-key", self._key)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise HarisError(
                f"HARIS {method} {path} -> HTTP {exc.code}: {detail}",
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise HarisError(f"HARIS {method} {path} unreachable: {exc.reason}") from exc
        return json.loads(raw) if raw else {}

    # -- reads -------------------------------------------------------------
    def list_directory(
        self,
        *,
        kind: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, list[dict[str, Any]]]:
        """List org agents and/or people.

        ``kind`` selects the slice: ``"agent"`` / ``"human"`` queries that one,
        anything else (or None) returns both. ``search`` maps to the API's ``q``
        free-text filter. Returns ``{"agents": [...], "humans": [...]}`` (only
        the requested slices populated).
        """
        params = {"q": search, "page": page, "pageSize": page_size}
        out: dict[str, list[dict[str, Any]]] = {}
        want = (kind or "").strip().lower()
        if want in ("", "agent", "agents"):
            out["agents"] = self._request("GET", "/agents", params=params).get("data", [])
        if want in ("", "human", "humans", "person", "people"):
            out["humans"] = self._request("GET", "/humans", params=params).get("data", [])
        return out

    # -- writes ------------------------------------------------------------
    def provision_agent(
        self,
        *,
        name: str,
        kind: str = "assistant",
        department: str | None = None,
        model: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Create an agent record (HARIS role manager+). Slug is derived from
        the name server-side; a duplicate raises ``HarisError(status=409)``."""
        if kind not in AGENT_KINDS:
            raise HarisError(f"kind must be one of {AGENT_KINDS}, got {kind!r}")
        body: dict[str, Any] = {"name": name, "kind": kind}
        if department:
            body["department"] = department
        if model:
            body["model"] = model
        body.update(extra)
        return self._request("POST", "/agents", body=body)

    def upsert_human(
        self,
        *,
        first_name: str,
        last_name: str,
        email: str,
        title: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Create a person, or update the existing record with that email.

        HARIS ``POST /humans`` (hr+) rejects a duplicate email with 409, so on
        a clash we look the person up by email and ``PATCH`` them instead
        (manager+) — making this a true upsert.
        """
        body: dict[str, Any] = {
            "firstName": first_name,
            "lastName": last_name,
            "email": email,
        }
        if title:
            body["title"] = title
        body.update(extra)
        try:
            return self._request("POST", "/humans", body=body)
        except HarisError as exc:
            if exc.status != 409:
                raise
        # Already exists — find by email and update.
        found = self._request("GET", "/humans", params={"q": email, "pageSize": 5})
        match = next(
            (h for h in found.get("data", []) if str(h.get("email", "")).lower() == email.lower()),
            None,
        )
        if not match or not match.get("id"):
            raise HarisError(f"human {email} reported as existing but not found to update")
        update = {k: v for k, v in body.items() if k != "email"}
        return self._request("PATCH", f"/humans/{match['id']}", body=update)
