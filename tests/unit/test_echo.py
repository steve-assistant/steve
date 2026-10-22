"""The desk's own voice is taken out of the microphone, so the user can talk over it on
the speakers (voice/echo.py).

On the speakers a live conversation muted their microphone while it talked
(half duplex), and what the user said over it was lost (7 October 2026).
"""

from __future__ import annotations

import numpy as np
import pytest

import tests.gate.voice_extra  # noqa: F401 - skips this module without the voice extra
from voice import audio as audio_io
from voice import echo, listen

SR = echo.SR


def _rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def _run(canceller, far, near):
    import soxr

    far48 = soxr.resample(far, SR, 48_000).astype(np.float32)
    out = []
    for i in range(0, len(far) - len(far) % 1600, 1600):
        canceller.far(far48[i * 3:(i + 1600) * 3], 48_000)
        out.append(canceller.near(near[i:i + 1600]))
    return np.concatenate(out)


def test_its_own_voice_is_taken_out_of_the_microphone():
    pytest.importorskip("livekit")
    canceller = echo.EchoCanceller()
    t = np.arange(SR * 3) / SR
    far = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    heard = np.concatenate([np.zeros(int(0.08 * SR), np.float32), 0.5 * far])[:len(far)]

    out = _run(canceller, far, heard)

    assert canceller.available and canceller.cancelled > 0
    assert _rms(out[SR:]) < _rms(heard[SR:]) / 20, "the echo was not taken out"


def test_switched_off_the_microphone_passes_untouched(monkeypatch):
    monkeypatch.setenv(echo.SWITCH_ENV, "0")
    canceller = echo.EchoCanceller()
    block = np.full(1600, 0.1, np.float32)
    assert not canceller.available and canceller.near(block) is block


class _Canceller:
    def __init__(self):
        self.seen = []

    def near(self, block):
        self.seen.append(len(block))
        return block * 0


def test_the_ear_hears_the_microphone_through_the_canceller():
    pushed, raws = [], []

    def push(self, block, raw=None):
        raws.append(raw)
        pushed.append(block)

    listener = type("L", (), {"push": push, "forget": lambda self: None})()
    ears = listen.Ears(listener, signed_in=lambda: True, locked=lambda: False,
                       canceller=_Canceller())
    ears._running = True
    ears._queue.put(np.full(1600, 0.2, np.float32))

    import threading
    import time

    worker = threading.Thread(target=ears._process)
    worker.start()
    # Until the thread has done it, up to 30 s, rather than a hundred waits of
    # 10 ms: when the thread runs is the machine's to say, and a second is
    # not long on a loaded one.
    deadline = time.monotonic() + 30
    while not pushed and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    ears._running = False
    worker.join(30)

    assert ears.canceller.seen == [1600] and not pushed[0].any(), "cleaned, then heard"
    assert raws[0] is not None and raws[0].any(), "the block as it was, beside it"


def test_the_player_tells_the_canceller_what_it_plays():
    """Each block, silence included -- from a thread of the player's own,
    not the audio thread's (9 October 2026 retest: a callback that waited
    on the canceller's lock left the speakers with nothing to play)."""
    import threading

    player = audio_io.Player()
    player.device_rate = 48_000
    told = []
    heard = threading.Event()

    def far(block, rate):
        told.append((len(block), rate, threading.current_thread().name))
        heard.set()

    player.on_play = far
    player._state.chunks.append(np.full(480, 0.5, np.float32))
    out = np.zeros((960, 1), np.float32)

    player._callback(out, 960, None, None)

    assert heard.wait(10)
    assert told == [(960, 48_000, "player-far")], "the block, silence included, not on its thread"


def test_with_the_echo_out_a_live_conversation_hears_them_while_it_talks(monkeypatch):
    from agent.worker import desk

    monkeypatch.setitem(desk._state, "echo", type("C", (), {"available": True})())
    assert desk.echo_cancelled()
    monkeypatch.setitem(desk._state, "echo", None)
    assert not desk.echo_cancelled()
