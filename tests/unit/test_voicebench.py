"""The voice bench's scorers, on seconds of synthetic audio with known answers.

The bench decides whether hands-free ships (docs/plan/07-voice.md, section
2), so a scorer that miscounts would pass a desk that cuts people off. Each
test plants the thing a row counts -- a miss, a false wake, an early cut, a
late end -- and checks it is counted once. Speech here is noise loud enough
for the fake voice detector, so no test needs a model or a microphone.
"""

from __future__ import annotations

import json
import sys
import types

import numpy as np
import pytest

from devtools.voicebench import record, sets, turns, wake
from voice import listen

SR = 16_000


class _Vad:
    """Speech wherever a 32 ms step is loud: what the synthetic takes hold."""

    STEP = 512

    def __init__(self):
        self.reset()

    def reset(self):
        self._pending = np.zeros(0, np.float32)

    def push(self, audio):
        self._pending = np.concatenate([self._pending, np.asarray(audio, np.float32)])
        out = []
        while len(self._pending) >= self.STEP:
            step, self._pending = self._pending[:self.STEP], self._pending[self.STEP:]
            out.append(float(np.sqrt((step ** 2).mean())) > 0.05)
        return out


class _Judge:
    def __init__(self, chance):
        self.chance = chance
        self.asked = 0

    def __call__(self, audio):
        self.asked += 1
        return self.chance


def _speech(seconds, seed=0):
    return np.random.default_rng(seed).normal(0, 0.3, int(seconds * SR)).astype(np.float32)


def _quiet(seconds):
    return np.zeros(int(seconds * SR), np.float32)


def _take(*parts, name="t.wav", two_part=False):
    """A take of speech and quiet; Enter is pressed 0.2 s after the last part."""
    audio = np.concatenate([_quiet(1.0), *parts, _quiet(1.5)])
    enter = (SR * 1.0 + sum(len(p) for p in parts)) / SR + 0.2
    return sets.Clip(name=name, audio=audio, enter_s=enter, two_part=two_part)


def test_a_pause_longer_than_the_timer_is_one_early_cut_and_a_short_request_is_not():
    """The early-cut row is what decides how long the desk waits. A take
    whose pause outlasts the timer must count once, and a take with no pause
    must count as held, with the timer's wait as its hold."""
    paused = _take(_speech(1.0), _quiet(1.0), _speech(1.0, seed=1), name="paused.wav",
                   two_part=True)
    plain = _take(_speech(1.5), name="plain.wav")
    policy = turns.Policy("timer", timer_s=0.8)

    results = [turns.run(c, vad=_Vad(), judge=None, policy=policy) for c in (paused, plain)]
    row = turns.rows(results)

    assert [t.early for t in results] == [True, False]
    assert row["early"] == 1 and row["early_two_part"] == 1
    assert results[1].ended_by == "timer"
    assert 0.75 <= results[1].hold_s <= 0.95
    assert turns.pauses(results, timers=(0.8, 1.2)) == {0.8: 1, 1.2: 0}


def test_speech_after_enter_is_not_the_requests_last_word():
    """Enter marks the end of the request; what follows is a cough, or the
    next thought. Taken as the last word, it would make a turn that ended
    on time look cut early."""
    request = _take(_speech(1.5))
    coughed = sets.Clip("coughed.wav", np.concatenate([request.audio, _speech(0.5, seed=2),
                                                       _quiet(1.0)]),
                        enter_s=request.enter_s)
    result = turns.run(coughed, vad=_Vad(), judge=None, policy=turns.Policy("timer", timer_s=0.8))

    assert result.last_s < coughed.enter_s
    assert not result.early


def test_a_judge_that_never_says_finished_ends_at_the_longest_pause():
    """A late end is the other half of the turn rows: a judge that always
    says "not yet" must show as a hold of the longest pause, ended by the
    cap, not as a quick turn."""
    judge = _Judge(0.1)
    result = turns.run(_take(_speech(1.5)), vad=_Vad(), judge=judge, policy=turns.TODAY)

    assert result.ended_by == "cap"
    assert judge.asked == len(listen.TURN_ASK_S)
    assert listen.END_SILENCE_MAX_S - 0.1 <= result.hold_s <= listen.END_SILENCE_MAX_S + 0.15
    assert [a for a, _ in result.asks] == pytest.approx(list(listen.TURN_ASK_S), abs=0.1)


def test_a_candidate_policy_is_put_back_after_its_run_even_when_the_run_fails():
    """The candidate's numbers are set on `voice/listen` itself. Left there,
    the next candidate, or the desk in the same process, would run them."""
    before = (listen.TURN_ASK_S, listen.END_SILENCE_MAX_S, listen.END_SILENCE_S)
    odd = turns.Policy("odd", ask_s=(0.1,), max_s=9.0, timer_s=7.0)
    with pytest.raises(RuntimeError), turns.applied(odd):
        assert listen.END_SILENCE_S == 7.0
        raise RuntimeError("a judge failed")
    after = (listen.TURN_ASK_S, listen.END_SILENCE_MAX_S, listen.END_SILENCE_S)
    assert after == before


def test_an_ear_that_reads_its_own_clock_fails_the_run(monkeypatch):
    """A run is minutes of audio in seconds. An ear that read the wall clock
    would see every pause as instant and never end a turn by its length,
    so the clock the bench gives it fails when read, rather than lying."""
    push = listen.Ear.push

    def reads_its_clock(self, audio, now=None, raw=None):
        self.clock()
        return push(self, audio, now=now, raw=raw)

    monkeypatch.setattr(listen.Ear, "push", reads_its_clock)
    with pytest.raises(AssertionError, match="recording's clock"):
        turns.run(_take(_speech(1.0)), vad=_Vad(), judge=None,
                  policy=turns.Policy("timer", timer_s=0.8))


class _Clicks:
    """A wake detector that fires on every block holding a click (a sample
    over 0.9). It has no refractory of its own, as a candidate detector may
    not, so only the bench's can count two close clicks as one wake."""

    def reset(self):
        pass

    def push(self, block):
        return 0.99 if np.max(np.abs(block)) >= 0.9 else None


def _clicked(seconds, at=()):
    audio = _quiet(seconds)
    for t in at:
        audio[int(t * SR)] = 0.95
    return audio


def test_a_planted_miss_and_a_false_wake_are_each_counted_once():
    """The miss and false-wake rows are the bar's first two. Three takes
    with a wake and one without must be one miss; clicks 0.5 s apart in
    ordinary sound are one false wake, as the desk would act on one."""
    positives = [sets.Clip(f"{i}.wav", _clicked(2.5, at=[1.2]), section="plain") for i in range(3)]
    positives.append(sets.Clip("silent.wav", _clicked(2.5), section="plain"))
    results = wake.takes(_Clicks(), positives, phrase_end=lambda clip: 1.2)
    rows = wake.miss_rows(results)

    assert rows["by_section"] == {"plain": {"takes": 4, "missed": 1}}
    assert all(0 <= t.late_s <= 0.1 for t in results if t.heard)

    room = sets.Clip("room.wav", _clicked(36.0, at=[3.0, 3.5, 20.0]))
    count, hours = wake.false_wakes(_Clicks(), [room])
    assert count == 2
    assert hours == pytest.approx(0.01)


def test_the_check_accepts_only_the_phrase_at_the_start_and_is_timed_on_the_given_clock():
    """The check's row is what it lets through and how long it takes. The
    phrase anywhere but the start is a false wake to the desk
    (core/desk.wake_phrase), and the time must be the check's alone."""
    heard = {"a.wav": "Hey Steve, what time is it?", "b.wav": "they say Steve is late",
             "c.wav": "hey steve"}
    clips = [sets.Clip(name, _quiet(0.1)) for name in heard]
    ticks = iter([0.0, 0.25, 1.0, 1.25, 2.0, 2.5])
    order = iter(heard.values())
    checked = wake.check(lambda audio: next(order), clips, clock=lambda: next(ticks))

    assert [c.accepted for c in checked] == [True, False, True]
    rows = wake.check_rows(positives=[checked[0], checked[2]], negatives=[checked[1]])
    assert rows["rejected"] == 0 and rows["accepted"] == 0
    assert rows["p50_s"] == pytest.approx(0.25)


def test_whisper_as_a_check_is_asked_for_on_the_cpu(monkeypatch):
    """The product's laptop has no GPU, so a check timed on one would pass
    a bar it cannot meet there."""
    made = {}

    class WhisperModel:
        def __init__(self, name, **kwargs):
            made.update(kwargs, name=name)

    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=WhisperModel))
    wake.whisper("tiny", threads=2)
    assert made["device"] == "cpu" and made["cpu_threads"] == 2


def test_a_session_folder_reads_as_clips_with_its_labels_and_one_not_asked_for_is_not_opened(
        tmp_path):
    """Every scorer reads the recorder's folders through `sets`, so a label
    read wrongly there is wrong everywhere. A session still being recorded
    must not be opened at all when another is asked for."""
    root = tmp_path / "out"
    b = root / "20261016-0740-b"
    record.write_wav(b / "01.wav", _speech(1.0))
    (b / "manifest.json").write_text(json.dumps({
        "session": "b", "takes": [{"file": "01.wav", "situation": "x", "two_part": True,
                                   "enter_s": 0.9}]}), encoding="utf-8", newline="\n")
    d = root / "20261016-0756-d"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{ half written", encoding="utf-8", newline="\n")

    found = sets.find(root, kinds="b")

    assert list(found) == ["b"]
    clip = found["b"][0].clips[0]
    assert (clip.enter_s, clip.two_part, clip.seconds) == (0.9, True, 1.0)


def test_the_loopback_is_brought_to_16_khz_and_an_odd_rate_is_refused():
    """Session C's loopback is recorded at the speakers' 48 kHz. Read at
    16 kHz unconverted, it would be three times too long and out of step
    with the microphone."""
    assert len(sets.to_16k(np.ones(4800, np.float32), 48_000)) == 1600
    with pytest.raises(ValueError):
        sets.to_16k(np.ones(441, np.float32), 44_100)
