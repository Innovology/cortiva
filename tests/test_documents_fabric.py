"""Document read/write wiring on the fabric + reflection parsing."""

from __future__ import annotations

import json

from cortiva.adapters.memory.inmemory import InMemoryAdapter
from cortiva.core.agent import Agent, WORKSPACE_DIRS
from cortiva.core.fabric import Fabric
from cortiva.core.reflection import parse_reflection_suffix


class _Chan:
    async def send(self, *a, **k):
        return None

    async def receive(self, *a, **k):
        return []

    async def listen(self, *a, **k):
        return None


class _Con:
    async def think(self, *a, **k):
        raise AssertionError("not used")

    async def reflect(self, *a, **k):
        raise AssertionError("not used")


def _fabric(tmp_path):
    return Fabric(
        agents_dir=tmp_path / "agents",
        memory=InMemoryAdapter(),
        consciousness=_Con(),
        channel=_Chan(),
    )


def _agent(tmp_path, aid="fin-01"):
    d = tmp_path / "agents" / aid
    d.mkdir(parents=True)
    for sub in WORKSPACE_DIRS:
        (d / sub).mkdir(exist_ok=True)
    return Agent(id=aid, directory=d)


# -- reflection parsing ------------------------------------------------------


def test_reflection_parses_document():
    txt = "done\n---REFLECTION---\n" + json.dumps(
        {"outcome": "x", "document": {"title": "T", "content": "hello", "visibility": "org"}}
    )
    r = parse_reflection_suffix(txt)
    assert r.suffix.document == {"title": "T", "content": "hello", "visibility": "org"}


def test_reflection_document_absent_is_none():
    txt = "done\n---REFLECTION---\n" + json.dumps({"outcome": "x"})
    assert parse_reflection_suffix(txt).suffix.document is None


# -- write (queue to outbox) -------------------------------------------------


def test_queue_outbound_document_writes_outbox(tmp_path):
    f = _fabric(tmp_path)
    a = _agent(tmp_path)
    f._queue_outbound_document(a, {"title": "Q3 report", "content": "# numbers", "visibility": "department", "department": "finance"})
    out = list((a.directory / "outbox" / "documents").glob("*.json"))
    assert len(out) == 1
    rec = json.loads(out[0].read_text())
    assert rec["title"] == "Q3 report"
    assert rec["visibility"] == "department"
    assert rec["department"] == "finance"


def test_queue_outbound_document_defaults_bad_visibility_to_private(tmp_path):
    f = _fabric(tmp_path)
    a = _agent(tmp_path)
    f._queue_outbound_document(a, {"title": "x", "content": "y", "visibility": "public"})
    rec = json.loads(next((a.directory / "outbox" / "documents").glob("*.json")).read_text())
    assert rec["visibility"] == "private"


def test_queue_outbound_document_ignores_empty(tmp_path):
    f = _fabric(tmp_path)
    a = _agent(tmp_path)
    f._queue_outbound_document(a, {"title": "", "content": ""})
    assert not (a.directory / "outbox" / "documents").exists() or not list(
        (a.directory / "outbox" / "documents").glob("*.json")
    )


# -- read (delivered docs → context) -----------------------------------------


def test_documents_context_renders_delivered(tmp_path):
    f = _fabric(tmp_path)
    a = _agent(tmp_path)
    ddir = a.directory / "documents"
    ddir.mkdir()
    (ddir / "d1.json").write_text(json.dumps({
        "doc_id": "d1", "title": "Companies House", "content": "Innovology Ltd 11778435",
        "visibility": "department", "owner_display": "Cortiva", "updated_at": "2026-06-09",
    }))
    ctx = f._documents_context(a)
    assert "Documents (shared with you)" in ctx
    assert "Companies House" in ctx
    assert "11778435" in ctx


def test_documents_context_empty_when_none(tmp_path):
    f = _fabric(tmp_path)
    a = _agent(tmp_path)
    assert f._documents_context(a) == ""


def test_documents_capability_always_available(tmp_path):
    f = _fabric(tmp_path)
    a = _agent(tmp_path)
    cap = f._documents_capability_context(a)
    assert "document" in cap.lower()
    assert "visibility" in cap.lower()
