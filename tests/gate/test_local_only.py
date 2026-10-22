"""No web page can drive the agent through localhost.

See core/localonly.py. CORS only ever stopped a page reading a response: a
`text/plain` POST to `/actions/decide` still ran, and a websocket to `/ws`
was never covered at all, so any page the user visited could chat with the
agent as them and read the answers. The end-to-end checks against a running
supervisor are in test_zero_downtime.py, next to the supervisor they need;
these pin the rule and that both processes apply it.
"""

from __future__ import annotations

import pytest

from core import localonly


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "null", "http://localhost:8080", "file://"]
)
def test_a_page_from_anywhere_else_is_refused(origin):
    assert localonly.refusal({"origin": origin, "host": "127.0.0.1:8790"}) is not None


@pytest.mark.parametrize("origin", sorted(localonly.ALLOWED_ORIGINS))
def test_the_tray_is_let_in(origin):
    assert localonly.refusal({"origin": origin, "host": "127.0.0.1:8790"}) is None


def test_a_client_that_is_not_a_browser_page_is_let_in():
    """curl, the tests, the supervisor's own proxy to a worker: no Origin."""
    assert localonly.refusal({"host": "127.0.0.1:8790"}) is None
    assert localonly.refusal({}) is None


@pytest.mark.parametrize(
    "host", ["evil.example:8790", "evil.example", "192.168.1.20:8790", "127.0.0.1.evil.example"]
)
def test_a_rebound_hostname_is_refused(host):
    """DNS rebinding makes the attacker's page same-origin with itself, so it
    sends no foreign Origin -- but its Host is still the attacker's name."""
    assert localonly.refusal({"host": host}) is not None


@pytest.mark.parametrize("host", ["127.0.0.1:8790", "localhost:8790", "[::1]:8790", "localhost"])
def test_loopback_names_are_this_machine(host):
    assert localonly.refusal({"host": host}) is None


def test_the_wildcard_is_never_allowed():
    assert "*" not in localonly.ALLOWED_ORIGINS


def test_the_supervisor_checks_before_anything_else_runs():
    from core import supervisor

    assert supervisor.app.user_middleware[0].cls is localonly.LocalOnly
    assert set(supervisor.ALLOWED_ORIGINS) == set(localonly.ALLOWED_ORIGINS)


def test_every_worker_checks_too():
    """A worker listens on its own port as well as behind the supervisor, so
    without its own check a page that found the port skipped the
    supervisor's."""
    import agent.server as server

    assert server.app.user_middleware[0].cls is localonly.LocalOnly


def test_every_method_a_worker_route_takes_the_apps_pages_may_send():
    """The app's pages are on an origin of their own, so the browser asks
    the supervisor first whether a method may be sent. PUT was left off its
    list: saving the user's name, in setup and on Profile, failed in the
    app as "Failed to fetch", while every screen test, on the backend's own
    origin, passed. The exit walk found it."""
    from starlette.middleware.cors import CORSMiddleware

    from agent import server
    from core import supervisor

    cors = next(m for m in supervisor.app.user_middleware if m.cls is CORSMiddleware)
    allowed = set(cors.kwargs["allow_methods"]) | {"HEAD"}
    taken = {method for route in server.app.routes
             for method in getattr(route, "methods", None) or ()}
    assert taken <= allowed, taken - allowed
