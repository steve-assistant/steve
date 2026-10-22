"""The hands-free ear's mechanics: the wake detector's numbers, its timing,
and what it reports to the desk's engine in each mode the engine sets
(voice/listen.Ear, EarMode). What it may never do is tests/gate/test_desk.py's;
what the desk makes of what it reports is tests/unit/test_desk_scenarios.py's."""

from __future__ import annotations

import logging

import numpy as np
import pytest

from agent import runtime
from agent.worker import desk, duties
from voice import listen, wake

BLOCK = 800
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


def _ear(detector=None):
    """The ear and what it reported: the captures it handed on (`heard`), and
    every event (`events`), in order."""
    heard, events = [], []
    clock = {"t": 0.0}

    def on_event(event):
        events.append(event)
        if event.kind == "captured":
            heard.append(event.heard)

    loopback = listen.Loopback(_Detector, _Vad(), clock=lambda: clock["t"])
    ear = listen.Ear(detector=detector or _Detector(), loopback=loopback, vad=_Vad(),
                     on_event=on_event, clock=lambda: clock["t"])
    return ear, heard, events, clock


def _follow(ear, clock):
    """What the engine does after a reply: a follow-up window, FOLLOW_UP_S long."""
    ear.set_mode(listen.EarMode(follow_until=clock["t"] + listen.desk.FOLLOW_UP_S))


def _feed(ear, clock, blocks):
    for block in blocks:
        clock["t"] += BLOCK / 16_000
        ear.push(block, now=clock["t"])


def _quiet(n):
    return [np.full(BLOCK, 0.001, np.float32) for _ in range(n)]


def _talk(n):
    rng = np.random.default_rng(n)
    return [(0.2 * rng.standard_normal(BLOCK)).astype(np.float32) for _ in range(n)]


def _wake():
    block = _talk(1)[0]
    block[0] = WAKE
    return block


@pytest.fixture
def models():
    if not (wake.model_dir() / wake.MEL_FILE).exists():
        pytest.skip(f"openWakeWord's front end is not downloaded to {wake.model_dir()}")
    return wake.Frontend()


def test_streamed_features_are_the_numbers_the_head_was_trained_on(models):
    """Training computes a clip's features at once (`Frontend.stream`), the
    ear in 80 ms pieces as they arrive. They differed by up to 30 in an
    embedding on 5 October 2026: the mel model floors quiet frames against
    the loudest value in whatever it is given, batch included, and the
    windows sat one frame off the grid. A head trained on one and run on the
    other hears something it never learned."""
    rng = np.random.default_rng(0)
    speechy = np.sin(2 * np.pi * 220 * np.arange(12_000) / 16_000).astype(np.float32) * 0.8
    clip = np.concatenate([np.zeros(16_000, np.float32), speechy,
                           (0.001 * rng.standard_normal(9_000)).astype(np.float32)])
    whole = models.stream(clip)
    features = wake.Features(models)
    for i in range(0, len(clip), 777):
        features.push(clip[i:i + 777])
    live = np.stack(list(features.embeddings))
    assert live.shape == whole.shape
    assert np.abs(live - whole).max() < 1e-3


def test_a_wake_fires_once_however_long_the_phrase_scores_high():
    class Front:
        def mel_frames(self, inputs):
            return np.zeros((len(inputs), wake.MEL_STEP, 32), np.float32)

        def embed_windows(self, windows):
            return np.zeros((len(windows), wake.EMBED_DIM), np.float32)

    d = wake.WINDOW * wake.EMBED_DIM
    head = wake.Head(w1=np.zeros((d, 2), np.float32), b1=np.zeros(2, np.float32),
                     w2=np.zeros((2, 2), np.float32), b2=np.zeros(2, np.float32),
                     w3=np.zeros((2, 1), np.float32), b3=np.full(1, 5.0, np.float32),
                     mean=np.zeros(d, np.float32), scale=np.ones(d, np.float32))
    detector = wake.Detector(Front(), head)
    fired = [detector.push(np.zeros(wake.CHUNK, np.float32)) for _ in range(60)]
    times = [i for i, f in enumerate(fired) if f is not None]
    assert len(times) >= 2
    assert all(b - a >= wake.REFRACTORY_S / 0.08 for a, b in zip(times, times[1:], strict=False))


def test_the_head_is_the_same_after_saving(tmp_path):
    rng = np.random.default_rng(1)
    d = wake.WINDOW * wake.EMBED_DIM
    head = wake.Head(w1=rng.standard_normal((d, 8)).astype(np.float32),
                     b1=np.zeros(8, np.float32), w2=rng.standard_normal((8, 8)).astype(np.float32),
                     b2=np.zeros(8, np.float32), w3=rng.standard_normal((8, 1)).astype(np.float32),
                     b3=np.zeros(1, np.float32), mean=np.zeros(d, np.float32),
                     scale=np.ones(d, np.float32), threshold=0.35)
    head.save(tmp_path / "h.npz")
    again = wake.Head.load(tmp_path / "h.npz")
    head.over_voice = 0.3
    head.save(tmp_path / "measured.npz")
    measured = wake.Head.load(tmp_path / "measured.npz")
    x = rng.standard_normal((4, wake.WINDOW, wake.EMBED_DIM)).astype(np.float32)
    assert again.threshold == pytest.approx(0.35)
    assert np.allclose(head.scores(x), again.scores(x))
    # A head trained before the bar over the desk's voice was measured has
    # none, and the ear falls back to the fixed one (`listen.over_voice_bar`).
    assert again.over_voice is None and measured.over_voice == pytest.approx(0.3)


def test_a_follow_up_window_closes_when_the_user_says_nothing():
    """The window is the engine's to open and the ear's to keep to the clock:
    once FOLLOW_UP_S has passed, speech without the phrase starts nothing."""
    ear, heard, _, clock = _ear()
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(20))
    assert ear.gathering is None and len(heard) == 1

    _follow(ear, clock)
    _feed(ear, clock, _quiet(int((listen.desk.FOLLOW_UP_S + 0.5) * 20)))
    _feed(ear, clock, _talk(20) + _quiet(20))

    assert len(heard) == 1 and ear.gathering is None


def test_a_follow_up_needs_no_wake_word():
    ear, heard, _, clock = _ear()
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(20))
    _follow(ear, clock)
    _feed(ear, clock, _quiet(10) + _talk(20) + _quiet(20))
    assert len(heard) == 2 and heard[1].woke is False and heard[1].kind == "follow"


def test_an_answer_after_a_moments_thought_needs_no_wake_word():
    """Seven seconds' thought after a reply was past the old six-second
    window, and the user took it that it wanted "hey Steve" before everything."""
    ear, heard, _, clock = _ear()
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(20))
    _follow(ear, clock)
    _feed(ear, clock, _quiet(7 * 20) + _talk(20) + _quiet(20))
    assert len(heard) == 2 and heard[1].woke is False


def test_in_a_live_conversation_the_ear_marks_when_the_user_speaks():
    """What tells the conversation the user is still in it (voice/engine._user_spoke):
    speech on the microphone, by the VAD -- not GPT-Live's transcript."""
    ear, _, _, clock = _ear()
    streamed = []
    ear.set_mode(listen.EarMode(busy=True, stream=streamed.append, utterances=True))

    _feed(ear, clock, _quiet(10))
    assert ear.speech_at == 0.0 and len(streamed) == 10
    _feed(ear, clock, _talk(5))
    assert ear.speech_at == pytest.approx(clock["t"])


def test_in_a_live_conversation_a_stretch_of_their_speech_is_handed_on_as_an_utterance():
    """What the desk's own Whisper writes down, to be handed to their
    assistant in place of GPT-Live's transcript of the room: a stretch of
    speech, ended by a pause, and never a request of its own."""
    ear, heard, events, clock = _ear()
    ear.set_mode(listen.EarMode(busy=True, stream=lambda block: None, utterances=True))

    _feed(ear, clock, _quiet(10) + _talk(10) + _quiet(20))

    assert [e.kind for e in events] == ["utterance"] and heard == []
    stretch = events[0].heard
    assert stretch.kind == "utterance" and stretch.woke is False and not stretch.barge_in
    assert ear.gathering is None


def test_a_note_the_user_started_is_handed_on_a_stretch_at_a_time():
    ear, heard, events, clock = _ear()
    ear.set_mode(listen.EarMode(segments=True))

    _feed(ear, clock, _quiet(10) + _talk(10) + _quiet(20))

    assert [e.kind for e in events] == ["segment"] and heard == []
    assert events[0].heard.segment and events[0].heard.kind == "segment"


def test_with_no_note_open_speech_between_requests_is_handed_to_nothing():
    ear, _, events, clock = _ear()

    _feed(ear, clock, _quiet(10) + _talk(40) + _quiet(20))

    assert events == [] and ear.gathering is None


def test_the_phrase_said_in_a_live_conversation_still_reaches_the_engine():
    """The stream hears everything, and "hey Steve" is the one thing the
    engine must still be told (it ends the conversation): the stretch the user was
    in the middle of is handed on first, then the wake, as a cut-in."""
    ear, _, events, clock = _ear()
    ear.set_mode(listen.EarMode(busy=True, stream=lambda block: None, utterances=True))

    _feed(ear, clock, _talk(5) + [_wake()])

    assert [e.kind for e in events] == ["utterance", "wake"] and events[1].barge is True


def test_the_phrase_said_over_a_reply_is_reported_as_a_cut_in():
    """Before 9 October 2026 the ear called a callback to cut the reply off;
    now it says what it heard -- a wake, with `barge` set because a capture
    had been handed on and the engine had not yet answered -- and the engine
    decides."""
    ear, heard, events, clock = _ear()
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(20))
    assert ear.gathering is None and events[0].kind == "wake" and events[0].barge is False

    _feed(ear, clock, [_wake()])

    assert events[-1].kind == "wake" and events[-1].barge is True
    assert ear.gathering == "capture" and ear.counts.barge_ins == 1


def test_a_near_miss_of_the_wake_detector_is_a_line_in_the_log(caplog):
    """9 October 2026: two "hey Steve"s left no trace at all. A window that
    scored most of the way and was not enough is logged once, with its score
    and the bar it missed -- numbers, never words -- and counted."""

    class Near(_Detector):
        last_score = 0.8
        threshold = 0.9

    ear, heard, events, clock = _ear(Near())

    with caplog.at_level(logging.INFO, logger="listen"):
        _feed(ear, clock, _quiet(int((listen.NEAR_WAKE_S + 0.5) * 20)))

    assert [e.kind for e in events] == ["near_wake"] and events[0].score == pytest.approx(0.8)
    assert ear.counts.near_wakes == 1 and heard == []
    lines = [r.getMessage() for r in caplog.records if "near wake" in r.getMessage()]
    assert len(lines) == 1 and "0.80" in lines[0] and "0.90" in lines[0]


class _Scored:
    """A wake detector as the trained one behaves: a score per block, read
    here from the block's first sample; it fires at its bar (`threshold`,
    which the ear may lower), and for a few windows after it fires the
    phrase, sliding out of its window, still scores high but fires nothing
    -- the refractory time of voice/wake.Detector."""

    MARKS = {np.float32(WAKE): 0.95, np.float32(0.2468): 0.75, np.float32(0.3579): 0.45}
    TAIL = (0.87, 0.71, 0.55)

    def __init__(self):
        self.threshold = 0.9
        self.last_score = 0.0
        self._tail: list[float] = []
        self.pushed = 0

    def push(self, audio):
        self.pushed += 1
        if self._tail:
            self.last_score = self._tail.pop(0)
            return None
        self.last_score = self.MARKS.get(np.float32(audio[0]), 0.0) if len(audio) else 0.0
        if self.last_score >= self.threshold:
            self._tail = list(self.TAIL)
            return self.last_score
        return None

    def reset(self):
        self._tail = []


def _marked(value):
    block = _talk(1)[0]
    block[0] = value
    return block


def test_the_windows_after_a_wake_are_not_a_near_miss(caplog):
    """Every "near wake" line of the 9 October 2026 retest -- 0.71, 0.87 and
    0.79 -- was logged two seconds after a wake that counted: the windows
    after it, scoring high as the phrase slid out of them. They read as
    "hey Steve"s it missed, and they were not."""
    ear, heard, events, clock = _ear(_Scored())

    with caplog.at_level(logging.INFO, logger="listen"):
        _feed(ear, clock, [_wake()] + _talk(10) + _quiet(60))

    assert [e.kind for e in events] == ["wake", "captured"]
    assert ear.counts.near_wakes == 0
    assert not [r for r in caplog.records if "near wake" in r.getMessage()]


def test_a_near_miss_down_to_four_tenths_is_logged_with_whether_it_talked_over_them(caplog):
    ear, _, events, clock = _ear(_Scored())

    with caplog.at_level(logging.INFO, logger="listen"):
        _feed(ear, clock, [_marked(0.3579)] + _quiet(50))

    lines = [r.getMessage() for r in caplog.records if "near wake" in r.getMessage()]
    assert [e.kind for e in events] == ["near_wake"]
    assert len(lines) == 1 and "0.45" in lines[0] and "over=quiet" in lines[0]


def test_over_the_desks_own_voice_the_bar_for_the_phrase_is_lower():
    """17:27-17:29, 9 October 2026: "hey Steve, stop" over its answers was
    missed three times. The echo canceller ducks their voice under its own,
    so while the desk is audible a phrase that scores 0.75 counts; with the
    desk quiet the same score is a near miss, as before."""
    talking = {"on": True}
    ear, heard, events, clock = _ear(_Scored())
    ear.set_mode(listen.EarMode(busy=True, audible=lambda: talking["on"]))

    _feed(ear, clock, [_marked(0.2468)] + _talk(10) + _quiet(30))

    assert events[0].kind == "wake" and events[0].barge is True
    assert ear.detector.threshold == listen.WAKE_OVER_VOICE

    talking["on"] = False
    ear.set_mode(listen.EarMode())
    _feed(ear, clock, _quiet(60) + [_marked(0.2468)] + _quiet(50))

    assert [e.kind for e in events][-1] == "near_wake" and ear.detector.threshold == 0.9


def test_a_head_measured_over_the_desks_voice_brings_its_own_bar():
    """From 11 October 2026 a head is trained on the desk's own voice as
    the canceller leaves it, and carries the bar it holds over that voice
    (`wake.Head.over_voice`): the ear uses it, not the fixed 0.7 that was
    calibrated on a head that had never heard the desk."""

    class _Head:
        over_voice = 0.4

    detector = _Scored()
    detector.head = _Head()
    ear, heard, events, clock = _ear(detector)
    ear.set_mode(listen.EarMode(busy=True, audible=lambda: True))

    _feed(ear, clock, [_marked(0.3579)] + _talk(10) + _quiet(30))

    assert events[0].kind == "wake" and ear.detector.threshold == 0.4


def test_over_its_voice_the_laptops_own_sound_is_checked_at_the_same_lower_bar():
    """The rule that a wake the laptop itself played is not their
    (core/desk.wake_counts) holds at the lower bar too: the loopback's
    detector is asked at the bar the microphone's wake was heard at."""
    ear, _, events, clock = _ear(_Scored())
    ear.loopback.detector = _Scored
    ear.set_mode(listen.EarMode(busy=True, audible=lambda: True))
    blocks = [_marked(0.2468)] + _talk(10) + _quiet(30)

    for block in blocks:
        clock["t"] += BLOCK / 16_000
        ear.loopback.push(block, now=clock["t"])
        ear.push(block, now=clock["t"])

    assert [e.kind for e in events][0] == "laptop_wake"
    assert ear.counts.laptop_wakes_ignored == 1 and ear.gathering is None


def test_over_its_voice_the_raw_microphone_is_scored_beside_the_cleaned_only_while_audible(
        caplog):
    """What a sound test needs to say whether the canceller is what hides
    "hey Steve" over the desk's voice: the same detector on the microphone
    before the canceller, run only while the desk is audible, and a near
    score logged beside the cleaned one -- a near score, under the bar,
    wakes nothing."""
    raw = _Scored()
    talking = {"on": True}
    events = []
    clock = {"t": 0.0}
    loopback = listen.Loopback(_Detector, _Vad(), clock=lambda: clock["t"])
    ear = listen.Ear(detector=_Scored(), loopback=loopback, vad=_Vad(), on_event=events.append,
                     clock=lambda: clock["t"], raw_detector=raw)
    ear.set_mode(listen.EarMode(busy=True, audible=lambda: talking["on"]))

    with caplog.at_level(logging.INFO, logger="listen"):
        for cleaned, heard_raw in [(_quiet(1)[0], _marked(0.3579))] + [
                (q, q) for q in _quiet(50)]:
            clock["t"] += BLOCK / 16_000
            ear.push(cleaned, now=clock["t"], raw=heard_raw)
        talking["on"] = False
        pushed = raw.pushed
        for block in _quiet(20):
            clock["t"] += BLOCK / 16_000
            ear.push(block, now=clock["t"], raw=block)

    lines = [r.getMessage() for r in caplog.records if "raw microphone" in r.getMessage()]
    assert len(lines) == 1 and "0.45" in lines[0] and "cleaned 0.00" in lines[0], lines
    assert events == [], "a raw score under the bar woke something"
    assert raw.pushed == pushed, "it ran with the desk quiet"


def _ear_over_voice(live: bool):
    """An ear while the desk is audible: a live conversation streaming, or
    a local answer."""
    raw, events, clock = _Scored(), [], {"t": 0.0}
    loopback = listen.Loopback(_Detector, _Vad(), clock=lambda: clock["t"])
    ear = listen.Ear(detector=_Scored(), loopback=loopback, vad=_Vad(), on_event=events.append,
                     clock=lambda: clock["t"], raw_detector=raw)
    if live:
        ear.set_mode(listen.EarMode(busy=True, audible=lambda: True, stream=lambda audio: None,
                                    utterances=True))
    else:
        ear.set_mode(listen.EarMode(busy=True, audible=lambda: True))
    return ear, events, clock


@pytest.mark.parametrize("live", [True, False], ids=["live conversation", "local answer"])
def test_over_its_voice_a_wake_heard_only_on_the_raw_microphone_is_a_wake(caplog, live):
    """10 October 2026, 00:20: over GPT-Live's voice the canceller left 0.00
    of their "hey Steve, stop" and the raw microphone scored it 0.91; the call
    talked on over them. That night's rehearsal had the same over a long
    local answer -- 0.62 cleaned, 1.00 raw -- and the answer talked on for
    31 seconds. Either path is a wake, once per phrase, however long its
    tail scores high."""
    ear, events, clock = _ear_over_voice(live)

    with caplog.at_level(logging.INFO, logger="listen"):
        for cleaned, heard_raw in [(_quiet(1)[0], _marked(WAKE)), (_quiet(1)[0], _marked(WAKE))] \
                + [(q, q) for q in _quiet(10)]:
            clock["t"] += BLOCK / 16_000
            ear.push(cleaned, now=clock["t"], raw=heard_raw)

    assert [e.kind for e in events].count("wake") == 1
    assert any("raw microphone over its voice" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("live", [True, False], ids=["live conversation", "local answer"])
def test_a_hey_steve_the_laptop_played_on_the_raw_microphone_is_ignored(live):
    """GPT-Live is not the desk's voice and may say anything, and a video
    may play over a local answer: a phrase the laptop played is on the
    loopback too, and stays ignored."""
    ear, events, clock = _ear_over_voice(live)

    for cleaned, heard_raw in [(_quiet(1)[0], _marked(WAKE))] + [(q, q) for q in _quiet(5)]:
        clock["t"] += BLOCK / 16_000
        ear.loopback.push(heard_raw, now=clock["t"])
        ear.push(cleaned, now=clock["t"], raw=heard_raw)

    kinds = [e.kind for e in events]
    assert "wake" not in kinds and "laptop_wake" in kinds


def test_a_weak_score_is_not_a_near_miss():
    class Weak(_Detector):
        last_score = 0.2
        threshold = 0.9

    ear, _, events, clock = _ear(Weak())

    _feed(ear, clock, _quiet(int((listen.NEAR_WAKE_S + 1.0) * 20)))

    assert events == [] and ear.counts.near_wakes == 0


def test_a_near_miss_followed_by_a_real_wake_is_not_reported_as_a_miss():
    """The user said it again and it was heard: nothing was missed."""

    class Near(_Detector):
        last_score = 0.8
        threshold = 0.9

        def push(self, audio):
            fired = super().push(audio)
            if fired is not None:
                self.last_score = 0.1  # the phrase is over: the window has gone quiet
            return fired

    ear, _, events, clock = _ear(Near())

    _feed(ear, clock, _quiet(10) + [_wake()] + _talk(10) + _quiet(40))

    assert ear.counts.near_wakes == 0
    assert [e.kind for e in events] == ["wake", "captured"]


async def test_the_switch_is_remembered(tmp_path, monkeypatch):
    from core import db

    monkeypatch.setenv("STEVE_HANDS_FREE", "1")  # a developer's run: not offered in v0.1
    monkeypatch.setattr(desk, "state_file", lambda: tmp_path / "listening.json")
    monkeypatch.setattr(runtime, "voice", None)
    monkeypatch.setattr(runtime, "conn", db.open_store(tmp_path / "desk.db"))
    monkeypatch.setitem(desk._state, "last_switch", None)
    assert desk.switched_on() is False

    said = await desk.switch(True, by="test")

    assert desk.switched_on() is True and said["on"] is True and said["state"] == "warming"
    await desk.switch(False, by="test")
    assert desk.switched_on() is False


async def test_a_switch_with_no_choice_made_turns_it_the_other_way(tmp_path, monkeypatch):
    """9 October 2026: the app's hotkey toggled from its own copy of the
    state, which was stale, and no switch reached the worker. `on=None` is
    decided here, from the file."""
    from core import db

    monkeypatch.setenv("STEVE_HANDS_FREE", "1")  # a developer's run: not offered in v0.1
    monkeypatch.setattr(desk, "state_file", lambda: tmp_path / "listening.json")
    monkeypatch.setattr(runtime, "voice", None)
    monkeypatch.setattr(runtime, "conn", db.open_store(tmp_path / "desk.db"))
    monkeypatch.setitem(desk._state, "last_switch", None)

    assert (await desk.switch(None, by="hotkey"))["on"] is True
    assert (await desk.switch(None, by="hotkey"))["on"] is False
    assert desk.switched_on() is False


async def test_a_worker_giving_up_its_duties_closes_the_microphone(monkeypatch):
    """A swap's new worker opens its own; two ears would answer twice. And it
    says it is a swap, so the old worker's engine and its clock go too."""
    closed = []
    monkeypatch.setattr(desk, "stop", lambda reason="off": closed.append(reason))
    monkeypatch.setitem(duties._duties, "state", "active")
    for name in ("jobs", "signal", "voice"):
        monkeypatch.setattr(runtime, name, None)

    await duties.give_up_duties("retired")

    assert closed == ["swap"]


def test_the_loopback_keeps_seconds_not_the_afternoon():
    """What the laptop played is kept KEEP_S, to be asked about; never more."""
    loopback = listen.Loopback(_Detector, _Vad(), clock=lambda: 0.0)
    for i in range(int(listen.Loopback.KEEP_S * 20) * 3):
        loopback.push(np.zeros(BLOCK, np.float32), now=i * 0.05)
    assert loopback._n <= listen.Loopback.KEEP_S * 16_000 + BLOCK


def test_a_phrase_still_in_the_loopbacks_last_read_is_waited_for():
    """The loopback is read 200 ms at a time, so the laptop's own copy of
    the phrase can arrive just after the mic's wake. Asked too soon, it
    would say the laptop played nothing -- and the laptop's wake would pass
    as theirs."""
    import threading
    import time

    loopback = listen.Loopback(_Detector, _Vad())
    # Not 0.4 s: the window it looks in ends `wait_s` after `near`, and the
    # thread below pushes 0.15 s after it starts, so a thread that a loaded
    # machine started 0.35 s late would push outside the window and the
    # question would be answered "nothing played". `wakes` returns the moment
    # the push arrives, so a long wait costs nothing; what the test pins is
    # that it waits at all (at 0.0 it answers at once, before the thread has
    # pushed).
    loopback.wait_s = 20.0
    near = time.monotonic()

    def late():
        time.sleep(0.15)
        loopback.push(_wake(), now=time.monotonic())

    threading.Thread(target=late).start()
    assert loopback.wakes(near=near), "it answered before the loopback's read came in"


def test_the_ear_follows_the_microphone_the_user_plugs_in(monkeypatch):
    """The maintainer, 5 October 2026: the laptop's own microphone does not work; the user
    plugs one in to talk to it. The ear must move to it without a restart."""
    import sys
    import threading
    import time
    import types

    current = {"name": "Microphone Array (built in)"}
    opened = []

    class Recorder:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            opened.append(self.name)
            return self

        def __exit__(self, *exc):
            return False

        def record(self, numframes):
            time.sleep(0.01)
            return np.zeros((numframes, 1), np.float32)

    class Mic:
        def __init__(self, name):
            self.name = name

        def recorder(self, **kwargs):
            return Recorder(self.name)

    monkeypatch.setitem(sys.modules, "soundcard", types.SimpleNamespace(
        default_microphone=lambda: Mic(current["name"])))
    monkeypatch.setattr(listen, "SETTLE_S", 0.0)
    monkeypatch.setattr(listen.Ears, "FOLLOW_S", 0.05)
    ear, _, _, _ = _ear()
    ears = listen.Ears(ear, signed_in=lambda: True, locked=lambda: False)
    ears._running = ears._want_mic = True
    reader = threading.Thread(target=ears._mic, daemon=True)
    reader.start()
    def until(check) -> None:
        # Up to 30 s, and over the moment it is true: these waited for a
        # thread of another speed than the test's, in a loop of a hundred
        # 20 ms sleeps -- two seconds, which a loaded machine can spend before
        # the thread has run.
        deadline = time.monotonic() + 30
        while not check() and time.monotonic() < deadline:
            time.sleep(0.02)

    try:
        until(lambda: opened)
        assert opened == ["Microphone Array (built in)"]

        current["name"] = "Microphone (USB Audio Device)"
        until(lambda: ears.device == "Microphone (USB Audio Device)")
        assert opened[-1] == "Microphone (USB Audio Device)"
        assert ears.device == "Microphone (USB Audio Device)"
    finally:
        ears._running = False
        reader.join(2)


def test_a_capture_begun_over_the_desks_answer_is_marked_as_a_cut_in():
    """9 October 2026: "hey Steve, stop" said over an 82-second answer was
    handled like any request. What follows a cut-in is told apart from an
    ordinary capture by where the wake came: while the desk was busy."""
    ear, heard, _, clock = _ear()
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(20))
    assert heard[0].barge_in is False, "a wake in quiet is no cut-in"

    _feed(ear, clock, [_wake()] + _talk(5) + _quiet(20))

    assert len(heard) == 2 and heard[1].barge_in is True and heard[1].woke is True


def test_a_wake_while_the_engine_says_the_desk_is_busy_is_a_cut_in():
    """The engine's word, not only the ear's own: a wake over a reply that was
    asked for by a hotkey, or a lookup being worked on, has handed nothing on
    but is as much a cut-in."""
    ear, heard, events, clock = _ear()
    ear.set_mode(listen.EarMode(busy=True))

    _feed(ear, clock, [_wake()] + _talk(5) + _quiet(20))

    assert events[0].kind == "wake" and events[0].barge is True
    assert len(heard) == 1 and heard[0].barge_in is True and heard[0].woke is True


def test_a_follow_up_is_never_a_cut_in():
    ear, heard, _, clock = _ear()
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(20))
    _follow(ear, clock)
    _feed(ear, clock, _quiet(10) + _talk(20) + _quiet(20))
    assert heard[1].woke is False and heard[1].barge_in is False


def test_a_capture_says_how_much_was_spoken_after_the_wake():
    """What `agent/worker/desk` needs to tell "stop" said after the phrase
    from a word or two the pre-roll left, mixed with the desk's own voice:
    on 9 October 2026 the voice detector heard nothing after the wake, and
    Whisper made "I'll talk." of the rest."""
    ear, heard, _, clock = _ear()
    _feed(ear, clock, [_wake()] + _quiet(int((listen.WAKE_WAIT_S + 0.5) * 20)))
    ear.set_mode(listen.QUIET)  # the engine has dealt with the first capture
    _feed(ear, clock, [_wake()] + _talk(10) + _quiet(30))

    assert heard[0].speech_s == 0.0 and heard[0].bare
    assert heard[1].speech_s > 0.2 and not heard[1].bare


def test_after_hey_steve_it_waits_for_them_to_begin_however_finished_the_phrase_sounds():
    """9 October 2026 retest, 17:26:08: "hey Steve", a pause for the tick,
    then the question. The end-of-turn judge -- to which "hey Steve" alone
    sounds finished -- closed the capture 0.48 s after the wake, and the
    question was lost. With nothing said after the wake, the capture is
    not judged and does not end until WAKE_WAIT_S has passed."""
    asked = []

    def judge(audio):
        asked.append(len(audio))
        return 0.99

    ear, heard, _, clock = _ear()
    ear.judge = judge
    pause = int((listen.WAKE_WAIT_S - 0.6) * 20)

    _feed(ear, clock, [_wake()] + _quiet(pause))
    assert heard == [] and ear.gathering == "capture" and asked == []

    _feed(ear, clock, _talk(20) + _quiet(20))

    assert len(heard) == 1 and heard[0].speech_s >= 0.9 and heard[0].ended_by == "judge"
    assert heard[0].ended - heard[0].started > listen.WAKE_WAIT_S, "the question is in it"


def test_a_hey_steve_with_nothing_after_it_ends_once_it_has_waited():
    ear, heard, _, clock = _ear()

    _feed(ear, clock, [_wake()] + _quiet(int((listen.WAKE_WAIT_S + 0.2) * 20)))

    assert len(heard) == 1 and heard[0].bare and heard[0].ended_by == "no request"
    waited = heard[0].ended - (heard[0].started + listen.desk.PRE_ROLL_S)
    assert listen.WAKE_WAIT_S <= waited <= listen.WAKE_WAIT_S + 0.2
