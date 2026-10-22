"""An ASGI fake, served on this computer for the length of a block.

Each HTTP fake (OpenAI, Jev, Telegram) is a Starlette app; a worker in its
own process reaches it only over a socket. uvicorn in a daemon thread, on a
port the system chooses: a fixed port would be taken by the next run, or
by the other install on this computer.

The screens' tests serve the app's pages the same way (tests/unit/ui/rig.py
`serve`).
"""

from __future__ import annotations

import contextlib
import socket
import socketserver
import ssl
import threading
import time
from collections.abc import Iterator

# A test's server stops when its test ends: nothing it serves needs to
# drain. With uvicorn's default, which waits without limit, the stop
# waited until `join(10)` gave up. 30 of the 168 screen tests spent 10 s
# each there on 19 October, about 5 minutes of the suite's 34. Each time,
# uvicorn's own lists of connections and tasks were empty, and the wait
# was on asyncio closing the browser's sockets (`Server.wait_closed`).
STOP_WITHIN_S = 0.5


@contextlib.contextmanager
def in_thread(app, name: str) -> Iterator[tuple[int, object]]:
    """`app` served on 127.0.0.1 in a thread until the block ends; gives its
    port and its uvicorn server."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off",
                                           timeout_graceful_shutdown=STOP_WITHIN_S))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError(f"{name} did not start")
        time.sleep(0.01)
    try:
        yield port, server
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


@contextlib.contextmanager
def served(app, name: str) -> Iterator[int]:
    """`app` served on 127.0.0.1 until the block ends; gives its port."""
    with in_thread(app, name) as (port, _server):
        yield port


@contextlib.contextmanager
def served_tls(app, tls: ssl.SSLContext, name: str) -> Iterator[int]:
    """A WSGI `app` (Radicale behind a provider's habits, a calendar feed)
    served over TLS on 127.0.0.1 until the block ends; gives its port. A
    backend in its own process reaches it as a provider's server, by name,
    through the world's network (tests/fakes/worldnet.py), so `tls` holds
    the certificate for that name."""
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

    class Handler(WSGIRequestHandler):
        def setup(self) -> None:
            # In the connection's own thread: done in `accept`, one slow
            # handshake would hold up every other connection.
            self.request.do_handshake()
            super().setup()

        def log_message(self, format: str, *args) -> None:  # noqa: A002 - the base's name
            pass

    class Server(socketserver.ThreadingMixIn, WSGIServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    server.set_app(app)
    server.base_environ["HTTPS"] = "on"
    server.socket = tls.wrap_socket(server.socket, server_side=True,
                                    do_handshake_on_connect=False)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True, name=name)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)
