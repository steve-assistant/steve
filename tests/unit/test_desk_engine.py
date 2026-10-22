"""The desk's speakers, its tick, its own words and its switch (voice/floor.py,
voice/engine.py, core/desk.own_words_removed, the /voice/listen route).

The whole desk is run against a room in tests/unit/test_desk_scenarios.py;
these pin the pieces that scenario rests on."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

import tests.gate.voice_extra  # noqa: F401 - skips this module without the voice extra
from core import desk as rules
from voice import audio as audio_io
from voice import engine as engine_mod
from voice.floor import AUDIBLE_RMS, ECHO_TAIL_S, Floor


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class _Player:
    device_rate = 24_000

    def __init__(self):
        self.queued: list[tuple[object, np.ndarray]] = []

    def enqueue(self, audio, rate, owner=None):
        self.queued.append((owner, np.asarray(audio, np.float32)))

    def take(self):
        chunks, self.queued = [a for _, a in self.queued], []
        return chunks

    @property
    def is_playing(self):
        return bool(self.queued)


def _floor():
    clock = _Clock()
    player = _Player()
    return Floor(player, clock=clock), player, clock


def test_only_the_owner_of_the_floor_makes_a_sound():
    """9 October 2026: the local voice and GPT-Live both answered one turn.
    A sound from anyone but the floor's owner is dropped, and counted."""
    floor, player, _ = _floor()
    first, second = object(), object()
    floor.claim(first)

    assert floor.play(first, np.ones(10), 24_000)
    assert not floor.play(second, np.ones(10), 24_000)
    assert [owner for owner, _ in player.queued] == [first] and floor.dropped == 1


def test_taking_the_floor_stops_what_the_last_owner_left():
    floor, player, _ = _floor()
    first, second = object(), object()
    floor.claim(first)
    floor.play(first, np.ones(24_000), 24_000)

    floor.claim(second)

    assert player.queued == [] and floor.owner is second


def test_silent_frames_are_not_a_voice_and_loud_ones_are_for_their_echo_too():
    """GPT-Live streams a frame every 100 ms, silence included; on 9 October
    2026 every frame was taken for its voice. Only what is loud enough to
    hear is, and for its echo's tail after."""
    floor, _, clock = _floor()
    call = object()
    for _ in range(100):
        floor.played(call, 0.0, 0.1)
        floor.played(call, AUDIBLE_RMS / 3, 0.1)
    assert not floor.audible(call)

    floor.played(call, 0.05, 0.1)
    assert floor.audible(call)
    clock.t += 0.1 + ECHO_TAIL_S + 0.05
    assert floor.audible(call), "its echo is still in the room"
    clock.t += 0.1
    assert not floor.audible(call)


def test_what_a_cut_turn_never_said_is_not_counted_as_said():
    """The desk's own words are told from their by what it said (core/desk.py,
    `own_words_removed`): a sentence that was queued but cut before it
    played was never in the room."""
    floor, _, clock = _floor()
    turn = object()
    floor.claim(turn)
    floor.play(turn, np.ones(24_000), 24_000, text="First sentence, being said.")
    floor.play(turn, np.ones(24_000), 24_000, text="Second sentence, never said.")
    clock.t += 0.5

    assert floor.cut(turn)

    said = floor.said_between(clock.t - 10, clock.t + 10)
    assert "First sentence" in said and "Second sentence" not in said
    assert floor.owner is None and not floor.play(turn, np.ones(10), 24_000)


async def test_a_turn_that_lost_the_floor_is_drained_at_once():
    floor, _, _ = _floor()
    turn, other = object(), object()
    floor.claim(turn)
    floor.play(turn, np.ones(24_000), 24_000)
    floor.claim(other)

    assert await asyncio.wait_for(floor.drained(turn, timeout=60.0), 30)


def test_the_player_says_who_each_block_it_played_was_and_how_loud():
    """What the floor measures is what the speakers played, block by block,
    from the player's own callback."""
    player = audio_io.Player()
    player.device_rate = 48_000
    turn = object()
    player._state.chunks.append(np.full(480, 0.5, np.float32))
    player._state.owners.append(turn)
    out = np.zeros((960, 1), np.float32)

    player._callback(out, 960, None, None)

    # Its loudness is the sound it wrote, not the silence filling the block.
    [(at, owner, rms, seconds)] = player.drain_blocks()
    assert owner is turn and rms == pytest.approx(0.5) and seconds == 480 / 48_000
    assert at == player.last_callback > 0 and player.drain_blocks() == []


def test_the_floor_reads_what_was_played_at_the_time_it_was_played():
    clock = _Clock()
    player = audio_io.Player()
    player.device_rate = 48_000
    player.clock = clock
    floor = Floor(player, clock=clock)
    turn = object()
    player._state.chunks.append(np.full(4800, 0.5, np.float32))
    player._state.owners.append(turn)
    player._callback(np.zeros((4800, 1), np.float32), 4800, None, None)
    played_at = clock.t

    clock.t += 0.3  # read later, on the event loop

    assert floor.audible(turn) and floor.onset(turn) == played_at
    assert floor.audible_until(turn) == pytest.approx(played_at + 0.1 + 0.06 + ECHO_TAIL_S)


class _Held:
    """A canceller whose lock someone else is holding."""

    def __init__(self):
        import threading

        self.lock = threading.Lock()
        self.calls = []
        self.told = threading.Event()

    def far(self, block, rate):
        with self.lock:
            self.calls.append(len(block))
        self.told.set()


def test_the_speaker_callback_waits_on_no_lock_but_its_own_queue():
    """9 October 2026 retest, 17:27:14: the speaker stream went over a
    second without a callback mid-reply, and the user heard three seconds of
    glitches. The callback told the floor and fed the echo canceller from
    the audio thread, each behind a lock of its own; one held elsewhere --
    or merely won back from a busy event loop -- and the device has nothing
    to play. With both locks held, the callback still returns."""
    import threading

    player = audio_io.Player()
    player.device_rate = 48_000
    floor = Floor(player)
    canceller = _Held()
    player.on_play = canceller.far
    player._state.chunks.append(np.full(960, 0.5, np.float32))
    player._state.owners.append(object())
    done = threading.Event()

    def callback():
        player._callback(np.zeros((960, 1), np.float32), 960, None, None)
        done.set()

    with floor._lock, canceller.lock:
        thread = threading.Thread(target=callback, daemon=True)
        thread.start()
        assert done.wait(10), "the callback waited on the floor or the canceller"
        assert canceller.calls == []
    thread.join(10)
    assert canceller.told.wait(10) and canceller.calls == [960], "the canceller was never told"


def test_the_callback_keeps_count_of_its_gaps_and_how_long_it_took():
    """What the next sound test needs to tell a late callback from a slow
    one: the longest gap between callbacks, the longest one took, and how
    many came later than their block -- measured by the callback itself."""
    times = iter([0.0, 0.004, 0.021, 0.023, 1.4, 1.401])
    player = audio_io.Player()
    player.device_rate = 48_000
    player.clock = lambda: next(times)
    for _ in range(3):
        player._callback(np.zeros((1024, 1), np.float32), 1024, None, None)

    stats = player.take_stats()

    assert stats["n"] == 3 and stats["late"] == 1
    assert stats["max_gap_s"] == pytest.approx(1.379) and stats["max_run_s"] == pytest.approx(0.004)
    assert player.take_stats()["n"] == 0, "read once, then counted afresh"


class _Watched(audio_io.Player):
    """The player as the floor's watchdog sees it: a stream that says it is
    running, or not, and a count of its reopenings."""

    def __init__(self, clock, running):
        super().__init__()
        self.device_rate = 24_000
        self.clock = clock
        self.stream_running = running
        self.reopenings = 0

    @property
    def running(self):
        return self.stream_running

    def reopen(self):
        self.reopenings += 1
        return self.queued_seconds


def _watched(gap, *, running=True):
    clock = _Clock()
    player = _Watched(clock, running)
    player._state.chunks.append(np.full(24_000, 0.5, np.float32))
    player.last_callback = clock.t
    floor = Floor(player, clock=clock)
    clock.t += gap
    return floor, player


def test_a_speaker_stream_that_is_late_keeps_what_it_has_queued(caplog):
    """The watchdog that dropped most of the Contoso answer: a second and a
    quarter without a callback, the stream still running. Late is logged,
    and the queue left to play when it catches up."""
    import logging

    floor, player = _watched(1.25)

    with caplog.at_level(logging.INFO, logger="desk"):
        assert floor.check() is False
        player.last_callback = player.clock()
        floor.check()

    assert player.queued_seconds == pytest.approx(1.0) and player.reopenings == 0
    assert floor.late == 1
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "late" in said and "caught up" in said


@pytest.mark.parametrize("gap, running", [(6.0, True), (0.5, False)])
def test_a_speaker_stream_that_has_stopped_is_reopened_off_the_event_loop(gap, running):
    floor, player = _watched(gap, running=running)

    assert floor.check() is True
    floor._reopener.join(10)

    assert player.reopenings == 1 and floor.reopened == 1
    assert floor._reopener is not None and floor._reopener.name == "floor-reopen"


def test_the_tick_on_a_wake_is_not_speech_to_the_voice_detector():
    """The user chose a soft tick on every heard wake (9 October 2026). It plays
    on the speakers, which the loopback hears: as speech it would count as
    the laptop talking over their request, and their yes would go to the
    screen. Silero, the real one, hears no speech in it."""
    from voice import listen

    try:
        vad = listen.Vad()
    except Exception as exc:  # noqa: BLE001 - the model ships with faster-whisper
        pytest.skip(f"no voice detector here: {exc}")
    sound, rate = engine_mod.TICK
    tick = np.interp(np.arange(0, len(sound), rate / 16_000), np.arange(len(sound)), sound)
    padded = np.concatenate([np.zeros(4_000), tick, np.zeros(4_000)]).astype(np.float32)

    assert not any(vad.push(padded))
    assert float(np.sqrt(np.mean(np.square(sound)))) >= AUDIBLE_RMS, "a tick she can hear"


def test_the_tick_on_a_wake_does_not_wake_the_detector():
    """Nor does the wake detector, on the loopback or the microphone, take the
    tick for "hey Steve"."""
    from voice import wake

    if not (wake.model_dir() / wake.HEAD_FILE).exists():
        pytest.skip(f"no wake model at {wake.model_dir()}")
    detector = wake.Detector(wake.Frontend(), wake.Head.load(wake.model_dir() / wake.HEAD_FILE))
    sound, rate = engine_mod.TICK
    tick = np.interp(np.arange(0, len(sound), rate / 16_000), np.arange(len(sound)), sound)
    stream = np.concatenate([np.zeros(32_000), np.tile(np.concatenate(
        [tick, np.zeros(16_000)]), 5)]).astype(np.float32)

    fired = [detector.push(stream[i:i + 1280]) for i in range(0, len(stream), 1280)]
    assert not any(f is not None for f in fired)


@pytest.mark.parametrize("heard, said, left", [
    ("Today you have two meetings", "Today you have two meetings and a deadline.", ""),
    ("today, you have two -- and what's the weather", "Today, you have two meetings.",
     "and what's the weather"),
    ("what's the weather tomorrow", "Today you have two meetings.", "what's the weather tomorrow"),
    ("do you have time", "you have two meetings", "do you have time"),
    ("stop", "", "stop"),
])
def test_only_the_desks_own_words_are_taken_out_of_what_the_user_said(heard, said, left):
    """Runs of three words or more that the desk was saying; never a word or
    two the user happens to share with it ("you have")."""
    assert rules.own_words_removed(heard, said) == left


def test_the_switch_toggles_by_what_the_worker_has_not_what_the_app_thinks(monkeypatch):
    """9 October 2026: the app toggled from its own copy of the state, which
    can be stale; a press of Alt+Home never reached the worker. A toggle is
    decided by the worker, and says which control asked."""
    monkeypatch.setenv("STEVE_HANDS_FREE", "1")  # a developer's run: not offered in v0.1
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from agent.worker import desk, links, routes

    calls = []

    async def switch(on, *, by):
        calls.append((on, by))
        return {"on": on, "state": "listening" if on else "off"}

    async def warm():
        return None

    monkeypatch.setattr(desk, "switch", switch)
    monkeypatch.setattr(links, "ensure_voice", warm)
    app = Starlette(routes=[Route("/voice/listen", routes.voice_listen, methods=["POST"])])
    client = TestClient(app)

    monkeypatch.setattr(desk, "switched_on", lambda: True)
    assert client.post("/voice/listen", json={"toggle": True, "by": "hotkey"}).json()["on"] is False
    monkeypatch.setattr(desk, "switched_on", lambda: False)
    assert client.post("/voice/listen", json={"toggle": True, "by": "tray"}).json()["on"] is True
    client.post("/voice/listen", json={"on": True})
    assert calls == [(False, "hotkey"), (True, "tray"), (True, "app")]
    assert client.post("/voice/listen", json={}).status_code == 400


class _Shell:
    """The worker's side of the engine, as small as a test needs."""

    engine = None

    def __init__(self):
        self.started: list[str] = []
        self.lines: list[str] = []

    def interrupt(self):
        pass

    def turn_context(self, turn):
        import contextlib

        from agent import runtime

        @contextlib.contextmanager
        def held():
            token = runtime.desk_turn.set(turn)
            try:
                yield
            finally:
                runtime.desk_turn.reset(token)

        return held()

    def start_room(self, title):
        self.started.append(title)

    def log_turn(self, turn):
        self.lines.append(turn.outcome)


class _EarFace:
    """The ear as the engine sees it."""

    def set_mode(self, mode):
        self.mode = mode


async def test_notes_a_hotkey_turn_asked_for_start_only_with_the_microphone_open():
    """A push-to-talk turn whose assistant asked for notes of the room: they
    start once it is over -- and only if hands-free is on, the microphone
    open. `take_notes` refuses them otherwise; this is the second lock
    (found in the port of the desk's tests, 9 October 2026)."""
    from agent import runtime

    shell = _Shell()
    engine = engine_mod.Engine(shell, Floor(_Player()))

    async def run(out):
        runtime.desk_turn.get().asks["notes"] = "Meeting"
        return "spoken"

    assert await engine.hotkey_turn(run) == "spoken"
    assert shell.started == [], "notes of the room started with the microphone off"

    engine.attach(_EarFace())
    engine.opened()
    await engine.hotkey_turn(run)
    assert shell.started == ["Meeting"]
    assert runtime.desk_turn.get() is None, "the turn outlived itself"


async def test_a_capture_whose_step_fails_says_so_in_its_line_and_the_desk_listens_on():
    from voice import listen

    class Failing(_Shell):
        async def transcribe(self, heard):
            raise RuntimeError("Whisper went away")

    shell = Failing()
    engine = engine_mod.Engine(shell, Floor(_Player()))
    engine.opened()
    heard = listen.Heard(audio=np.zeros(1600, np.float32), woke=True, laptop_speech_s=0.0,
                         started=0.0, ended=1.0)

    engine.ear_event(listen.EarEvent("captured", 1.0, heard=heard))
    await engine.settle()

    assert shell.lines == ["failed"] and engine.mode == "idle"


async def test_under_decision_a_a_cut_in_turn_opens_no_live_conversation(monkeypatch):
    """The tool refuses a cut-in turn; the engine is the second lock, as the
    desk's handler was before the rebuild: what the desk heard of a cut-in
    has its own voice in it ("I'll talk.", 9 October 2026)."""
    from types import SimpleNamespace

    from voice import listen

    class Asks(_Shell):
        async def transcribe(self, heard):
            return SimpleNamespace(transcribe_ms=1)

        async def decide(self, turn, transcript, heard):
            return engine_mod.Decision("agent", then="agent", request="plan my week")

        async def answer(self, turn):
            turn.asks["live"] = True
            return SimpleNamespace(reply_text="Sure.")

    monkeypatch.setattr(engine_mod, "LIVE_OPENED_BY", "user and assistant")
    engine = engine_mod.Engine(Asks(), Floor(_Player()))
    opened = []

    async def live(turn, **kwargs):
        opened.append(turn.cut_in)

    monkeypatch.setattr(engine, "_live", live)
    engine.opened()
    for cut_in in (True, False):
        heard = listen.Heard(audio=np.zeros(1600, np.float32), woke=True, laptop_speech_s=0.0,
                             started=0.0, ended=1.0, barge_in=cut_in)
        engine.ear_event(listen.EarEvent("captured", 1.0, heard=heard, barge=cut_in))
        await engine.settle()

    assert opened == [False]


async def test_a_duplex_question_that_raises_mutes_it_and_says_so(caplog):
    """Its microphone stays open over its voice only while the echo is
    being kept out, asked every block. A question that raised ended the
    sender in silence and left the conversation deaf to them; now it is
    muted while it talks, and the log says why, once."""
    import base64
    import json
    import logging

    from voice.desk_call import LiveCall, Words

    class Socket:
        def __init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(json.loads(text))

    def broken():
        raise RuntimeError("the canceller went away")

    async def ask(text, *, heard_at):
        return ""

    floor = Floor(_Player())
    call = LiveCall(floor=floor, words=Words(), ask=ask, duplex=broken)
    floor.claim(call)
    floor.played(call, 0.2, 5.0)  # its voice is audible
    call.live, call._loop, call._mic = Socket(), asyncio.get_running_loop(), asyncio.Queue()
    with caplog.at_level(logging.WARNING, logger="desk"):
        sender = asyncio.ensure_future(call._send_mic())
        for _ in range(3):
            call._mic.put_nowait(np.full(1600, 0.3, np.float32))
        call._mic.put_nowait(None)
        await asyncio.wait_for(sender, 30)

    sent = call.live.sent
    audio = [e for e in sent if e["type"] == "session.input_audio.append"]
    assert len(audio) == 3, "the sender stopped"
    assert not any(np.frombuffer(base64.b64decode(e["audio"]), "<i2").any() for e in audio)
    assert any(e["type"] == "session.input_audio.mute" for e in sent)
    assert sum("muting it while it talks" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("request_, topic", [
    ("okay, so, let's talk live, actually about my week", "about my week"),
    ("um, let's go live about the trip", "about the trip"),
    ("let's talk live", ""),
    ("let's talk about my week", None),
])
def test_what_the_user_wants_to_talk_live_about_is_user_words_without_the_fillers(request_, topic):
    from agent.worker import desk

    assert desk.live_asked(request_) == topic


async def test_a_mode_that_outlives_its_longest_stay_is_left_and_logged(caplog):
    """"Not responding for several minutes" (9 October 2026) becomes a line
    in the log: a mode that outlasts its longest stay is left, and the desk
    goes back to waiting for the phrase."""
    import logging

    clock = _Clock()
    floor = Floor(_Player(), clock=clock)

    class Shell:
        engine = None

        def interrupt(self):
            pass

    engine = engine_mod.Engine(Shell(), floor, clock=clock)
    engine.opened()
    engine._to("transcribe", "wake")
    clock.t += engine_mod.DWELL_S["transcribe"] + 1

    with caplog.at_level(logging.WARNING, logger="desk"):
        engine.tick()

    assert engine.mode == "idle" and engine.counts["stuck"] == 1
    assert any("outlived" in r.getMessage() for r in caplog.records)


async def test_a_live_conversation_that_ends_as_not_its_turns_never_leaves_the_desk_in_it():
    """17:29:35-17:29:57, 9 October 2026: a live conversation ended as if by
    a wake that never came for it, and the desk stayed in "live" with no
    conversation for 23 s, deaf to everything but "hey Steve". However a
    conversation ends, a desk still in it with the same turn goes back to
    waiting for the phrase."""
    clock = _Clock()
    floor = Floor(_Player(), clock=clock)
    shell = _Shell()
    thread = []
    shell.live_ended = lambda call, reason, *, unanswered: None
    shell.to_thread = thread.append
    engine = engine_mod.Engine(shell, floor, clock=clock)
    engine.opened()

    class Words:
        async def settle(self, timeout, sleep):
            return None

        def unanswered(self):
            return ""

    class Call:
        end_reason = "wake"
        work = ()
        late = ()
        ended_at = None
        words = Words()

    turn = engine._new_turn("follow")
    engine.turn = turn
    engine.call = call = Call()
    engine._to("live", "user")

    await engine._after_live(turn, call, "voice closed")

    assert engine.mode == "idle" and engine.call is None and engine.turn is None
    assert thread == [""]
