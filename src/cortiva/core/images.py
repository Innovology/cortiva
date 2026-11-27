"""Pictures, made on a machine the business owns.

A team that publishes needs pictures: an article with none is passed over,
and a site of text reads as unfinished. On Mini-2 the team shipped nine
changes to its public site in three days and none carried an image; the
whole repository held one illustration (2026-11-27). A second machine sat
idle beside it with room for an image model.

Two halves, both standard library only so the serving half runs as a single
file on a machine with no framework installed:

* ``serve`` runs on the machine with the model. It keeps a line: a brief
  joins it, one picture is made at a time, and the line is kept on disk so
  a restart loses nothing.
* ``make`` joins the line from a work session and waits a little. A picture
  that takes longer than the wait is still made: ``collect`` fetches it
  when it is ready, writes it where the member asked, and says who asked.
  ``cortiva image`` is the command.

How long one picture takes does not matter so long as the one who asked
knows it is in the line and hears when it is done.

Where the service is, is read from ``image.json`` in the Cortiva home:
``{"url": "http://host:port", "token": "..."}``. No file, no pictures, and
nothing is said about them.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shlex
import subprocess
import sys
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
WAITING_DIR = "image-waiting"
KEEP_DAYS = 7.0
# How long one picture is taken to need before any has been made here.
TYPICAL_S = 120.0
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


def _home(home: Path | None) -> Path:
    return home or Path.home() / ".cortiva"


def waiting(home: Path | None = None) -> list[dict[str, Any]]:
    """Pictures asked for from this machine and not yet written."""
    out: list[dict[str, Any]] = []
    try:
        files = sorted((_home(home) / WAITING_DIR).glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            ask = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(ask, dict) and ask.get("id") and ask.get("out"):
            out.append(ask)
    return sorted(out, key=lambda a: float(a.get("asked") or 0))


def guide(home: Path | None = None, by: str = "") -> str:
    """How a work session asks for a picture, and what is in the line."""
    text = (
        "## 🖼 Pictures\n"
        '- `cortiva image "<what the picture shows and its style>" [--out <path.png>] '
        "[--shape wide|square|tall]` asks for one picture from the business's own machine. "
        "Pictures are made one at a time, in the order asked. You are given a job and how "
        "long it should be, from the line ahead of it. Yours may be written within minutes "
        "or take hours; either way it is in the line, it is written to the path when made "
        "(your own `pictures/` folder when you name none), and you are told where. Ask and "
        "carry on; do not ask twice for the same one.\n"
        "- `cortiva image --list` shows what is being made and what waits; "
        "`cortiva image --job <job>` says where one stands.\n"
        "- It draws illustrations and diagrams. It does not draw people, logos or words, "
        "and nothing it makes is to be passed off as a photograph of real work or of a "
        "real client.\n"
        "- A picture goes where the words go: in the same change, read by the same "
        "reviewer. Look at it before you use it; ask again with a clearer brief if it is "
        "not right."
    )
    asks = waiting(home)
    if asks:
        mine = [a for a in asks if by and str(a.get("by") or "") == by]
        text += f"\n- In the line now from this team: {len(asks)}."
        for a in mine[:5]:
            text += f"\n  - yours, for `{a['out']}`: {str(a.get('brief') or '')[:90]}"
    return text


def _call(
    cfg: dict[str, str], path: str, body: dict[str, Any] | None = None, timeout: float = 20.0
) -> tuple[int, bytes]:
    req = urllib.request.Request(
        cfg["url"] + path,
        json.dumps(body).encode() if body is not None else None,
        {"Content-Type": "application/json", "Authorization": f"Bearer {cfg['token']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def own_place(brief: str, agent_dir: Path) -> Path:
    """Where a picture goes when the one asking names no place: her own
    ``pictures`` folder, named for what it shows."""
    words = "".join(c if c.isalnum() else "-" for c in brief.lower()).split("-")
    name = "-".join(w for w in words if w)[:48].rstrip("-") or "picture"
    return Path(agent_dir) / "pictures" / f"{name}-{time.strftime('%m%d-%H%M%S')}.png"


def about(seconds: float) -> str:
    """A wait, said the way a person would say it."""
    minutes = max(1, round(seconds / 60))
    if minutes < 90:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    return f"about {round(minutes / 60)} hours"


def _standing(job: dict[str, Any]) -> str:
    """Where a picture not yet made stands, with how long it should be."""
    wait = about(float(job.get("eta_s") or 0))
    if job.get("state") == "making":
        return f"It is being made now, {wait} to go."
    return f"It is in the line with {int(job.get('ahead') or 0)} ahead of it, {wait} to go."


def _fetch(cfg: dict[str, str], ask: dict[str, Any]) -> tuple[str, str]:
    """Where one asked-for picture stands: ``(state, what happened)``.
    When it is made it is written to where it was asked for."""
    code, raw = _call(cfg, f"/job/{ask['id']}")
    if code == 404:
        return "failed", "The machine that makes pictures no longer holds this one."
    if code != 200:
        return "unknown", raw.decode(errors="replace")[:200]
    job = json.loads(raw)
    state = str(job.get("state") or "")
    if state == "failed":
        return "failed", str(job.get("error") or "no picture")[:300]
    if state != "done":
        return ("making" if state == "making" else "queued"), _standing(job)
    code, data = _call(cfg, f"/job/{ask['id']}/png", timeout=60.0)
    if code != 200 or not data.startswith(b"\x89PNG"):
        return "failed", "What came back was not a picture."
    out = Path(str(ask["out"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return "done", f"Written to {out} ({len(data) // 1024} KB). Look at it before you use it."


def make(
    brief: str,
    out: Path,
    *,
    shape: str = "wide",
    home: Path | None = None,
    by: str = "",
    wait: float = 180.0,
) -> tuple[bool, str]:
    """Join the line for one picture, to be written to ``out``.

    Waits ``wait`` seconds for it. A picture not made by then stays in the
    line and ``collect`` writes it later. ``(ok, what happened)``."""
    cfg = config(home)
    if not cfg:
        return False, "Pictures are not set up on this machine."
    text = " ".join((brief or "").split())
    if len(text) < 12:
        return False, "Say what the picture shows and in what style."
    if shape not in SHAPES:
        return False, f"Shape is one of: {', '.join(SHAPES)}."
    out = Path(out).expanduser().resolve()
    for a in waiting(home):
        if str(a.get("out")) == str(out):
            return True, f"A picture for {out} is already in the line. You will be told."
    try:
        code, raw = _call(cfg, "/make", {"brief": text[:MAX_BRIEF], "shape": shape, "by": by})
    except (OSError, TimeoutError) as exc:
        return False, f"The machine that makes pictures did not answer ({exc})."
    if code not in (200, 202):
        return False, f"The picture was not asked for: {raw.decode(errors='replace')[:200]}"
    try:
        job = json.loads(raw)
        ask = {
            "id": str(job["id"]),
            "out": str(out),
            "by": by,
            "brief": text[:MAX_BRIEF],
            "asked": time.time(),
            # Whoever is waiting on it here fetches it; nobody else until then.
            "held_until": time.time() + wait + 30.0,
        }
    except (ValueError, KeyError, TypeError):
        return False, "The machine that makes pictures gave no receipt."
    note = _home(home) / WAITING_DIR / f"{ask['id']}.json"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(json.dumps(ask), encoding="utf-8")
    said = _standing(job)
    until = time.time() + wait
    while time.time() < until:
        time.sleep(min(5.0, max(0.2, wait / 20)))
        try:
            state, said = _fetch(cfg, ask)
        except (OSError, TimeoutError, ValueError):
            continue
        if state in ("done", "failed"):
            note.unlink(missing_ok=True)
            if state == "done":
                return True, said
            return False, f"The picture was not made: {said}"
    return True, (
        f"Asked for, job {ask['id']}. {said} It will be written to {out} when it is made "
        f"and you will be told; carry on with the rest. `cortiva image --job {ask['id']}` "
        "says where it stands."
    )


def track(job_id: str, home: Path | None = None) -> str:
    """Where one asked-for picture stands."""
    cfg = config(home)
    if not cfg:
        return "Pictures are not set up on this machine."
    try:
        code, raw = _call(cfg, f"/job/{job_id.strip()}")
        job = json.loads(raw) if code == 200 else None
    except (OSError, TimeoutError, ValueError) as exc:
        return f"The machine that makes pictures did not answer ({exc})."
    if not isinstance(job, dict):
        return "No picture is known by that job."
    where = next((str(a["out"]) for a in waiting(home) if str(a["id"]) == job.get("id")), "")
    state = str(job.get("state") or "")
    if state == "failed":
        return f"It was not made: {str(job.get('error') or 'no picture')[:300]}"
    if state == "done":
        took = about(float(job.get("took") or 0))
        if where:
            return f"It is made (it took {took}) and is on its way to {where}."
        return f"It is made (it took {took}) and was written where it was asked for."
    return _standing(job) + (f" It will be written to {where}." if where else "")


def collect(home: Path | None = None) -> list[dict[str, Any]]:
    """Fetch every asked-for picture that is ready and write it where it
    was asked for. Returns what finished, made or not, with who asked."""
    cfg = config(home)
    finished: list[dict[str, Any]] = []
    if not cfg:
        return finished
    for ask in waiting(home):
        if float(ask.get("held_until") or 0) > time.time():
            continue
        try:
            state, said = _fetch(cfg, ask)
        except (OSError, TimeoutError, ValueError):
            break  # The machine is not answering; the line keeps.
        if state in ("done", "failed"):
            (_home(home) / WAITING_DIR / f"{ask['id']}.json").unlink(missing_ok=True)
            finished.append({**ask, "ok": state == "done", "said": said})
    return finished


def line(home: Path | None = None) -> str:
    """The line as the machine that makes pictures has it."""
    cfg = config(home)
    if not cfg:
        return "Pictures are not set up on this machine."
    try:
        code, raw = _call(cfg, "/jobs")
        jobs = json.loads(raw) if code == 200 else None
    except (OSError, TimeoutError, ValueError) as exc:
        return f"The machine that makes pictures did not answer ({exc})."
    if not isinstance(jobs, list):
        return "The machine that makes pictures did not say."
    mine = {str(a["id"]): a for a in waiting(home)}
    rows = []
    for j in jobs:
        where = mine.get(str(j.get("id")), {}).get("out", "")
        who = f" [{j['by']}]" if j.get("by") else ""
        rows.append(
            f"- {j.get('state')}{who}, job {j.get('id')}, {about(float(j.get('eta_s') or 0))} "
            f"to go: {str(j.get('brief') or '')[:80]}" + (f" -> {where}" if where else "")
        )
    return "\n".join(rows) if rows else "The line is empty; a picture asked for now is made next."


# --------------------------------------------------------------------------
# The serving half
# --------------------------------------------------------------------------


class Line:
    """The line of pictures to make, kept on disk, worked one at a time."""

    def __init__(self, command: str, root: Path, *, limit: float = 4 * 3600.0) -> None:
        self.command = command
        self.root = Path(root)
        self.limit = limit
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self.jobs: dict[str, dict[str, Any]] = {}
        for f in self.root.glob("*.json"):
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("state") == "making":
                job["state"] = "queued"  # Cut off by a restart; made again.
            self.jobs[str(job["id"])] = job

    def _save(self, job: dict[str, Any]) -> None:
        tmp = self.root / f"{job['id']}.tmp"
        tmp.write_text(json.dumps(job), encoding="utf-8")
        os.replace(tmp, self.root / f"{job['id']}.json")

    def _queued(self) -> list[dict[str, Any]]:
        return sorted(
            (j for j in self.jobs.values() if j["state"] == "queued"),
            key=lambda j: float(j["asked"]),
        )

    def _typical(self) -> float:
        """How long one picture has been taking here, in seconds."""
        took = [
            float(j["took"])
            for j in sorted(self.jobs.values(), key=lambda j: float(j.get("finished") or 0))
            if j["state"] == "done" and j.get("took")
        ][-10:]
        return max(5.0, sum(took) / len(took)) if took else TYPICAL_S

    def _eta(self, job: dict[str, Any]) -> float:
        """Seconds until this picture should be made, from the line ahead
        of it and how long pictures have been taking."""
        typical = self._typical()
        now = time.time()
        total = 0.0
        for j in self.jobs.values():
            if j["state"] == "making":
                left = max(typical - (now - float(j.get("started") or now)), typical * 0.1)
                if j is job:
                    return left
                total += left
            elif j["state"] == "queued" and j is not job and j["asked"] < job["asked"]:
                total += typical
        return total + typical

    def add(self, brief: str, shape: str, by: str) -> dict[str, Any]:
        with self._lock:
            ahead = sum(1 for j in self.jobs.values() if j["state"] in ("queued", "making"))
            job: dict[str, Any] = {
                "id": secrets.token_hex(6),
                "brief": brief,
                "shape": shape,
                "by": by[:40],
                "state": "queued",
                "asked": time.time(),
            }
            self.jobs[job["id"]] = job
            self._save(job)
            receipt = {**job, "ahead": ahead, "eta_s": round(self._eta(job))}
        self._wake.set()
        return receipt

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            ahead = 0
            if job["state"] == "queued":
                ahead = sum(
                    1
                    for j in self.jobs.values()
                    if j["state"] == "making"
                    or (j["state"] == "queued" and j["asked"] < job["asked"])
                )
            live = job["state"] in ("queued", "making")
            return {**job, "ahead": ahead, "eta_s": round(self._eta(job)) if live else 0}

    def picture(self, job_id: str) -> Path:
        return self.root / f"{job_id}.png"

    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            live = [j for j in self.jobs.values() if j["state"] in ("queued", "making")]
            live = [{**j, "eta_s": round(self._eta(j))} for j in live]
            return sorted(live, key=lambda j: (j["state"] != "making", float(j["asked"])))

    def busy(self) -> int:
        return len(self.listing())

    def work_one(self) -> bool:
        """Make the next picture in the line. False when the line is empty."""
        with self._lock:
            queued = self._queued()
            if not queued:
                return False
            job = queued[0]
            job["state"] = "making"
            job["started"] = time.time()
            self._save(job)
        width, height = SHAPES[job["shape"]]
        out = self.picture(job["id"])
        argv = [
            part.format(prompt=f"{job['brief']}, {ALWAYS}", width=width, height=height, out=out)
            for part in shlex.split(self.command)
        ]
        error = ""
        try:
            done = subprocess.run(argv, capture_output=True, timeout=self.limit, check=False)
            if done.returncode != 0 or not out.exists():
                error = done.stderr.decode(errors="replace")[-300:] or "no picture"
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = f"could not run: {exc}"[:300]
        with self._lock:
            job["took"] = round(time.time() - float(job["started"]))
            job["finished"] = time.time()
            job["state"] = "failed" if error else "done"
            if error:
                job["error"] = error
            self._save(job)
        sys.stderr.write(f"{job['state']} in {job['took']}s: {job['brief'][:100]}\n")
        return True

    def prune(self) -> None:
        """Forget pictures finished long ago."""
        cutoff = time.time() - KEEP_DAYS * 86400
        with self._lock:
            old = [
                k
                for k, j in self.jobs.items()
                if j["state"] in ("done", "failed") and float(j.get("finished") or 0) < cutoff
            ]
            for k in old:
                del self.jobs[k]
                (self.root / f"{k}.json").unlink(missing_ok=True)
                self.picture(k).unlink(missing_ok=True)

    def work_forever(self) -> None:
        while True:
            if not self.work_one():
                self.prune()
                self._wake.wait(60.0)
                self._wake.clear()


def _handler(line_: Line, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _say(self, code: int, text: str, kind: str = "text/plain; charset=utf-8") -> None:
            self._send(code, text.encode(), kind)

        def _send(self, code: int, data: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {fmt % args}\n")

        def _allowed(self) -> bool:
            given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if token and hmac.compare_digest(given, token):
                return True
            self._say(401, "no")
            return False

        def do_GET(self) -> None:  # noqa: N802
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if not parts:
                live = line_.listing()
                if live:
                    last = about(max(float(j["eta_s"]) for j in live))
                    self._say(200, f"busy, {len(live)} in the line, {last} to clear")
                else:
                    self._say(200, "ready")
                return
            if not self._allowed():
                return
            if parts == ["jobs"]:
                self._say(200, json.dumps(line_.listing()), "application/json")
                return
            job = line_.get(parts[1]) if len(parts) >= 2 and parts[0] == "job" else None
            if job is None:
                self._say(404, "no such picture")
            elif len(parts) == 2:
                self._say(200, json.dumps(job), "application/json")
            elif job["state"] == "done" and line_.picture(job["id"]).exists():
                self._send(200, line_.picture(job["id"]).read_bytes(), "image/png")
            else:
                self._say(404, "not made")

        def do_POST(self) -> None:  # noqa: N802
            if not self._allowed():
                return
            try:
                size = min(int(self.headers.get("Content-Length") or 0), 8192)
                asked = json.loads(self.rfile.read(size) or b"{}")
                brief = " ".join(str(asked.get("brief") or "").split())[:MAX_BRIEF]
                shape = str(asked.get("shape") or "wide")
                if shape not in SHAPES:
                    raise KeyError(shape)
            except (ValueError, KeyError, AttributeError):
                self._say(400, "a brief and a shape (wide, square, tall)")
                return
            if len(brief) < 12:
                self._say(400, "say what the picture shows")
                return
            job = line_.add(brief, shape, str(asked.get("by") or ""))
            self._say(202, json.dumps(job), "application/json")

    return Handler


def serve(
    command: str,
    token: str,
    *,
    host: str = "0.0.0.0",
    port: int = 9200,
    root: Path | None = None,
    limit: float = 4 * 3600.0,
) -> None:
    """Make pictures for whoever holds the token, one at a time, in order.

    ``command`` is what makes one, with ``{prompt}``, ``{width}``,
    ``{height}`` and ``{out}`` where those go. ``root`` is where the line
    and the pictures are kept."""
    if not token:
        raise SystemExit("a token is needed: nobody is served without one")
    line_ = Line(command, root or Path("line"), limit=limit)
    threading.Thread(target=line_.work_forever, daemon=True).start()
    server = ThreadingHTTPServer((host, port), _handler(line_, token))
    sys.stderr.write(f"pictures on {host}:{port}, {line_.busy()} in the line\n")
    server.serve_forever()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Make pictures for a Cortiva team")
    ap.add_argument("--command", required=True)
    ap.add_argument("--port", type=int, default=9200)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--line", default="line", help="Where the line and pictures are kept")
    ap.add_argument("--limit", type=float, default=4 * 3600.0, help="Seconds one may take")
    ns = ap.parse_args()
    serve(
        ns.command,
        os.environ.get("CORTIVA_IMAGE_TOKEN", ""),
        host=ns.host,
        port=ns.port,
        root=Path(ns.line),
        limit=ns.limit,
    )
