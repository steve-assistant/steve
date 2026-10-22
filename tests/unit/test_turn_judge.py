"""The ear ends a request when the user sounds finished, not after a fixed 0.8 s of
quiet (voice/turns.py, voice/listen.Ear._finished).

"Okay, perfect... can you Google..." -- a pause to think -- was cut in two
(7 October 2026). With a judge, a pause that sounds unfinished is waited
out; one that sounds finished ends sooner; without one, nothing changes.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.gate.extras import needs_addon
from voice import listen, turns
from voice.listen import END_SILENCE_MAX_S, END_SILENCE_S, TURN_ASK_S

BLOCK = 800  # 50 ms
WAKE = 0.4321


class _Detector:
    def push(self, audio):
        return 0.9 if len(audio) and audio[0] == np.float32(WAKE) else None

    def reset(self):
        pass


class _Vad:
    STEP = 512

    def __init__(self):
        self.reset()

    def reset(self):
        self._pending = np.zeros(0, np.float32)

    def push(self, audio):
        self._pending = np.concatenate([self._pending, audio])
        out = []
        while len(self._pending) >= self.STEP:
            step, self._pending = self._pending[:self.STEP], self._pending[self.STEP:]
            out.append(float(np.sqrt((step ** 2).mean())) > 0.05)
        return out


def _quiet(n):
    return [np.full(BLOCK, 0.001, np.float32) for _ in range(n)]


def _talk(n):
    rng = np.random.default_rng(n)
    return [(0.2 * rng.standard_normal(BLOCK)).astype(np.float32) for _ in range(n)]


def _wake():
    block = _talk(1)[0]
    block[0] = WAKE
    return block


def _ear(judge):
    heard = []
    clock = {"t": 0.0}
    loopback = listen.Loopback(_Detector, _Vad(), clock=lambda: clock["t"])

    def on_event(event):
        if event.kind == "captured":
            heard.append(event.heard)

    ear = listen.Ear(detector=_Detector(), loopback=loopback, vad=_Vad(), on_event=on_event,
                     clock=lambda: clock["t"], judge=judge)
    return ear, heard, clock


def _feed(ear, clock, blocks):
    for block in blocks:
        clock["t"] += BLOCK / 16_000
        ear.push(block, now=clock["t"])


def _blocks(seconds):
    return int(seconds * 20)


class _Judge:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = 0

    def __call__(self, audio):
        self.asked += 1
        return self.answers.pop(0) if self.answers else self.answers_default

    answers_default = 0.1


def test_a_finished_sounding_request_ends_before_the_old_timer():
    ear, heard, clock = _ear(_Judge(0.9))
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(_blocks(TURN_ASK_S[0] + 0.1)))
    assert len(heard) == 1, "ended at the first question, under 0.8 s of quiet"
    assert TURN_ASK_S[0] < END_SILENCE_S


def test_a_pause_to_think_is_waited_out_and_the_rest_is_the_same_request():
    judge = _Judge(0.1, 0.2, 0.9)  # unfinished, unfinished; then, after the rest, finished
    ear, heard, clock = _ear(judge)
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(_blocks(1.5)) + _talk(10))
    assert heard == [] and ear.gathering == "capture", "cut at the pause"
    _feed(ear, clock, _quiet(_blocks(0.5)))
    assert len(heard) == 1 and judge.asked == 3, "asked twice in the pause, once after"


def test_an_unfinished_sounding_pause_ends_at_the_longest_wait():
    ear, heard, clock = _ear(_Judge(0.1, 0.1))
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(_blocks(END_SILENCE_MAX_S - 0.2)))
    assert heard == []
    _feed(ear, clock, _quiet(_blocks(0.3)))
    assert len(heard) == 1


def test_a_judge_that_cannot_say_leaves_the_timer_in_charge():
    ear, heard, clock = _ear(lambda audio: None)
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(_blocks(END_SILENCE_S + 0.1)))
    assert len(heard) == 1 and ear.judge is None


def test_without_a_judge_nothing_changes():
    ear, heard, clock = _ear(None)
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(_blocks(END_SILENCE_S - 0.15)))
    assert heard == []
    _feed(ear, clock, _quiet(_blocks(0.25)))
    assert len(heard) == 1


def test_how_a_capture_ended_and_what_the_judge_said_travel_with_it():
    """8 October 2026 audit, finding 12: the sound test cannot tune 0.4 / 1.2 /
    0.5 s if nothing records which of them ended a turn."""
    ear, heard, clock = _ear(_Judge(0.9))
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(_blocks(TURN_ASK_S[0] + 0.1)))
    (quiet, chance), = heard[0].asks
    assert heard[0].ended_by == "judge" and chance == 0.9
    assert TURN_ASK_S[0] <= quiet < END_SILENCE_S


def test_the_longest_wait_is_recorded_with_what_the_judge_said_before_it():
    ear, heard, clock = _ear(_Judge(0.1, 0.2))
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(_blocks(END_SILENCE_MAX_S + 0.3)))
    assert heard[0].ended_by == "cap" and [c for _, c in heard[0].asks] == [0.1, 0.2]


@pytest.mark.parametrize("judge", [None, lambda audio: None])
def test_the_timer_is_recorded_when_there_is_no_judge_or_it_cannot_say(judge):
    ear, heard, clock = _ear(judge)
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(_blocks(END_SILENCE_S + 0.2)))
    assert heard[0].ended_by == "timer" and heard[0].asks == ()


def test_a_request_that_never_pauses_is_recorded_as_cut_at_the_longest():
    from core import desk

    ear, heard, clock = _ear(_Judge())
    _feed(ear, clock, [_wake()] + _talk(_blocks(desk.MAX_CAPTURE_S) + 30))
    assert heard[0].ended_by == "max" and heard[0].asks == ()


def test_each_capture_starts_its_own_record():
    ear, heard, clock = _ear(_Judge(0.9, 0.9))
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(_blocks(TURN_ASK_S[0] + 0.1)))
    ear.set_mode(listen.QUIET)  # the engine's answer to the first capture
    _feed(ear, clock, _quiet(10) + [_wake()] + _talk(10) + _quiet(_blocks(TURN_ASK_S[0] + 0.1)))
    assert len(heard) == 2 and len(heard[1].asks) == 1, "the first capture's asks leaked"


def test_no_model_means_no_judge(tmp_path):
    judge = turns.TurnJudge(tmp_path / "missing.onnx")
    assert not judge.available and judge(np.zeros(16_000, np.float32)) is None


@needs_addon
def test_the_model_hears_eight_seconds_as_whisper_would():
    judge = turns.TurnJudge()
    from faster_whisper.feature_extractor import FeatureExtractor

    judge._features = FeatureExtractor(feature_size=80, sampling_rate=16_000, chunk_length=8)
    features = judge.features(np.random.default_rng(1).standard_normal(16_000 * 3))
    assert features.shape == (1, 80, 800) and features.dtype == np.float32


@pytest.mark.skipif(not (turns.model_dir() / turns.FILE).exists(),
                    reason="Smart Turn is not downloaded (python -m voice.turns setup)")
def test_the_real_model_answers_with_a_chance():
    chance = turns.TurnJudge()(np.zeros(16_000, np.float32))
    assert chance is not None and 0.0 <= chance <= 1.0
