"""Pictures, made on a machine the business owns.

A team that publishes needs pictures: an article with none is passed over,
and a site of text reads as unfinished. On Mini-2 the team shipped nine
changes to its public site in three days and none carried an image; the
whole repository held one illustration (2026-11-27). A second machine sat
idle beside it with room for an image model.

Two halves, both standard library only so the serving half runs as a single
file on a machine with no framework installed:

* ``serve`` runs on the machine with the model. One picture at a time: a
  brief comes in, a command is run, the file goes back.
* ``make`` asks it, from a work session, and writes the file where the
  member wants it. ``cortiva image`` is the command.

Where the service is, is read from ``image.json`` in the Cortiva home:
``{"url": "http://host:port", "token": "..."}``. No file, no pictures, and
nothing is said about them.
"""

from __future__ import annotations

import hmac
import json
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CONFIG_FILE = "image.json"
SHAPES: dict[str, tuple[int, int]] = {
    "wide": (1024, 576),
    "square": (768, 768),
    "tall": (576, 1024),
}
MAX_BRIEF = 600
# Said to the model whatever the brief says. A made-up face or a made-up
# photograph of work done is a false statement about the business.
ALWAYS = "illustration, no text, no lettering, no logos, no people, no faces"


def config(home: Path | None = None) -> dict[str, str]:
    """Where pictures are made; empty when nowhere is set."""
    path = (home or Path.home() / ".cortiva") / CONFIG_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not str(data.get("url") or "").startswith("http"):
        return {}
    return {"url": str(data["url"]).rstrip("/"), "token": str(data.get("token") or "")}


def guide() -> str:
    """How a work session asks for a picture."""
    return (
        "## 🖼 Pictures\n"
        '- `cortiva image "<what the picture shows and its style>" --out <path.png> '
        "[--shape wide|square|tall]` makes one picture on the business's own machine and "
        "writes it to the path. It takes a minute or two; one is made at a time.\n"
        "- It draws illustrations and diagrams. It does not draw people, logos or words, "
        "and nothing it makes is to be passed off as a photograph of real work or of a "
        "real client.\n"
        "- A picture goes where the words go: in the same change, read by the same "
        "reviewer. Look at it before you use it; ask again with a clearer brief if it is "
        "not right."
    )


def make(
    brief: str,
    out: Path,
    *,
    shape: str = "wide",
    home: Path | None = None,
    timeout: float = 900.0,
) -> tuple[bool, str]:
    """Ask for one picture and write it to ``out``. ``(ok, what happened)``."""
    cfg = config(home)
    if not cfg:
        return False, "Pictures are not set up on this machine."
    text = " ".join((brief or "").split())
    if len(text) < 12:
        return False, "Say what the picture shows and in what style."
    if shape not in SHAPES:
        return False, f"Shape is one of: {', '.join(SHAPES)}."
    body = json.dumps({"brief": text[:MAX_BRIEF], "shape": shape}).encode()
    req = urllib.request.Request(
        cfg["url"] + "/make",
        body,
        {"Content-Type": "application/json", "Authorization": f"Bearer {cfg['token']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        return False, f"The picture was not made: {exc.read().decode(errors='replace')[:200]}"
    except (OSError, TimeoutError) as exc:
        return False, f"The machine that makes pictures did not answer ({exc})."
    if not data.startswith(b"\x89PNG"):
        return False, "What came back was not a picture."
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return True, f"Written to {out} ({len(data) // 1024} KB). Look at it before you use it."


# --------------------------------------------------------------------------
# The serving half
# --------------------------------------------------------------------------


def _handler(command: str, token: str, lock: threading.Lock) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _say(self, code: int, text: str) -> None:
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {fmt % args}\n")

        def do_GET(self) -> None:  # noqa: N802
            self._say(200, "busy" if lock.locked() else "ready")

        def do_POST(self) -> None:  # noqa: N802
            given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not token or not hmac.compare_digest(given, token):
                self._say(401, "no")
                return
            try:
                size = min(int(self.headers.get("Content-Length") or 0), 8192)
                asked = json.loads(self.rfile.read(size) or b"{}")
                brief = " ".join(str(asked.get("brief") or "").split())[:MAX_BRIEF]
                width, height = SHAPES[str(asked.get("shape") or "wide")]
            except (ValueError, KeyError):
                self._say(400, "a brief and a shape (wide, square, tall)")
                return
            if len(brief) < 12:
                self._say(400, "say what the picture shows")
                return
            with lock, tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "picture.png"
                argv = [
                    part.format(prompt=f"{brief}, {ALWAYS}", width=width, height=height, out=out)
                    for part in shlex.split(command)
                ]
                started = time.time()
                try:
                    done = subprocess.run(argv, capture_output=True, timeout=840, check=False)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self._say(500, f"could not run: {exc}")
                    return
                if done.returncode != 0 or not out.exists():
                    self._say(500, done.stderr.decode(errors="replace")[-300:] or "no picture")
                    return
                data = out.read_bytes()
            sys.stderr.write(f"made in {time.time() - started:.0f}s: {brief[:100]}\n")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(command: str, token: str, *, host: str = "0.0.0.0", port: int = 9200) -> None:
    """Make pictures for whoever holds the token, one at a time.

    ``command`` is what makes one, with ``{prompt}``, ``{width}``,
    ``{height}`` and ``{out}`` where those go."""
    if not token:
        raise SystemExit("a token is needed: nobody is served without one")
    server = ThreadingHTTPServer((host, port), _handler(command, token, threading.Lock()))
    sys.stderr.write(f"pictures on {host}:{port}\n")
    server.serve_forever()


if __name__ == "__main__":
    import argparse
    import os

    ap = argparse.ArgumentParser(description="Make pictures for a Cortiva team")
    ap.add_argument("--command", required=True)
    ap.add_argument("--port", type=int, default=9200)
    ap.add_argument("--host", default="0.0.0.0")
    ns = ap.parse_args()
    serve(ns.command, os.environ.get("CORTIVA_IMAGE_TOKEN", ""), host=ns.host, port=ns.port)
