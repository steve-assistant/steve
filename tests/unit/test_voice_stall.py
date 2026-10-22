"""A slow reply is preceded by a few words, so a quiet laptop is not taken
for one that did not hear.

The maintainer, 7 October 2026: "sometimes it goes quiet for a while before
responding" -- a reply needing a lookup took 5 to 6 s to its first word.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

import tests.gate.voice_extra  # noqa: F401 - skips this module without the voice extra
from voice import pipeline
from voice.stt import Transcript


class _Speaker:
    def say(self, text):
        return type("S", (), {"audio": np.full(10, 0.5, np.float32), "sample_rate": 24_000,
                              "duration_s": 0.5, "text": text})()


class _Player:
    def __init__(self):
        self.queued: list[str] = []

    def enqueue(self, audio, rate):
        self.queued.append("stall" if audio[0] == np.float32(0.25) else "reply")

    def wait(self, *a):
        return True


def _session(delay: float):
    session = object.__new__(pipeline.VoiceSession)

    async def reply(text):
        await asyncio.sleep(delay)
        return "It's 60 and clear.", {}

    session.reply_fn, session.speak = reply, True
    session.speaker, session.player = _Speaker(), _Player()
    session._listening = session._cut = False
    session._stalls = [(np.full(10, 0.25, np.float32), 24_000, 0.5) for _ in pipeline.STALLS]
    session._preambles = {}
    session._last_stall = -1
    session.streams, session.on_cut = False, None
    return session


def _heard():
    return Transcript(text="what's the weather", language="en", duration_s=1.0,
                      transcribe_ms=1, device="cpu")


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(pipeline, "STALL_S", 0.05)


async def test_a_slow_reply_is_preceded_by_a_few_words():
    session = _session(delay=0.3)
    await session.respond(_heard(), released=0.0)
    assert session.player.queued == ["stall", "reply"]


async def test_a_quick_reply_comes_alone():
    session = _session(delay=0.0)
    await session.respond(_heard(), released=0.0)
    await asyncio.sleep(0.15)  # past when the words would have come
    assert session.player.queued == ["reply"]


async def test_the_same_words_are_not_said_twice_in_a_row():
    session = _session(delay=0.2)
    said = []
    for _ in range(6):
        await session.respond(_heard(), released=0.0)
        said.append(session._last_stall)
    assert all(a != b for a, b in zip(said, said[1:], strict=False))


def test_the_words_are_short():
    assert all(len(s.split()) <= 4 for s in pipeline.STALLS) and len(pipeline.STALLS) >= 4
