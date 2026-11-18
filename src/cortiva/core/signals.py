"""Small models that answer one narrow question each, beside the large one.

A node may name, per job, a small model reachable over HTTP (the
OpenAI-compatible chat API). In shadow, the fabric sends it the same
question it has just put to the large model, records both answers and both
times, and acts on the large model's. Nothing depends on a small model
until its record says it can be trusted with the job.

``<agents_dir>/.signals.json``::

    {"triage": {"base_url": "http://host:port/v1", "model": "<id>"}}

Records go to ``<agents_dir>/.signals/shadow.jsonl``.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG = ".signals.json"
SHADOW = Path(".signals") / "shadow.jsonl"
TIMEOUT_S = 30.0


def config(agents_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(agents_dir) / CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def model_for(agents_dir: Path, job: str) -> dict[str, Any] | None:
    entry = config(agents_dir).get(job)
    if isinstance(entry, dict) and entry.get("base_url") and entry.get("model"):
        return entry
    return None


def ask(
    entry: dict[str, Any], system: str, user: str, *, max_tokens: int = 200
) -> tuple[str, float]:
    """One question to a small model: ``(answer, seconds)``. Raises on any
    failure; the caller decides what a missing answer means."""
    body = {
        "model": entry["model"],
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        str(entry["base_url"]).rstrip("/") + "/chat/completions",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 - configured host
        data = json.load(resp)
    text = str(data["choices"][0]["message"].get("content") or "").strip()
    return text, time.monotonic() - started


def record(agents_dir: Path, row: dict[str, Any]) -> None:
    try:
        path = Path(agents_dir) / SHADOW
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **row}) + "\n")
    except OSError:
        logger.debug("could not record a shadow answer", exc_info=True)


def shadow(
    agents_dir: Path,
    job: str,
    agent_id: str,
    system: str,
    user: str,
    main_answer: str,
    main_seconds: float,
) -> None:
    """Put the question to the job's small model and record both answers.
    Blocking; run it off the event loop. Never raises."""
    entry = model_for(agents_dir, job)
    if entry is None:
        return
    row: dict[str, Any] = {
        "job": job,
        "agent": agent_id,
        "model": entry["model"],
        "main": str(main_answer or "").strip()[:1200],
        "main_s": round(main_seconds, 2),
        "context_chars": len(system),
    }
    try:
        answer, seconds = ask(entry, system, user)
        row.update(small=answer[:1200], small_s=round(seconds, 2))
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"[:200]
    record(agents_dir, row)
