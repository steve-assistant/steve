"""The voice's level reaches the app while Steve listens or speaks.

The mark showed the same still diamond whether the microphone heard the
user or nothing at all, and nothing while Steve spoke (voice/level.py). The
audio callbacks keep each block's level; the worker tells every open window
a few times a second (agent/worker/background.py, `voice_level`).
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from agent import runtime
from agent.worker import background
from voice import audio, level


def _db(decibels: float) -> float:
    """An RMS at this many decibels below full scale."""
    return 10 ** (decibels / 20)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(level, "LAST", {"heard": (0.0, 0.0), "played": (0.0, 0.0)})


def test_the_level_is_drawn_from_a_quiet_room_to_a_raised_voice():
    """Speech's range fills the mark's: below a quiet room is nothing, a
    raised voice is the most, and halfway between is half."""
    assert level.scaled(0.0) == 0.0
    assert level.scaled(_db(-70)) == 0.0
    assert level.scaled(_db(-35)) == 0.5
    assert level.scaled(_db(-15)) == 1.0
    assert level.scaled(1.0) == 1.0


def test_listening_comes_before_speaking_and_a_stream_that_stopped_has_no_level():
    """A press stops what is being said, and its click plays as the
    microphone opens: the mark listens. A level a moment old is a stream
    that stopped calling back, and the mark settles."""
    level.LAST["played"] = (100.0, _db(-35))
    assert level.now(100.1) == ("speaking", 0.5)

    level.LAST["heard"] = (100.05, _db(-15))
    assert level.now(100.1) == ("listening", 1.0)

    assert level.now(100.1 + level.FRESH_S) is None


def test_the_microphones_and_the_speakers_blocks_each_keep_their_level():
    """Each audio callback keeps the level of the block it just took or
    played, in one assignment: the speakers' callback must take no lock it
    could wait on."""
    recorder = audio.Recorder()
    recorder._callback(np.full((480, 1), 0.25, np.float32), 480, None, None)
    player = audio.Player()
    player.device_rate = 48_000
    player._state.chunks.append(np.full(480, 0.5, np.float32))
    player._state.owners.append(object())
    player._callback(np.zeros((960, 1), np.float32), 960, None, None)

    heard_at, heard = level.LAST["heard"]
    played_at, played = level.LAST["played"]
    assert heard == pytest.approx(0.25) and played == pytest.approx(0.5)
    assert time.monotonic() - min(heard_at, played_at) < 5


class _Window:
    """An open window's socket: what it was sent."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


async def test_every_open_window_hears_the_level_while_there_is_one_and_once_as_it_stops(
        monkeypatch):
    """Eight times a second to every window while the voice listens or
    speaks; once more as it stops, so each mark settles; then nothing, so a
    quiet worker sends nothing at all."""
    windows = [_Window(), _Window()]
    monkeypatch.setattr(runtime, "listeners", set(windows))

    assert background.tell_level(None) is None
    level.keep("played", _db(-35))
    told = background.tell_level(None)
    level.LAST["played"] = (0.0, 0.0)
    told = background.tell_level(told)
    assert background.tell_level(told) is None
    await asyncio.sleep(0)

    for window in windows:
        assert window.sent == [{"type": "level", "of": "speaking", "level": 0.5},
                               {"type": "level", "of": None, "level": 0.0}]
