"""The supervisor's websocket proxy closes quietly when the app has gone.

Quitting the app from its tray (the live run of 13 October 2026) left two
"Exception in ASGI application" tracebacks in the backend's log, one for
each of the app's sockets: the proxy closed a socket whose client had
already disconnected, and Starlette raises then. Nothing broke, but that log
is what the app shows when the backend keeps stopping, and every quit would
put an error there that is not one.
"""

from __future__ import annotations

import socket

from starlette.websockets import WebSocketDisconnect

from core import supervisor


class _GoneApp:
    """A socket whose app has quit: closing it raises, as Starlette's does
    once the client has disconnected."""

    scope = {"type": "websocket", "subprotocols": ["steve"]}

    def __init__(self) -> None:
        self.closes = 0

    async def accept(self, subprotocol: str | None = None) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        self.closes += 1
        raise WebSocketDisconnect(code=1006)


class _Worker:
    alive = True

    def __init__(self, port: int) -> None:
        self.port = port
        self.inflight = 0


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_a_socket_whose_app_has_quit_is_closed_without_an_error(monkeypatch):
    """The worker is gone too (nothing listens on its port), as on a quit:
    the proxy ends, tries the close, and raises nothing for the log."""
    worker = _Worker(_unused_port())
    monkeypatch.setattr(supervisor.state, "active", worker)
    app = _GoneApp()

    await supervisor.ws_proxy(app)

    assert app.closes == 1
    assert worker.inflight == 0


def test_the_proxy_drops_every_hop_by_hop_field_http_names():
    """A hop-by-hop field is for one connection, never passed on (RFC 9110,
    7.6.1). The list had "trailers", a misspelling RFC 2616 carried, and not
    the field's name, "trailer"."""
    named = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
             "trailer", "transfer-encoding", "upgrade"}

    assert named <= supervisor.HOP_BY_HOP
