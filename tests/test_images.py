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


def _serving(token: str) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), images._handler(FAKE, token, threading.Lock()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_a_picture_is_made_and_written_where_asked(tmp_path: Path) -> None:
    server, url = _serving("s3cret")
    try:
        (tmp_path / images.CONFIG_FILE).write_text(json.dumps({"url": url, "token": "s3cret"}))
        out = tmp_path / "pics" / "one.png"
        ok, said = images.make("a conveyor of blocks, flat style", out, home=tmp_path)
        assert ok, said
        data = out.read_bytes()
        assert data.startswith(b"\x89PNG") and b"a conveyor of blocks" in data
        # Whatever the brief says, no people and no words.
        assert b"no people" in data and b"no text" in data
        assert "Look at it" in said
    finally:
        server.shutdown()


def test_nobody_is_served_without_the_token(tmp_path: Path) -> None:
    server, url = _serving("s3cret")
    try:
        (tmp_path / images.CONFIG_FILE).write_text(json.dumps({"url": url, "token": "wrong"}))
        ok, said = images.make(
            "a conveyor of blocks, flat style", tmp_path / "x.png", home=tmp_path
        )
        assert not ok and "not made" in said and not (tmp_path / "x.png").exists()
    finally:
        server.shutdown()


def test_where_pictures_are_not_set_up_it_says_so(tmp_path: Path) -> None:
    assert images.config(tmp_path) == {}
    ok, said = images.make("a conveyor of blocks, flat style", tmp_path / "x.png", home=tmp_path)
    assert not ok and "not set up" in said
    (tmp_path / images.CONFIG_FILE).write_text(json.dumps({"url": "http://h:1", "token": "t"}))
    assert images.make("short", tmp_path / "x.png", home=tmp_path) == (
        False,
        "Say what the picture shows and in what style.",
    )
    assert "cortiva image" in images.guide() and "does not draw people" in images.guide()
