"""Photos given in the app reach the turn as the phones' do, and the day
shows them on the message they came with.

The app had no way in for a photo, though Signal and Telegram took them;
and a photo's message showed on the day as "[1 photo attached]", so the
user couldn't see which photo a reply was about (agent/worker/app_photos.py,
channels/photos.py `link`, db/migrations/0019_message_photos.sql).
"""

from __future__ import annotations

import asyncio
import time
from datetime import date

import httpx
import pytest
from starlette.applications import Starlette

from agent import runtime
from agent.worker import app_photos, journal
from channels import photos
from core import db

PNG = b"\x89PNG\r\n\x1a\n a heron, drawn"
JPEG = b"\xff\xd8\xff\xe0 a heron, photographed"


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    yield c
    c.close()


def _call(method: str, path: str, **kw) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=Starlette(routes=app_photos.ROUTES))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.request(method, path, **kw)

    return asyncio.run(go())


def _said(conn, content: str) -> int:
    session = conn.execute("INSERT INTO sessions (channel) VALUES ('tray')").lastrowid
    return conn.execute(
        "INSERT INTO messages (session_id, role, content, created_at)"
        " VALUES (?, 'user', ?, '2026-10-15 14:00:00')", (session, content)).lastrowid


def test_a_photo_given_in_the_app_is_kept_by_its_bytes_and_anything_else_is_refused(monkeypatch):
    """Checked by its bytes, as the phones' are: a file called a photo is
    not sent as one, and one too big for the model is refused before it is
    held whole. Each refusal is a sentence the app shows."""
    kept = _call("POST", "/control/photo", content=PNG, headers={"Content-Type": "image/png"})
    name = kept.json()["name"]
    assert kept.json()["ok"] and name.endswith(".png") and len(name) == 36
    assert (app_photos.folder() / name).read_bytes() == PNG

    pdf = _call("POST", "/control/photo", content=b"%PDF-1.7 a form",
                headers={"Content-Type": "image/png"})
    assert pdf.status_code == 400 and pdf.json()["error"] == app_photos.NOT_A_PHOTO

    monkeypatch.setattr(photos, "MOST_BYTES", len(PNG) - 1)
    big = _call("POST", "/control/photo", content=PNG, headers={"Content-Type": "image/png"})
    assert big.status_code == 400 and big.json()["error"] == app_photos.TOO_BIG
    assert sorted(p.name for p in app_photos.folder().iterdir()) == [name]


def test_a_message_finds_only_photos_kept_here_and_one_gone_is_said_not_looked_for():
    """The names come over the socket: one of another shape, or pointing out
    of the folder, must not make the worker read another file."""
    name = app_photos.keep(JPEG)
    secret = app_photos.folder().parent / "secret.png"
    secret.write_bytes(PNG)
    came = app_photos.attached(["../secret.png", name, "0" * 32 + ".png", 5, str(secret)])
    assert came.images == [app_photos.folder() / name]
    assert came.unopened == [app_photos.GONE] * 4


def test_the_chat_socket_gives_the_turn_its_photos_and_records_the_line_for_them(monkeypatch):
    """The app's message reaches the turn as a Signal photo's does: the
    photos to look at, and the line that tells later turns one was sent. A
    message of photos alone is a message."""
    from starlette.routing import WebSocketRoute
    from starlette.testclient import TestClient

    from agent.worker import answering, desk, routes

    asked: list[tuple[str, list]] = []

    class _Reply:
        def as_dict(self) -> dict:
            return {"text": "A heron."}

    class _Convo:
        session_id = 7

        async def send(self, text, *, on_tool=None, images=None):
            asked.append((text, images))
            return _Reply()

    monkeypatch.setattr(answering, "conversation", lambda _sid, channel="tray": _Convo())
    monkeypatch.setattr(runtime, "voice", None)
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setattr(runtime, "turning", set())
    monkeypatch.setitem(desk._state, "engine", None)
    monkeypatch.setitem(desk._state, "ticker", None)
    first, second = app_photos.keep(JPEG), app_photos.keep(PNG)
    app = Starlette(routes=[WebSocketRoute("/ws", routes.ws_session)])

    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        for sent in ({"type": "chat", "text": "What bird is this?", "photos": [first, second]},
                     {"type": "chat", "text": "", "photos": [first]}):
            ws.send_json(sent)
            while ws.receive_json()["type"] != "reply":
                pass

    folder = app_photos.folder()
    assert asked == [("[2 photos attached]\nWhat bird is this?", [folder / first, folder / second]),
                     ("[1 photo attached]", [folder / first])]


def test_the_day_shows_a_messages_photos_on_it_in_place_of_the_line_naming_them(conn):
    """The day showed "[2 photos attached]" where the photos were. It
    carries how many came, for the page to fetch, and the words alone."""
    one = app_photos.folder() / "one.jpg"
    asked = _said(conn, "[2 photos attached]\nWhat bird is this?")
    photos.link(conn, asked, [one, one])
    plain = _said(conn, "Thanks")
    entries = {e["id"]: e for e in journal.entries(conn, date(2026, 10, 15))}
    assert (entries[asked]["photos"], entries[asked]["text"]) == (2, "What bird is this?")
    assert (entries[plain]["photos"], entries[plain]["text"]) == (0, "Thanks")


def test_a_photo_is_served_by_its_message_and_place_until_its_day_is_over(conn):
    """Fetched by the message and its place, never a path the request
    names; once the sweep has taken the file, the day is told it is gone
    and says "a photo"."""
    name = app_photos.keep(PNG)
    message = _said(conn, "[1 photo attached]")
    photos.link(conn, message, [app_photos.folder() / name])
    conn.commit()

    shown = _call("GET", f"/control/photo?message={message}&n=0")
    assert shown.status_code == 200 and shown.content == PNG
    assert shown.headers["content-type"] == "image/png"
    assert _call("GET", f"/control/photo?message={message}&n=1").status_code == 404
    assert _call("GET", "/control/photo?message=x").status_code == 400

    assert app_photos.sweep(now=time.time() + app_photos.KEEP_ATTACHMENTS_S - 60) == 0
    assert app_photos.sweep(now=time.time() + app_photos.KEEP_ATTACHMENTS_S + 60) == 1
    assert _call("GET", f"/control/photo?message={message}&n=0").status_code == 404
