"""Local subshell wrapper around the ``claude`` CLI.

The agent runtime calls ``deep_think(prompt, ...)`` when the agent
invokes the ``claude_code_deep_think.think`` tool. We spawn
``claude -p "$prompt"`` as a subprocess, wait for it to finish, and
return its stdout to the agent.

Why subprocess instead of the Anthropic SDK:

- ``claude`` (the CLI) already has the authentication, retry, prompt
  caching, and tool-use scaffolding the operator configured. Reusing it
  means one source of truth.
- The CLI's --print mode is non-interactive and predictable.
- The CLI surfaces token usage in its output, so we can charge the
  agent's budget without re-implementing accounting.

Design notes:

- Long calls are real (a complex critique can run for 30-90s). The
  default timeout is generous (180s). Callers can override.
- stdout/stderr are captured separately so that token-usage parsing
  doesn't get mixed with model output.
- Failure modes (binary missing, API key missing, non-zero exit,
  timeout) all raise ``DeepThinkError`` with a clear message. The
  agent runtime can decide whether to surface to the user or retry.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 180.0
_BINARY = "claude"


class DeepThinkError(RuntimeError):
    """Raised when the deep-think subshell fails or its preconditions
    are not satisfied. Callers should treat as 'reasoning is not
    available right now' and fall back to local reasoning."""


@dataclass
class DeepThinkResult:
    text: str
    """The model's response (stdout from ``claude -p ...``)."""

    raw_stdout: str
    """Full stdout including any usage trailers."""

    estimated_cost_gbp: float
    """Best-effort cost estimate. Real number when ``claude`` reports
    usage; falls back to the skill's typical estimate if not."""

    duration_s: float


def deep_think(
    prompt: str,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    extra_args: list[str] | None = None,
) -> DeepThinkResult:
    """Send ``prompt`` to ``claude -p ...`` and return the response.

    Args:
        prompt: the question or task for Claude. Long is fine.
        timeout_s: kill the subprocess after this many seconds and
            raise. Default 180s.
        extra_args: passed through to claude after the prompt (e.g.
            ``["--model", "claude-opus-4-20250514"]`` to override the
            CLI's default).

    Raises:
        DeepThinkError: binary missing, API key missing, non-zero exit,
            or timeout.
    """
    import time

    _check_preconditions()

    cmd: list[str] = [_BINARY, "-p", prompt]
    if extra_args:
        cmd.extend(extra_args)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        raise DeepThinkError(
            f"claude -p timed out after {elapsed:.1f}s "
            f"(limit {timeout_s}s); kill or simplify the prompt",
        ) from exc

    elapsed = time.monotonic() - started

    if proc.returncode != 0:
        raise DeepThinkError(
            f"claude -p exited rc={proc.returncode}; stderr tail:\n"
            f"{(proc.stderr or '')[-500:]}",
        )

    # Parse the response. claude --print returns plain text.
    # If the CLI version embeds a usage line we'd parse it here; for
    # the v1 fall back to the skill's typical cost estimate.
    response_text = proc.stdout.strip()
    estimated_cost = _estimate_cost_gbp(proc.stdout, proc.stderr)

    logger.info(
        "deep_think completed in %.1fs; estimated cost £%.4f",
        elapsed, estimated_cost,
    )
    return DeepThinkResult(
        text=response_text,
        raw_stdout=proc.stdout,
        estimated_cost_gbp=estimated_cost,
        duration_s=elapsed,
    )


def _check_preconditions() -> None:
    """Validate that the runtime can actually invoke claude.

    Cheap on the hot path — `shutil.which` is filesystem-only.

    Note: we deliberately do NOT require ``ANTHROPIC_API_KEY`` in env.
    The ``claude`` CLI handles authentication itself — either an
    interactive OAuth session (``claude`` was logged in) or
    ``ANTHROPIC_API_KEY``. Either is fine. If neither is configured the
    CLI itself fails with a clear "not authenticated" message that
    surfaces through our subprocess error path.
    """
    if shutil.which(_BINARY) is None:
        raise DeepThinkError(
            f"`{_BINARY}` binary not found on PATH. Install with: "
            "brew install --cask claude-code  (macOS) "
            "or  npm install -g @anthropic-ai/claude-code  (Linux).",
        )


def _estimate_cost_gbp(stdout: str, stderr: str) -> float:
    """Best-effort cost estimate.

    Future: parse a structured usage line from ``claude`` and compute
    real cost from per-1K-token pricing. For v1 we return the skill's
    typical estimate (£0.50) so budget accounting at least exists.
    """
    # Crude heuristic: longer responses cost more. Treat the response
    # length as a proxy until we have real usage parsing.
    _ = stderr
    length = len(stdout)
    if length < 500:
        return 0.10
    if length < 2000:
        return 0.30
    if length < 5000:
        return 0.60
    return 1.20


# ---------------------------------------------------------------------
# Tool schema — what the agent sees when it invokes the skill.
# ---------------------------------------------------------------------

TOOL_SCHEMA: dict[str, Any] = {
    "name": "claude_code_deep_think.think",
    "description": (
        "Ask a more capable model (Claude via the local CLI) a hard "
        "question. Use for nuanced UX critique, prioritisation under "
        "uncertainty, persona synthesis, and other reasoning your local "
        "model cannot do reliably. Every call costs real API tokens — "
        "use sparingly."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The question or task for the deeper model. Be "
                    "specific. Long prompts (multiple paragraphs) are "
                    "fine and often necessary."
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "Why you are invoking the deeper model instead of "
                    "reasoning yourself. Logged for cost-justification "
                    "review."
                ),
            },
        },
        "required": ["prompt", "rationale"],
    },
}
