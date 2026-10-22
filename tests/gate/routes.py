"""Every route the two apps answer, read from the apps themselves.

A list of routes written by hand misses the one added after it, and that
is the route a check most needs to see. test_local_token.py sends each of
these a request without the token; test_doors.py asks that each be placed
on the table of guarantees (tests/e2e/doors.py).
"""

from __future__ import annotations

from starlette.routing import Route, WebSocketRoute


def apps():
    """The supervisor's app and a worker's, by name."""
    import agent.server as server
    from core import supervisor

    return [("supervisor", supervisor.app), ("worker", server.app)]


def routes():
    """(app name, route) for every route and websocket of both apps."""
    for name, app in apps():
        for route in app.routes:
            # A Mount or a Host would hide routes from this list; neither
            # app has one, and one added must be listed here first.
            assert isinstance(route, Route | WebSocketRoute), (
                f"{name} has a {type(route).__name__}: list what it holds here")
            yield name, route
