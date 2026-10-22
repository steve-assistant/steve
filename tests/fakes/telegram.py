"""Telegram's Bot API, faked: in the test's process, or over HTTP on this
computer for a worker in its own.

As much of the Bot API as Steve calls (channels/telegram.py): `getMe`,
`getUpdates` long-polled, `sendMessage`, `sendChatAction`, callback
answers, `editMessageReplyMarkup`, and files. The test queues what the user
does -- `says`, `presses` -- and Telegram's updates come back until a later
offset confirms them, as Telegram's do.

Two ways in, one fake: `transport()` for a channel built in the test
(tests/unit/test_telegram.py), and `running()` for a worker started as the
app starts it, pointed here by `STEVE_TELEGRAM_URL` (core/endpoints.py).
Over HTTP, a poll with nothing to give waits, as Telegram's long poll does,
rather than answering at once: a worker polling a fake that answered at
once would spin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Iterator

import httpx2

from tests.fakes.served import served

TOKEN = "123456789:" + "A" * 35
USER = 4242
STRANGER = 777
# How long an HTTP poll with nothing to give waits, at most: Telegram's
# own wait is the poll's `timeout`, which the channel sets long.
LONG_POLL_S = 1.0
# Telegram's refusals, as it words them: another poller took the updates,
# and too many requests, with when to try again.
CONFLICT = (409, {"ok": False, "error_code": 409,
                  "description": "Conflict: terminated by other getUpdates request; make sure"
                                 " that only one bot instance is running"})


def too_many(retry_after: int) -> tuple[int, dict]:
    return 429, {"ok": False, "error_code": 429,
                 "description": f"Too Many Requests: retry after {retry_after}",
                 "parameters": {"retry_after": retry_after}}


class FakeBotApi:
    """Updates queued by the test, confirmed by a later offset as Telegram's
    are; every call kept."""

    def __init__(self, token: str = TOKEN, username: str = "steve_fake_bot", *,
                 wait_s: float = 0.01):
        self.token = token
        self.username = username
        self.updates: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.files: dict[str, bytes] = {}
        self.refuse: set[str] = set()
        # The hostile modes, set mid-run: by method, the answers the next
        # calls get instead of their own, one each (`CONFLICT`, `too_many`);
        # None lets one call through.
        self.trouble: dict[str, list[tuple[int, dict] | None]] = {}
        # The calls answered as asked, unlike `calls`, which keeps every try.
        self.went: list[tuple[str, dict]] = []
        self.last_id = 0
        self.polled_from = 0
        self.wait_s = wait_s
        self._lock = threading.Lock()

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def sent(self, chat: int = USER) -> list[dict]:
        return [p for m, p in self.calls if m == "sendMessage" and p.get("chat_id") == chat]

    def delivered(self, chat: int = USER) -> list[str]:
        """The texts that reached the chat: each message Telegram took."""
        return [p.get("text", "") for m, p in self.went
                if m == "sendMessage" and p.get("chat_id") == chat]

    def called(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]

    # -- what the user does --------------------------------------------------

    def _queue(self, update: dict) -> None:
        with self._lock:
            self.last_id += 1
            self.updates.append({"update_id": self.last_id, **update})

    def says(self, text: str = "", *, chat: int = USER, kind: str = "private", **more) -> None:
        message = {"message_id": self.last_id + 100, "date": int(time.time()),
                   "chat": {"id": chat, "type": kind}, "from": {"id": chat, "is_bot": False},
                   **more}
        if text:
            message["text"] = text
        self._queue({"message": message})

    def presses(self, data: str, *, chat: int = USER, by: int | None = None) -> None:
        self._queue({"callback_query": {
            "id": f"q{self.last_id}", "from": {"id": by or chat}, "data": data,
            "message": {"message_id": 5, "chat": {"id": chat, "type": "private"}}}})

    async def caught_up(self) -> None:
        """Until every queued update has been confirmed by a later poll:
        everything the channel yielded for them has been taken."""
        while self.polled_from <= self.last_id:
            await asyncio.sleep(0.005)

    # -- the API -------------------------------------------------------------

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        status, body = await self.answer(request.url.path, request.content)
        if isinstance(body, bytes):
            return httpx2.Response(status, content=body)
        return httpx2.Response(status, json=body)

    async def answer(self, path: str, content: bytes) -> tuple[int, dict | bytes]:
        """A request's status and body, by its path, either way in."""
        if path.startswith(f"/file/bot{self.token}/"):
            name = path.split(f"/file/bot{self.token}/", 1)[1]
            return (200, self.files[name]) if name in self.files else (404, b"")
        if not path.startswith(f"/bot{self.token}/"):
            return 401, {"ok": False, "description": "Unauthorized"}
        method = path.split("/")[-1]
        params = json.loads(content or b"{}")
        self.calls.append((method, params))
        if self.trouble.get(method) and (trouble := self.trouble[method].pop(0)) is not None:
            return trouble
        if method in self.refuse:
            return 400, {"ok": False, "description": f"Bad Request: no {method}"}
        result = await self._answer(method, params)
        self.went.append((method, params))
        return 200, {"ok": True, "result": result}

    async def _answer(self, method: str, params: dict):
        if method == "getMe":
            return {"id": 1, "is_bot": True, "username": self.username}
        if method == "getUpdates":
            offset = int(params.get("offset") or 0)
            self.polled_from = max(self.polled_from, offset)
            deadline = time.monotonic() + self.wait_s
            while True:
                with self._lock:
                    self.updates = [u for u in self.updates if u["update_id"] >= offset]
                    waiting = list(self.updates)
                if waiting or time.monotonic() >= deadline:
                    return waiting
                await asyncio.sleep(0.01)
        if method == "getFile":
            return {"file_id": params["file_id"], "file_path": f"files/{params['file_id']}"}
        if method == "sendMessage":
            return {"message_id": len(self.calls), "chat": {"id": params["chat_id"]}}
        return True

    # -- over HTTP -----------------------------------------------------------

    @contextlib.contextmanager
    def running(self) -> Iterator[str]:
        """Serves for the length of the block on a port of the system's
        choosing; gives the address `STEVE_TELEGRAM_URL` takes. Its polls
        wait as Telegram's do (`LONG_POLL_S`)."""
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route

        async def serve(request: Request) -> Response:
            status, body = await self.answer(request.url.path, await request.body())
            if isinstance(body, bytes):
                return Response(body, status_code=status)
            return JSONResponse(body, status_code=status)

        self.wait_s = max(self.wait_s, LONG_POLL_S)
        app = Starlette(routes=[Route("/{path:path}", serve, methods=["GET", "POST"])])
        with served(app, "the fake Bot API") as port:
            yield f"http://127.0.0.1:{port}"
