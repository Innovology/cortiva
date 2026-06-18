"""HARIS directory client — verified against the real HARIS API contract
(Hono + BetterAuth apiKey, all routes under /api/v1, x-api-key header).

No network: ``urllib.request.urlopen`` is faked with a scripted queue of
responses so we assert the exact requests the client makes (method, path,
headers, query, body) and the upsert/RBAC behaviours.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import cortiva.skills.haris_directory.client as cl
from cortiva.skills.haris_directory.client import HarisClient, HarisError


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *a) -> None:  # noqa: ANN002
        return None


def _install(monkeypatch, script):
    """``script`` is a list of (dict payload) or HTTPError; served in order.
    Returns the list that records each urllib Request the client sends."""
    sent = []
    queue = list(script)

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        sent.append(req)
        item = queue.pop(0)
        if isinstance(item, urllib.error.HTTPError):
            raise item
        return _FakeResp(item)

    monkeypatch.setattr(cl.urllib.request, "urlopen", fake_urlopen)
    return sent


def _http_error(code: int, body: str = "{}"):
    return urllib.error.HTTPError(
        url="x",
        code=code,
        msg="err",
        hdrs=None,
        fp=io.BytesIO(body.encode()),
    )


def test_not_configured_without_key(monkeypatch):
    monkeypatch.delenv("HARIS_API_KEY", raising=False)
    c = HarisClient()
    assert c.configured is False
    with pytest.raises(HarisError):
        c.list_directory()


def test_list_directory_both_slices_and_auth_header(monkeypatch):
    sent = _install(monkeypatch, [{"data": [{"id": "a1"}]}, {"data": [{"id": "h1"}]}])
    c = HarisClient(api_key="k-noor", base_url="https://haris.innovology.io/")
    out = c.list_directory(search="finance", page_size=5)
    assert out == {"agents": [{"id": "a1"}], "humans": [{"id": "h1"}]}
    # two calls: /agents then /humans, both carrying the key + q->search mapping
    assert [r.get_method() for r in sent] == ["GET", "GET"]
    assert sent[0].full_url.startswith("https://haris.innovology.io/api/v1/agents?")
    assert "q=finance" in sent[0].full_url and "pageSize=5" in sent[0].full_url
    assert sent[1].full_url.startswith("https://haris.innovology.io/api/v1/humans?")
    assert sent[0].get_header("X-api-key") == "k-noor"


def test_list_directory_kind_selects_one_slice(monkeypatch):
    sent = _install(monkeypatch, [{"data": [{"id": "a1"}]}])
    c = HarisClient(api_key="k")
    out = c.list_directory(kind="agent")
    assert "agents" in out and "humans" not in out
    assert len(sent) == 1 and "/api/v1/agents" in sent[0].full_url


def test_provision_agent_posts_body(monkeypatch):
    sent = _install(monkeypatch, [{"id": "a9", "name": "Vera"}])
    c = HarisClient(api_key="k-mgr")
    res = c.provision_agent(name="Vera", kind="assistant", department="AR", model="qwen")
    assert res["id"] == "a9"
    req = sent[0]
    assert req.get_method() == "POST" and req.full_url.endswith("/api/v1/agents")
    body = json.loads(req.data)
    assert body == {"name": "Vera", "kind": "assistant", "department": "AR", "model": "qwen"}
    assert req.get_header("Content-type") == "application/json"


def test_provision_agent_rejects_bad_kind(monkeypatch):
    _install(monkeypatch, [])
    c = HarisClient(api_key="k")
    with pytest.raises(HarisError):
        c.provision_agent(name="X", kind="robot")


def test_provision_agent_403_maps_to_status(monkeypatch):
    _install(monkeypatch, [_http_error(403, '{"error":"forbidden"}')])
    c = HarisClient(api_key="k-viewer")
    with pytest.raises(HarisError) as ei:
        c.provision_agent(name="Vera")
    assert ei.value.status == 403


def test_upsert_human_create_path(monkeypatch):
    sent = _install(monkeypatch, [{"id": "h5", "email": "a@x.io"}])
    c = HarisClient(api_key="k-hr")
    res = c.upsert_human(first_name="Alex", last_name="B", email="a@x.io", title="Founder")
    assert res["id"] == "h5"
    body = json.loads(sent[0].data)
    assert body["firstName"] == "Alex" and body["email"] == "a@x.io"
    assert sent[0].full_url.endswith("/api/v1/humans")


def test_upsert_human_conflict_falls_back_to_patch(monkeypatch):
    sent = _install(
        monkeypatch,
        [
            _http_error(409, '{"error":"exists"}'),  # POST -> already exists
            {"data": [{"id": "h7", "email": "a@x.io"}]},  # GET lookup by email
            {"id": "h7", "email": "a@x.io", "title": "CEO"},  # PATCH update
        ],
    )
    c = HarisClient(api_key="k-mgr")
    res = c.upsert_human(first_name="Alex", last_name="B", email="a@x.io", title="CEO")
    assert res["title"] == "CEO"
    assert [r.get_method() for r in sent] == ["POST", "GET", "PATCH"]
    assert sent[2].full_url.endswith("/api/v1/humans/h7")
    patched = json.loads(sent[2].data)
    assert "email" not in patched and patched["title"] == "CEO"


def test_reads_creds_from_env(monkeypatch):
    monkeypatch.setenv("HARIS_API_KEY", "env-key")
    monkeypatch.setenv("HARIS_BASE_URL", "https://h.example/")
    sent = _install(monkeypatch, [{"data": []}])
    HarisClient().list_directory(kind="agent")
    assert sent[0].get_header("X-api-key") == "env-key"
    assert sent[0].full_url.startswith("https://h.example/api/v1/agents")
