"""A swap lets the app's idle sockets go at once, and a turn under way
finish first.

The supervisor pins each socket to the worker it opened on and cannot tell
one mid-turn from one sitting idle. The app holds two open all day, so every
swap waited out the whole 30-second drain, and whatever was still going was
killed at its end -- while a research turn takes minutes (6 October 2026).
The worker can tell, and closes the idle ones as it hands over.
"""

from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent import runtime
from agent.worker import duties, routes


class _Socket:
    def __init__(self) -> None:
        self.closed_with: int | None = None

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


async def test_handing_over_closes_the_idle_sockets_and_not_one_mid_turn(monkeypatch):
    idle, busy = _Socket(), _Socket()
    monkeypatch.setattr(runtime, "listeners", {idle, busy})
    monkeypatch.setattr(runtime, "turning", {busy})
    monkeypatch.setattr(duties, "_duties", {"state": "active"})
    monkeypatch.setattr(duties, "_duty_tasks", [])
    for name in ("jobs", "signal", "voice"):
        monkeypatch.setattr(runtime, name, None)

    await duties.give_up_duties("retired")

    assert idle.closed_with == routes.SERVICE_RESTART
    assert busy.closed_with is None, "a turn under way is not cut off"


def test_a_turn_answered_after_the_handover_then_lets_its_socket_go(monkeypatch):
    """Asked before the swap, answered after it: the answer arrives, and then
    the socket closes so the app reconnects to the worker serving now."""
    seen_busy: list[bool] = []

    class _Convo:
        session_id = 7

        async def send(self, text, *, on_tool=None, images=None):
            seen_busy.append(bool(runtime.turning))
            duties._duties["state"] = "retired"  # the swap, mid-turn
            return SimpleNamespace(as_dict=lambda: {"text": "Amtrak 5:10 pm"})

    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setattr(runtime, "turning", set())
    monkeypatch.setattr(duties, "_duties", {"state": "active"})
    monkeypatch.setattr(duties, "inert", lambda: False)
    monkeypatch.setattr(routes.answering, "conversation", lambda session_id=None: _Convo())
    app = Starlette(routes=[WebSocketRoute("/ws", routes.ws_session)])

    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "chat", "text": "Which Amtrak?"})
        assert ws.receive_json()["type"] == "thinking"
        assert ws.receive_json() == {"type": "reply", "session_id": 7, "text": "Amtrak 5:10 pm"}
        # A socket left open answers this, and the test fails rather than
        # waiting for a close that never comes.
        ws.send_json({"type": "ping"})
        try:
            closed = ws.receive_json()
        except WebSocketDisconnect as exc:
            closed = exc.code

    assert seen_busy == [True], "the turn was marked as under way"
    assert closed == routes.SERVICE_RESTART and not runtime.turning


def test_a_quiet_worker_is_not_kept_but_real_work_has_minutes():
    """The drain bounds only real work now; a turn takes up to minutes."""
    from core import supervisor

    assert supervisor.DRAIN_TIMEOUT_S >= 270
