"""Photos given to Steve in the app: chosen with the paperclip, pasted or
dropped on the page, and shown on the user's message in the day.

Until 18 October 2026 the app had no way in for a photo, though Signal and
Telegram took them (channels/photos.py). What holds now:
- a photo comes up on its own request, before the message: the socket
  through the supervisor takes a megabyte a message, and four photos are up
  to 80 MB;
- it is checked by its bytes, as the phones' are, and refused in a sentence
  otherwise: over `photos.MOST_BYTES`, or not JPEG, PNG, WebP or GIF;
- the message names its photos by the names given here, and only a name of
  this folder's shape is looked for, so a message cannot point the worker at
  another file;
- the files go after a day, as the phones' do (`sweep`); the day then says
  "a photo" where one was.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent import offloop, runtime
from channels import photos
from channels.base import KEEP_ATTACHMENTS_S, delete_older
from core import paths

_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
_TYPE = {ext: kind for kind, ext in _EXT.items()}
_NAME = re.compile(r"[0-9a-f]{32}\.(?:jpg|png|webp|gif)")
TOO_BIG = f"That photo is over {photos.MOST_BYTES // 1_000_000} MB."
NOT_A_PHOTO = "That isn't a photo Steve can read: JPEG, PNG, WebP or GIF."
GONE = "a photo that was no longer there"


def folder() -> Path:
    return paths.data_dir() / "app-photos"


def keep(data: bytes) -> str:
    """`data` kept as a photo: the name the message gives it. ValueError, in
    a sentence, for what isn't one. Its size was checked as it was read
    (`post_photo`). BLOCKING."""
    kind = photos.format_of(data)
    if kind is None:
        raise ValueError(NOT_A_PHOTO)
    name = uuid.uuid4().hex + _EXT[kind]
    where = folder()
    where.mkdir(parents=True, exist_ok=True)
    (where / name).write_bytes(data)
    return name


def attached(names: list) -> photos.Sorted:
    """The photos a message names, sorted as the phones' are
    (`photos.sort`): a name not of this folder's shape, or no longer in it,
    is said, not looked for elsewhere. BLOCKING."""
    given: list[tuple[Path, str, str | None]] = []
    gone: list[str] = []
    for name in names:
        path = folder() / name if isinstance(name, str) and _NAME.fullmatch(name) else None
        if path is not None and path.is_file():
            given.append((path, _TYPE[path.suffix], None))
        else:
            gone.append(GONE)
    out = photos.sort(given)
    out.unopened += gone
    return out


def sweep(*, now: float | None = None) -> int:
    """Delete the photos over a day old: how many went. BLOCKING."""
    return delete_older([folder()], (time.time() if now is None else now) - KEEP_ATTACHMENTS_S)


def _shown(conn, message_id: int, n: int) -> tuple[bytes, str] | None:
    """The `n`th photo of the user's message, from where its channel kept
    it, while it is still there and still a photo. BLOCKING."""
    row = conn.execute("SELECT path FROM message_photos WHERE message_id = ? AND n = ?",
                       (message_id, n)).fetchone()
    if row is None:
        return None
    try:
        data = Path(row["path"]).read_bytes()
    except OSError:
        return None
    kind = photos.format_of(data)
    return (data, kind) if kind is not None else None


async def post_photo(request: Request) -> JSONResponse:
    """The photo's bytes as the body: `{"ok", "name"}`, or why not. Read in
    pieces, so a body past the limit is refused without being held whole."""
    body = bytearray()
    async for piece in request.stream():
        body += piece
        if len(body) > photos.MOST_BYTES:
            return JSONResponse({"ok": False, "error": TOO_BIG}, status_code=400)
    try:
        name = await asyncio.to_thread(keep, bytes(body))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "name": name})


async def get_photo(request: Request) -> Response:
    """`?message=<id>&n=<k>`: the photo, or 404 once its day is over."""
    try:
        message_id = int(request.query_params["message"])
        n = int(request.query_params.get("n") or 0)
    except (KeyError, ValueError):
        return JSONResponse({"ok": False, "error": "message and n are numbers"}, status_code=400)
    shown = await offloop.with_own_conn(runtime.conn, _shown, message_id, n)
    if shown is None:
        return JSONResponse({"ok": False, "error": "no longer kept"}, status_code=404)
    data, kind = shown
    return Response(data, media_type=kind)  # never stored (`core/localonly._unstored`)


ROUTES = [
    Route("/control/photo", post_photo, methods=["POST"]),
    Route("/control/photo", get_photo, methods=["GET"]),
]
