"""Stream classification for the steerable Claude session driver."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from cortiva.adapters.terminal.claude_session import (
    Checkpoint,
    ClaudeSession,
    _looks_destructive,
)


def _s() -> ClaudeSession:
    return ClaudeSession(cwd=Path("/tmp"))


def test_destructive_detection():
    assert _looks_destructive("Bash", {"command": "git push --force origin main"})
    assert _looks_destructive("Bash", {"command": "rm -rf build/"})
    assert not _looks_destructive("Bash", {"command": "ls -la"})
    assert not _looks_destructive("Read", {"file": "x.py"})


def test_classify_init_and_result():
    s = _s()
    assert (
        s._classify({"type": "system", "subtype": "init", "session_id": "abc"})[0].checkpoint
        is Checkpoint.INIT
    )
    done = s._classify({"type": "result", "session_id": "abc", "is_error": False, "result": "ok"})[
        0
    ]
    assert done.checkpoint is Checkpoint.DONE and done.text == "ok"


def test_classify_tool_vs_destructive():
    s = _s()
    safe = s._classify(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
            },
        }
    )[0]
    assert safe.checkpoint is Checkpoint.TOOL and safe.tool_name == "Bash"
    danger = s._classify(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "git push --force"}}
                ]
            },
        }
    )[0]
    assert danger.checkpoint is Checkpoint.DESTRUCTIVE


def test_classify_narration_and_rate_limit():
    s = _s()
    nar = s._classify(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Let me check the tests first."}]},
        }
    )[0]
    assert nar.checkpoint is Checkpoint.NARRATION and "tests" in nar.text
    assert s._classify({"type": "rate_limit_event"})[0].checkpoint is Checkpoint.RATE_LIMIT


def test_classify_tool_result():
    s = _s()
    ev = s._classify(
        {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": False}]}}
    )[0]
    assert ev.checkpoint is Checkpoint.TOOL_RESULT


def test_events_survive_lines_longer_than_asyncio_default_limit(tmp_path: Path) -> None:
    # A 200KB tool-result line used to kill the session (asyncio's 64KiB limit).
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "big = {'type': 'user', 'message': {'content': [{'type': 'tool_result', "
        "'content': 'x' * 200_000}]}}\n"
        "print(json.dumps(big), flush=True)\n"
        "print(json.dumps({'type': 'result', 'result': 'ok', 'session_id': 's1'}), flush=True)\n"
    )
    s = ClaudeSession(cwd=tmp_path)
    s._build_cmd = lambda: [sys.executable, str(script)]  # type: ignore[method-assign]

    async def run() -> list[Checkpoint]:
        await s.start("go")
        seen = [ev.checkpoint async for ev in s.events()]
        await s.close()
        return seen

    assert asyncio.run(run())[-1] is Checkpoint.DONE
