"""Installing the local readers from the app (agent/worker/readers.py).

When Jev can't answer, PIGuard and Tev1 read new mail and pages in its
place; without them such an item is cut short. They were installed only by
commands a user of the app does not have. What must hold:
- an install runs both setups, off the request, once, and says when it is
  done or how it failed;
- asked again while one runs, it starts nothing, since a second would fetch
  the same gigabytes beside the first;
- the state says which reader is here, so the page shows what is missing.

The setups fetch about 7 GB; small fakes stand in for them here.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from agent import wake
from agent.worker import readers
from core import decider, detector


class Asked:
    """The little of a Starlette request the routes read."""

    async def json(self):
        return {}


class _Setups:
    """Both setups, held at `gate` until the test lets them finish."""

    def __init__(self, *, fails: str | None = None):
        self.gate = threading.Event()
        self.ran: list[str] = []
        self.fails = fails

    def setup(self, name: str):
        def run():
            self.gate.wait(timeout=10)
            self.ran.append(name)
            if self.fails == name:
                raise RuntimeError(f"{name} hashed wrong")
        return run


@pytest.fixture
def setups(monkeypatch):
    fake = _Setups()
    monkeypatch.setattr(readers, "_install", {"thread": None, "error": None})
    monkeypatch.setattr(detector, "setup", fake.setup("piguard"))
    monkeypatch.setattr(decider, "setup", fake.setup("tev1"))
    monkeypatch.setattr(detector, "installed", lambda: "piguard" in fake.ran)
    monkeypatch.setattr(decider, "installed", lambda: "tev1" in fake.ran)
    monkeypatch.setattr(wake, "kept_awake", _Awake)
    return fake


class _Awake:
    """`wake.kept_awake` without Windows' power request."""

    def __init__(self, why: str):
        self.why = why

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


async def _state() -> dict:
    return json.loads((await readers.state(Asked())).body)


async def _finished() -> None:
    thread = readers._install["thread"]
    await asyncio.to_thread(thread.join, 10)
    assert not thread.is_alive()


async def test_an_install_runs_both_setups_once_and_says_when_both_are_here(setups):
    """The page polls the state: installing while the setups run, then both
    here. A second press meanwhile starts no second install."""
    said = await _state()
    assert (said["piguard"], said["tev1"], said["installing"]) == (False, False, False)

    assert json.loads((await readers.install(Asked())).body) == {"ok": True}
    first = readers._install["thread"]
    await readers.install(Asked())
    assert readers._install["thread"] is first
    assert (await _state())["installing"] is True

    setups.gate.set()
    await _finished()
    said = await _state()
    assert setups.ran == ["piguard", "tev1"]
    assert (said["piguard"], said["tev1"], said["installing"], said["error"]) == (
        True, True, False, None)


async def test_a_failed_install_says_why_and_can_be_tried_again(setups):
    """A hash that didn't match leaves the reader missing and says so; the
    next press starts afresh, with the old failure cleared."""
    setups.fails = "tev1"
    setups.gate.set()
    await readers.install(Asked())
    await _finished()
    said = await _state()
    assert (said["piguard"], said["installing"]) == (True, False)
    assert "tev1 hashed wrong" in said["error"]

    setups.fails = None
    await readers.install(Asked())
    assert (await _state())["error"] is None
    await _finished()
    assert (await _state())["tev1"] is True


async def test_the_state_says_what_the_readers_add_and_take():
    said = await _state()
    assert "Jev" in said["adds"] and said["takes"].startswith("It takes about 7 GB")
