"""A test's server stops when its test ends (tests/fakes/served.py).

The fakes and the screens' pages are served by uvicorn in a thread. Left
to uvicorn's default, a stop waits without limit for every request still
being answered and every socket still closing, and the thread's
`join(10)` gave up first: 30 of the screens' 168 tests spent their last
10 s there on 19 October.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

from tests.fakes.served import served


def test_a_tests_server_stops_within_a_second_with_a_request_still_being_answered():
    """A request the test left unanswered, a page's poll or a stream, is
    nothing the next test needs; waiting on it cost each such test 10 s."""
    arrived = threading.Event()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            arrived.set()
            await asyncio.sleep(3600)

    with served(app, "a slow server") as port:
        client = socket.create_connection(("127.0.0.1", port))
        client.sendall(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
        assert arrived.wait(5), "the request never reached the app"
        stopping = time.monotonic()
    stopped_in = time.monotonic() - stopping
    client.close()

    assert stopped_in < 2, f"the stop took {stopped_in:.1f} s"
