"""The supervisor's websocket proxy closes quietly when the app has gone,
and says why when the worker did not take a socket.

Quitting the app from its tray (the live run of 13 October 2026) left two
"Exception in ASGI application" tracebacks in the backend's log, one for
each of the app's sockets: the proxy closed a socket whose client had
already disconnected, and Starlette raises then. Nothing broke, but that log
is what the app shows when the backend keeps stopping, and every quit would
put an error there that is not one.
"""

from __future__ import annotations

import logging
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


class _App(_GoneApp):
    """A socket whose app is still there: its closes, by code."""

    def __init__(self) -> None:
        super().__init__()
        self.codes: list[int] = []

    async def close(self, code: int = 1000) -> None:
        self.codes.append(code)


async def test_a_socket_the_worker_does_not_take_is_closed_to_be_tried_again_and_the_log_says_why(
        monkeypatch, caplog):
    """On CI (22 October 2026) a chat's socket closed before its hello with
    an ordinary close, and the log said nothing: the proxy noted why only at
    debug level. The app reconnects on any close, but the log is where the
    next one is read, and "try again later" is what the proxy says when it
    has no worker at all."""
    worker = _Worker(_unused_port())
    monkeypatch.setattr(supervisor.state, "active", worker)
    app = _App()

    with caplog.at_level(logging.INFO, logger="supervisor"):
        await supervisor.ws_proxy(app)

    assert app.codes == [1013]
    [said] = [r for r in caplog.records if r.name == "supervisor"]
    assert said.levelno == logging.WARNING
    assert f"worker :{worker.port} did not take an app socket" in said.getMessage()
    assert worker.inflight == 0


def test_the_proxy_drops_every_hop_by_hop_field_http_names():
    """A hop-by-hop field is for one connection, never passed on (RFC 9110,
    7.6.1). The list had "trailers", a misspelling RFC 2616 carried, and not
    the field's name, "trailer"."""
    named = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
             "trailer", "transfer-encoding", "upgrade"}

    assert named <= supervisor.HOP_BY_HOP
