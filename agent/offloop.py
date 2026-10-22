"""Blocking work, done somewhere other than the worker's event loop.

Everything the worker does -- every conversation, Signal, `/health` --
shares one asyncio loop, and the supervisor's watchdog judges the worker
by whether `/health` answers. So a synchronous call that takes twenty
seconds does not merely make one request slow: it freezes the worker, the
watchdog counts strikes, and a healthy worker is killed and replaced -- the
crash loop, caused by nothing crashing. The tray polls `/status` every five
seconds, and `/status` ran `docker info` (15 s timeout), `docker images`
(30 s) and a Google token check on the loop; `run_code` ran the whole
sandbox on it; `recall` and `remember` embedded text and committed to git
on it.

The rule, enforced by tests/unit/test_offloop.py: an `async def` never calls
a known-blocking function directly. It goes through one of these.

The trap every earlier fix of this kind fell into is the database. A
`sqlite3` connection belongs to the thread that opened it, so handing the
loop's connection to `asyncio.to_thread` raises `ProgrammingError` -- which
killed every nightly upgrade attempt on the first real night. `with_own_conn`
opens a connection of the thread's own, to the *same file* as the one it was
given, so a test's temporary database stays the test's and the live one is
never touched from a test.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


def db_path(conn: sqlite3.Connection) -> Path | None:
    """The file behind a connection, or None for an in-memory database."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        name, file = row[1], row[2]
        if name == "main":
            return Path(file) if file else None
    return None


async def with_own_conn[T](
    conn: sqlite3.Connection, fn: Callable[..., T], *args: Any, **kwargs: Any
) -> T:
    """`fn(own_conn, *args, **kwargs)` in a worker thread, on a connection of
    its own to the same database as `conn`.

    An in-memory database cannot be opened twice, so there it runs on `conn`
    in place -- which only happens in tests, and blocks nothing that matters.
    """
    from core import db

    path = db_path(conn)
    if path is None:
        return fn(conn, *args, **kwargs)

    def run() -> T:
        own = db.open_store(path)
        try:
            return fn(own, *args, **kwargs)
        finally:
            own.close()

    return await asyncio.to_thread(run)


class Probe:
    """A slow status check, run off the loop and remembered for `ttl_s`.

    For values a status page shows and nothing decides on -- whether Docker
    is up, whether the Google token is valid. At most one check is in flight;
    everyone asking meanwhile waits for that one rather than starting their
    own, so a tray polling every five seconds costs one check per `ttl_s`
    however slow the check is.
    """

    def __init__(self, fn: Callable[[], T], *, ttl_s: float) -> None:
        self.fn = fn
        self.ttl_s = ttl_s
        self._value: T | None = None
        self._at = float("-inf")
        self._lock: asyncio.Lock | None = None

    async def get(self) -> T:
        if time.monotonic() - self._at < self.ttl_s:
            return self._value  # type: ignore[return-value]
        # Created on first use, inside the loop that will use it.
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if time.monotonic() - self._at < self.ttl_s:
                return self._value  # type: ignore[return-value]
            self._value = await asyncio.to_thread(self.fn)
            self._at = time.monotonic()
        return self._value
