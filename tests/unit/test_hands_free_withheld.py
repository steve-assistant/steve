"""Hands-free is not offered in v0.1: no control shows it, and the worker refuses it.

On the maintainer's recordings, 16 October 2026, hands-free's front end cut
24 of 60 requests before their last word, and no wake check fits a laptop's
CPU (docs/plan/07-voice.md, section 1). So push-to-talk is everyone's voice,
and hands-free waits for its own step. Hiding its switch in the app was not
enough on its own: a `listening.json` left on by a developer's run, or any
POST to /voice/listen, would still have opened the microphone to listen all
day. The worker refuses it unless a developer sets STEVE_HANDS_FREE=1,
which no screen shows.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agent import runtime
from agent.worker import desk, links, routes

ROOT = Path(__file__).resolve().parents[2]

# What would put hands-free in front of a user: its route, the tray's
# event and command, the API call, its hotkey's setting, and its words.
CONTROLS = re.compile(r"/voice/listen|hands-free-toggle|set_listening|setHandsFree|"
                      r"LISTEN_HOTKEY|listen-hotkey|Listen for|Hands-free:", re.IGNORECASE)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """A worker with hands-free not offered, and what it was asked to load."""
    monkeypatch.delenv("STEVE_HANDS_FREE", raising=False)
    monkeypatch.setattr(desk, "state_file", lambda: tmp_path / "listening.json")
    monkeypatch.setattr(runtime, "voice", None)
    monkeypatch.setitem(desk._state, "last_switch", None)
    loaded: list[str] = []

    async def ensure_voice():
        loaded.append("voice")

    monkeypatch.setattr(links, "ensure_voice", ensure_voice)
    app = Starlette(routes=[Route("/voice/listen", routes.voice_listen, methods=["POST"])])
    return TestClient(app), loaded, tmp_path / "listening.json"


@pytest.mark.parametrize("body", [{"on": True}, {"toggle": True, "by": "hotkey"}])
def test_switching_hands_free_on_is_refused_and_loads_nothing(worker, body):
    """A request from anything, the app's old controls included, must not
    open the microphone, nor load the speech models for it."""
    client, loaded, state = worker

    response = client.post("/voice/listen", json=body)

    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": desk.NOT_OFFERED}
    assert loaded == [] and not state.exists()


async def test_the_worker_itself_refuses_to_switch_it_on(worker):
    """The route is one caller; the switch is the gate."""
    _, _, state = worker

    with pytest.raises(desk.NotOffered):
        await desk.switch(True, by="test")

    assert not state.exists() and desk.status()["on"] is False


async def test_a_switch_left_on_by_a_developers_run_opens_no_microphone(worker, monkeypatch):
    """The data folder keeps `listening.json` across versions: left on, it
    would have opened the ear at the worker's start."""
    _, _, state = worker
    state.write_text(json.dumps({"on": True}), encoding="utf-8", newline="\n")
    monkeypatch.setattr(runtime, "voice", object())
    built: list[str] = []
    monkeypatch.setattr(desk, "_build_ear", lambda eng, loop: built.append("ear"))
    monkeypatch.setattr(desk, "engine", lambda: None)

    await desk.start()

    assert desk.switched_on() is False and built == []
    assert desk.status()["offered"] is False


async def test_switching_it_off_still_works_where_it_is_not_offered(worker):
    """Off must always be reachable, so that a switch left on can be cleared."""
    _, _, state = worker

    said = await desk.switch(False, by="test")

    assert said["on"] is False
    assert json.loads(state.read_text(encoding="utf-8"))["on"] is False


async def test_a_developer_who_sets_the_setting_can_switch_it_on(worker, monkeypatch):
    """The hands-free step is built on this code: it stays reachable."""
    client, loaded, _ = worker
    monkeypatch.setenv("STEVE_HANDS_FREE", "1")

    response = client.post("/voice/listen", json={"on": True})

    assert response.status_code == 200 and response.json()["on"] is True
    assert loaded == ["voice"] and desk.status()["offered"] is True


@pytest.mark.parametrize("path", [
    *sorted((ROOT / "ui/src").glob("*.ts*")),
    ROOT / "ui/src-tauri/src/lib.rs",
], ids=lambda path: path.name)
def test_the_app_shows_no_hands_free_switch_hotkey_or_status(path):
    """No control means no user switches it on, and none is told it exists."""
    found = [f"{n}: {line.strip()}"
             for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
             if CONTROLS.search(line)]

    assert found == []


async def test_the_model_is_not_sent_to_a_switch_the_app_does_not_have(tmp_path, monkeypatch):
    """Asked for notes of the room, the desk told the model "the user can
    turn it on in the app", and the model sent the user looking for a
    setting v0.1 does not show."""
    from core import db

    conn = db.open_store(tmp_path / "desk.db")
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.delenv(desk.OFFERED_ENV, raising=False)
    monkeypatch.setitem(desk._state, "listener", None)

    said = await desk.notes_asked("room", True, channel="voice", asked="take notes on this")

    assert not said["ok"]
    assert "does not offer hands-free" in said["error"]
    assert "in the app" not in said["error"]
    conn.close()
