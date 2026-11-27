"""Pictures made on the business's own machine."""

from __future__ import annotations

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from cortiva.core import images

FAKE = (
    f"{sys.executable} -c \"import sys; open(sys.argv[2],'wb').write("
    "b'\\x89PNG'+sys.argv[1].encode())\" {prompt} {out}"
)
BRIEF = "a conveyor of blocks, flat style"


def _serving(token: str, root: Path, *, working: bool = True) -> tuple[ThreadingHTTPServer, str]:
    line = images.Line(FAKE, root)
    if working:
        threading.Thread(target=line.work_forever, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), images._handler(line, token))
    server.line = line  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _set_up(home: Path, url: str, token: str = "s3cret") -> None:
    (home / images.CONFIG_FILE).write_text(json.dumps({"url": url, "token": token}))


def test_a_picture_is_made_and_written_where_asked(tmp_path: Path) -> None:
    server, url = _serving("s3cret", tmp_path / "line")
    try:
        _set_up(tmp_path, url)
        out = tmp_path / "pics" / "one.png"
        ok, said = images.make(BRIEF, out, home=tmp_path, wait=20)
        assert ok, said
        data = out.read_bytes()
        assert data.startswith(b"\x89PNG") and b"a conveyor of blocks" in data
        # Whatever the brief says, no people and no words.
        assert b"no people" in data and b"no text" in data
        assert "Look at it" in said
        assert images.waiting(tmp_path) == []
    finally:
        server.shutdown()


def test_a_slow_picture_stays_in_the_line_and_is_collected(tmp_path: Path) -> None:
    # Nobody is making pictures yet: the machine is busy for hours.
    server, url = _serving("s3cret", tmp_path / "line", working=False)
    try:
        _set_up(tmp_path, url)
        out = tmp_path / "pics" / "slow.png"
        ok, said = images.make(BRIEF, out, home=tmp_path, by="andre", wait=0)
        assert ok and "in the line" in said and "you will be told" in said
        assert not out.exists()
        asks = images.waiting(tmp_path)
        assert [a["by"] for a in asks] == ["andre"]
        # She is told it is waiting, and asking again does not add another.
        assert "yours, for" in images.guide(tmp_path, by="andre")
        assert "In the line now from this team: 1" in images.guide(tmp_path, by="maren")
        again = images.make(BRIEF, out, home=tmp_path, by="andre", wait=0)
        assert again[0] and "already in the line" in again[1]
        assert server.line.busy() == 1  # type: ignore[attr-defined]
        assert "queued [andre]" in images.line(tmp_path)
        # Whoever waits on it holds it; nobody else collects until then.
        assert images.collect(tmp_path) == []
        note = tmp_path / images.WAITING_DIR / f"{asks[0]['id']}.json"
        note.write_text(json.dumps({**asks[0], "held_until": 0}))
        assert images.collect(tmp_path) == []  # Still not made.
        assert server.line.work_one()  # type: ignore[attr-defined]
        finished = images.collect(tmp_path)
        assert [(f["by"], f["ok"]) for f in finished] == [("andre", True)]
        assert out.read_bytes().startswith(b"\x89PNG")
        assert images.waiting(tmp_path) == []
    finally:
        server.shutdown()


def test_the_line_outlives_a_restart(tmp_path: Path) -> None:
    line = images.Line(FAKE, tmp_path / "line")
    first = line.add(BRIEF, "wide", "yuki")
    second = line.add(BRIEF + " again", "square", "amara")
    assert (first["ahead"], second["ahead"]) == (0, 1)
    # One was being made when the machine went down.
    line.jobs[first["id"]]["state"] = "making"
    line._save(line.jobs[first["id"]])
    back = images.Line(FAKE, tmp_path / "line")
    assert [j["id"] for j in back.listing()] == [first["id"], second["id"]]
    assert back.work_one() and back.work_one() and not back.work_one()
    assert back.get(second["id"])["state"] == "done"  # type: ignore[index]
    assert back.picture(first["id"]).exists()


def test_a_picture_that_cannot_be_made_is_said_so(tmp_path: Path) -> None:
    line = images.Line(f"{sys.executable} -c \"raise SystemExit('no room')\"", tmp_path / "line")
    server = ThreadingHTTPServer(("127.0.0.1", 0), images._handler(line, "s3cret"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=line.work_forever, daemon=True).start()
    try:
        _set_up(tmp_path, f"http://127.0.0.1:{server.server_address[1]}")
        ok, said = images.make(BRIEF, tmp_path / "x.png", home=tmp_path, wait=20)
        assert not ok and "no room" in said
        assert images.waiting(tmp_path) == []
    finally:
        server.shutdown()


def test_nobody_is_served_without_the_token(tmp_path: Path) -> None:
    server, url = _serving("s3cret", tmp_path / "line")
    try:
        _set_up(tmp_path, url, "wrong")
        ok, said = images.make(BRIEF, tmp_path / "x.png", home=tmp_path, wait=1)
        assert not ok and "not asked for" in said and not (tmp_path / "x.png").exists()
        assert server.line.busy() == 0  # type: ignore[attr-defined]
    finally:
        server.shutdown()


def test_where_pictures_are_not_set_up_it_says_so(tmp_path: Path) -> None:
    assert images.config(tmp_path) == {}
    ok, said = images.make(BRIEF, tmp_path / "x.png", home=tmp_path)
    assert not ok and "not set up" in said
    assert images.collect(tmp_path) == []
    (tmp_path / images.CONFIG_FILE).write_text(json.dumps({"url": "http://h:1", "token": "t"}))
    assert images.make("short", tmp_path / "x.png", home=tmp_path) == (
        False,
        "Say what the picture shows and in what style.",
    )
    assert "cortiva image" in images.guide(tmp_path)
    assert "does not draw people" in images.guide(tmp_path)
