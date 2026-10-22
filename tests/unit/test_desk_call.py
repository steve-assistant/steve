"""A live conversation at the desk: the client that carries it
(voice/desk_call.py), when the desk opens one, and what it keeps of one.
What it may never do -- hear itself, hand the laptop its own words -- is
tests/gate/test_desk.py's; how a whole conversation goes, against a room,
is tests/unit/test_desk_scenarios.py's.

Rewritten on 9 October 2026 for the rebuilt desk (docs/desk-voice-design.md).
The conversation's clocks -- when it has gone quiet, when it has been silent
too long -- moved out of the call into the engine (voice/engine.py
`_live_tick`), so what was tested here of `DeskCall._clock` is tested at the
end of this file, against the engine; `DeskAudio`'s queue heuristics are gone
with the class (the floor measures what was audible, voice/floor.py). What
each test here pins is a guarantee the version before it pinned, from the
incident it cites."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time

import numpy as np
import pytest

import tests.gate.voice_extra  # noqa: F401 - skips this module without the voice extra
from agent import runtime
from agent.worker import desk as handler
from core import db
from core import desk as rules
from voice import desk_call, listen
from voice import engine as engine_mod
from voice.engine import Engine, Turn
from voice.floor import ECHO_TAIL_S, Floor
from voice.stt import Transcript


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Live:
    """GPT-Live's socket: records what it is sent, plays back what it is given."""

    def __init__(self, first=None):
        self.events: asyncio.Queue = asyncio.Queue()
        self.events.put_nowait(json.dumps(first or {"type": "session.started"}))
        self.sent: list[dict] = []

    def push(self, **event):
        self.events.put_nowait(json.dumps(event))

    async def send(self, text):
        event = json.loads(text)
        self.sent.append(event)
        if event["type"] == "session.close":
            self.push(type="session.closed", reason="close_requested", usage={"seconds": 7})

    async def recv(self):
        return await self.events.get()

    async def close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()


class _Player:
    """The speakers, as the floor sees them. Nothing queued plays by itself:
    `play_all` tells the floor each block was played, as the real player's
    callback does, and `sounds` is every sound ever given it, by whom."""

    device_rate = 48_000
    is_playing = False
    queued_seconds = 0.0

    def __init__(self):
        self.queued: list[tuple[object, np.ndarray]] = []
        self.sounds: list[object] = []

    def enqueue(self, audio, rate, owner=None):
        self.queued.append((owner, np.asarray(audio, np.float32)))
        self.sounds.append(owner)

    def take(self):
        chunks, self.queued = [a for _, a in self.queued], []
        return chunks

    def play_all(self, floor):
        for owner, audio in self.queued:
            for i in range(0, len(audio), 4800):
                block = audio[i:i + 4800]
                floor.played(owner, float(np.sqrt(np.mean(np.square(block)))),
                             len(block) / self.device_rate)
        self.queued = []


async def _inline(fn, *args):
    return fn(*args)


async def _until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return predicate()


def _frame(level, samples=2400):
    pcm = (np.full(samples, level) * 32767).astype("<i2")
    return base64.b64encode(pcm.tobytes()).decode()


async def _opened(*, duplex=True, ask=None, history=(), opener=None):
    """A live client that has connected over a fake socket, on a floor and a
    clock of its own: the call, its socket, the floor, the speakers, the clock."""
    clock = _Clock()
    player = _Player()
    floor = Floor(player, clock=clock)
    live = _Live()

    async def connect():
        return live

    async def nobody(text, *, heard_at):
        return "Done."

    call = desk_call.LiveCall(
        floor=floor, words=desk_call.Words(clock=clock), ask=ask or nobody, clock=clock,
        duplex=duplex if callable(duplex) else (lambda: duplex), connect=connect)
    floor.claim(call)
    await call.start(list(history), opener=opener)
    return call, live, floor, player, clock


def _mic_sent(live):
    return [np.frombuffer(base64.b64decode(e["audio"]), dtype="<i2")
            for e in live.sent if e["type"] == "session.input_audio.append"]


async def _finish(call, running):
    await call.close("done")
    await asyncio.wait_for(running, 30)


# ---------------------------------------------------------------------------
# The session, and the voice in and out
# ---------------------------------------------------------------------------


def test_the_desk_speaks_wideband_and_is_given_the_conversation_as_history_not_a_line():
    """9 October 2026: the local voice said its reply, and GPT-Live, told to
    "open by saying" the same words, said it again. What was said goes in as
    the session's own history -- their words as theirs, the assistant's as its --
    and nothing is left for it to repeat."""
    start = desk_call.session_start(history=[("user", "what's on today"),
                                             ("assistant", "Two meetings.")])
    s = start["session"]
    assert s["audio"]["format"] == {"type": "audio/pcm", "rate": 24_000}
    assert s["store"] is False and s["delegation"] == {"type": "client"}
    assert "at their laptop" in s["instructions"]
    assert s["input"] == [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "what's on today"}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Two meetings."}]}]
    assert "Open by saying" not in json.dumps(start) and "Two meetings." not in s["instructions"]


def test_only_the_last_of_a_long_history_is_given_and_nothing_empty():
    history = [("user", f"question {n}") for n in range(30)] + [("assistant", "   ")]

    items = desk_call.session_start(history=history)["session"]["input"]

    assert len(items) <= desk_call.HISTORY_MESSAGES
    assert items[-1]["content"][0]["text"] == "question 29"


async def test_their_voice_goes_in_at_24k_and_its_frames_reach_the_floors_player():
    call, live, floor, player, _ = await _opened(history=[("user", "what's on today")])
    assert live.sent[0]["type"] == "session.start"
    assert live.sent[0]["session"]["input"][0]["role"] == "user"
    running = asyncio.ensure_future(call.run())

    for _ in range(10):
        call.feed((0.1 * np.ones(1600)).astype(np.float32))
    assert await _until(lambda: len(_mic_sent(live)) >= 10)
    samples = sum(len(block) for block in _mic_sent(live))
    assert 10 * 1600 * 1.5 * 0.8 <= samples <= 10 * 1600 * 1.5, "not resampled from 16 to 24 kHz"

    for _ in range(5):
        live.push(type="session.output_audio.delta", delta=_frame(0.1))
    assert await _until(lambda: len(player.queued) >= 5)
    assert all(owner is call for owner, _ in player.queued), "its sound bypassed the floor"
    played = sum(len(audio) for _, audio in player.queued)
    assert 5 * 4800 * 0.85 <= played <= 5 * 4800, "not resampled to the speakers' rate"
    await _finish(call, running)


async def test_a_voice_that_cannot_start_is_said_before_a_word():
    clock, live = _Clock(), _Live(first={"type": "error", "error": {"message": "no"}})

    async def connect():
        return live

    call = desk_call.LiveCall(floor=Floor(_Player()), words=desk_call.Words(), clock=clock,
                              ask=None, connect=connect)

    with pytest.raises(desk_call.live.LiveUnavailable):
        await call.start([("user", "hello")], opener=desk_call.ANSWER_NOW)

    assert call.live is None and [e["type"] for e in live.sent] == ["session.start"]


async def test_a_connection_that_cannot_be_made_is_the_same_unavailable():
    async def connect():
        raise OSError("no route")

    call = desk_call.LiveCall(floor=Floor(_Player()), words=desk_call.Words(), ask=None,
                              connect=connect)

    with pytest.raises(desk_call.live.LiveUnavailable, match="connect"):
        await call.start([])


def test_full_duplex_only_on_headphones(monkeypatch):
    from voice import audio as audio_io

    def device(name):
        return audio_io.Device(index=1, name=name, hostapi="WASAPI", sample_rate=48000,
                               channels=2, is_default=True)

    monkeypatch.setattr(audio_io, "default_device", lambda kind: device("Speakers (Realtek)"))
    assert desk_call.duplex_default() is False
    monkeypatch.setattr(audio_io, "default_device", lambda kind: device("Headphones (AirPods)"))
    assert desk_call.duplex_default() is True


async def test_losing_the_echo_canceller_mid_conversation_mutes_the_microphone():
    """8 October 2026 audit, finding 2: duplex was decided once, when the
    conversation opened. The canceller turns itself off when it fails
    (`voice/echo.py`), and the microphone then went on to GPT-Live raw, its
    own echo included, for up to the 15 minutes of the call. Duplex is asked
    again for every block, so a lost canceller is a muted microphone while it
    talks -- and open again when it stops."""
    working = {"on": True}
    call, live, floor, player, clock = await _opened(duplex=lambda: working["on"])
    running = asyncio.ensure_future(call.run())
    live.push(type="session.output_audio.delta", delta=_frame(0.2))
    assert await _until(lambda: player.queued)
    player.play_all(floor)
    assert floor.audible(call)
    loud = (0.3 * np.random.default_rng(0).standard_normal(1600)).astype(np.float32)

    for _ in range(4):
        call.feed(loud)
    assert await _until(lambda: len(_mic_sent(live)) >= 4)
    assert all(block.any() for block in _mic_sent(live)), "muted while the canceller worked"

    working["on"] = False  # the canceller fails, mid-conversation
    for _ in range(4):
        call.feed(loud)
    assert await _until(lambda: len(_mic_sent(live)) >= 8)
    # Not the first block after the flip: the resampler holds about 60 ms of
    # what was sent before it, and gives it back there.
    assert not any(block.any() for block in _mic_sent(live)[5:]), "raw echo went to GPT-Live"
    assert any(e["type"] == "session.input_audio.mute" for e in live.sent)

    clock.t += 10.0  # it has stopped talking and its echo has died: the user can be heard again
    call.feed(loud)
    assert await _until(lambda: len(_mic_sent(live)) >= 9)
    assert _mic_sent(live)[8].any()
    assert any(e["type"] == "session.input_audio.unmute" for e in live.sent)
    await _finish(call, running)


def test_its_voice_cut_off_still_ended_just_now_so_its_echo_is_not_their_yes():
    """The floor once set the end of its own voice, when it was cut off, to
    zero, as if it had never spoken. On the speakers, its own echo can be what
    cuts it off -- and then that echo, a second old, passed
    `core/desk.heard_user` as their yes for the next eight seconds."""
    clock, player = _Clock(), _Player()
    floor = Floor(player, clock=clock)
    call = object()
    floor.claim(call)
    floor.play(call, np.full(24_000, 0.2, np.float32), 24_000)
    floor.played(call, 0.2, 3.0)  # three seconds of it audible, still to go
    echo_at = clock.t

    floor.cut(call)
    until = floor.audible_until(call)

    assert clock.t <= until <= clock.t + ECHO_TAIL_S + 1e-6
    assert not rules.heard_user(mic_speech_at=echo_at, own_voice_until=until, now=clock.t)
    assert rules.heard_user(mic_speech_at=clock.t + 1.0, own_voice_until=until,
                           now=clock.t + 1.0), "her yes a second after it stopped counts"


# ---------------------------------------------------------------------------
# Their words, as the desk's own Whisper heard them
# ---------------------------------------------------------------------------


async def test_what_the_user_said_before_it_listened_is_its_first_handoff():
    """"Let's talk live about my week" was heard by the ear, not by
    GPT-Live; it must still be what their assistant is asked -- and nothing of
    GPT-Live's own transcript of the room is."""
    asked = []

    async def ask(text, *, heard_at):
        asked.append((text, heard_at))
        return "Three meetings."

    call, live, _, _, _ = await _opened(ask=ask)
    call.words.add("about my week", 1.0, 2.0)  # as the engine does, when it opens one
    running = asyncio.ensure_future(call.run())

    live.push(type="session.input_transcript.delta", delta="Casey stop, yes maple")
    live.push(type="session.delegation.created", delegation={"id": "d1"})
    assert await _until(lambda: any(e["type"] == "session.commentary.append" for e in live.sent))

    assert asked == [("about my week", 2.0)]
    answer = [e for e in live.sent if e["type"] == "session.commentary.append"][0]
    assert answer["delegation_id"] == "d1" and answer["content"] == "Three meetings."
    assert call.words.untaken() == ""
    await _finish(call, running)


def test_a_goodbye_or_a_hmm_is_not_left_unanswered_and_real_words_are():
    words = desk_call.Words()
    words.add("thanks, bye", 1.0, 2.0)
    words.add("hmm", 3.0, 4.0)
    words.add("uh, okay", 5.0, 6.0)
    assert words.unanswered() == "", "a goodbye was handed to her assistant as a request"

    words.add("and what's on tomorrow", 7.0, 8.0)
    assert "what's on tomorrow" in words.unanswered()


def test_what_the_user_said_before_it_spoke_has_had_its_answer():
    words = desk_call.Words()
    words.add("what's the weather", 1.0, 2.0)
    words.add("and the bank", 9.0, 10.0)

    words.answered_before(5.0)  # it spoke at 5.0

    assert words.unanswered() == "and the bank"


def test_what_is_handed_over_is_taken_once_and_is_then_answered():
    words = desk_call.Words()
    words.add("what's on", 1.0, 2.0)
    words.add("tomorrow", 3.0, 4.5)

    text, heard_at = words.take()

    assert text == "what's on tomorrow" and heard_at == 4.5
    assert words.take() == ("", 0.0) and words.unanswered() == ""


async def test_a_stretch_still_being_transcribed_is_waited_for_up_to_a_limit():
    clock = _Clock()
    words = desk_call.Words(clock=clock)
    words.expect()
    assert words.pending == 1

    async def sleep(seconds):
        clock.t += seconds

    await words.settle(1.5, sleep)

    assert 1.5 <= clock.t - 1000.0 < 1.7, "it did not wait, or waited past the limit"
    words.add("late", 1.0, 2.0)
    assert words.pending == 0


@pytest.mark.parametrize(("said", "counts"), [
    (" mm", False), (" Hmm.", False), (" 3", False), ("", False),
    (" what about Friday", True), ("今日の予定は", True), (" ¿qué hora es?", True),
])
def test_words_in_any_language_are_words_and_a_hmm_is_not(said, counts):
    """What a quiet call is told from a talking one by: a transcript in
    another script is still them talking; a "hmm" or a bare number is what a
    room makes of noise."""
    assert bool(desk_call.real_words(said)) is counts


# ---------------------------------------------------------------------------
# When the desk opens one: by name, and only by name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("said", "topic"), [
    ("let's talk live", ""),
    ("Let's talk live about my week", "about my week"),
    ("okay, let's talk live", ""),
    ("talk live", ""),
    ("switch to live", ""),
    ("switching over to live", ""),
    ("go live", ""),
    ("can we go live about the trip", "about the trip"),
    ("live conversation", ""),
    ("live mode", ""),
    ("live chat", ""),
])
def test_a_live_conversation_is_asked_for_by_name(said, topic):
    assert handler.live_asked(said) == topic


@pytest.mark.parametrize("said", [
    "let's talk", "let's talk through my week", "talk me through my week", "I'll talk",
    "can we talk about it", "I want to live in Boston", "what's alive", "olive oil", "",
])
def test_nothing_short_of_the_name_asks_for_a_live_conversation(said):
    """9 October 2026: "hey Steve, stop" came back from Whisper as "I'll
    talk." and opened a billed conversation. The back-and-forth the user wants is
    the follow-up window, in the local voice, for nothing."""
    assert handler.live_asked(said) is None


# ---------------------------------------------------------------------------
# The desk, with the worker's side of it, and fakes behind them
# ---------------------------------------------------------------------------


class _Session:
    """The warm speech session, as the desk's worker side sees it."""

    def __init__(self, text="", reply="ok"):
        self.text, self.reply = text, reply
        self.asked: list[str] = []
        self.said: list[str] = []
        self.transcriber = self

    def transcribe(self, audio):
        return Transcript(text=self.text, language="en", duration_s=1.0, transcribe_ms=1,
                          device="cpu")

    async def respond(self, transcript, *, released, text=None, timing=None, out=None):
        from voice.pipeline import TurnTiming, VoiceTurn

        self.asked.append(text)
        return VoiceTurn(transcript=transcript, reply_text=self.reply, timing=TurnTiming())

    async def say(self, text, *, out=None):
        self.said.append(text)

    def interrupt(self):
        pass


def _talk_it_through():
    from agent.toolset import desk as tools

    found: dict = {}
    tools.register(lambda fn: found.setdefault(fn.__name__, fn))
    return found["talk_it_through"]


def _ctx(channel="voice"):
    from types import SimpleNamespace

    return SimpleNamespace(deps=SimpleNamespace(tools_used=[], channel=channel, asked=""))


class _Asks(_Session):
    """A session whose model, in the turn, calls `talk_it_through`."""

    def __init__(self, text, channel="voice", reply="Okay."):
        super().__init__(text, reply)
        self.channel, self.answer = channel, None

    async def respond(self, transcript, *, released, text=None, timing=None, out=None):
        self.answer = await _talk_it_through()(_ctx(self.channel))
        return await super().respond(transcript, released=released, text=text, timing=timing,
                                     out=out)


@pytest.fixture
def store(tmp_path, monkeypatch):
    conn = db.open_store(tmp_path / "desk.db")
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(runtime, "voice", None)
    monkeypatch.setattr(handler, "state_file", lambda: tmp_path / "listening.json")
    for key, value in (("engine", None), ("ticker", None), ("listener", None), ("ears", None),
                       ("last", None), ("last_call", None), ("false_wakes", 0),
                       ("dropped_follow_ups", 0), ("echo", None)):
        monkeypatch.setitem(handler._state, key, value)
    monkeypatch.setattr(desk_call, "duplex_default", lambda: True)

    async def no_network(*args, **kwargs):
        raise AssertionError("a test reached GPT-Live")

    monkeypatch.setattr(desk_call.live, "connect", no_network)
    yield conn
    conn.close()


def _desk(monkeypatch, session, *, connect=None):
    """The desk's engine and worker side, listening, over `session`; a live
    conversation, if it opens one, is on the fake socket `connect` gives."""
    player = _Player()
    engine = Engine(handler.DeskShell(session, blocking=_inline, connect=connect),
                    Floor(player))
    monkeypatch.setitem(handler._state, "engine", engine)
    engine.opened()
    return engine, player


def _heard(*, woke=True, laptop=0.0, kind=None, barge_in=False, speech_s=1.0, ended_by="",
           asks=(), seconds=1.0):
    """A capture that ended just now, on the desk's clock."""
    now = time.monotonic()
    return listen.Heard(audio=np.zeros(16_000, np.float32), woke=woke, laptop_speech_s=laptop,
                        started=now - seconds, ended=now, barge_in=barge_in, speech_s=speech_s,
                        ended_by=ended_by, asks=asks, kind=kind or ("wake" if woke else "follow"))


def _cut_in(*, speech_s=1.0, laptop=2.0):
    """The capture that follows a wake said over the desk's own voice."""
    return _heard(barge_in=True, speech_s=speech_s, laptop=laptop, seconds=2.1)


async def _capture(engine, heard):
    engine.ear_event(listen.EarEvent("captured", 0.0, heard=heard, barge=heard.barge_in))
    await engine.settle()


def _forbid_live(engine, monkeypatch):
    def boom():
        raise AssertionError("a live conversation was opened")

    monkeypatch.setattr(engine.shell, "live_call", boom)


def _turn_lines(caplog):
    return [r.getMessage() for r in caplog.records if "hands-free turn" in r.getMessage()]


async def _hear_him_say_hey_steve(engine):
    """"Hey Steve" in the middle of whatever is going on: it ends a live
    conversation and starts a capture."""
    engine.ear_event(listen.EarEvent("wake", 0.0, barge=True))
    await engine.settle()


async def test_asking_for_a_live_conversation_opens_one_with_user_words_as_history(store,
                                                                                  monkeypatch):
    live = _Live()

    async def connect():
        return live

    session = _Session("Hey Steve, let's talk live about my week")
    engine, _ = _desk(monkeypatch, session, connect=connect)

    await _capture_until_open(engine, _heard())

    start = live.sent[0]
    assert start["type"] == "session.start" and session.asked == [], "it went to a turn as well"
    assert start["session"]["input"][-1] == {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "let's talk live about my week"}]}
    assert "Open by saying" not in json.dumps(start)
    assert any(e.get("content") == desk_call.ANSWER_NOW for e in live.sent), \
        "it was not told to answer what she had already said"
    assert engine.call.words.untaken() == "about my week"
    await _hear_him_say_hey_steve(engine)
    assert engine.call is None and handler._state["last_call"]["reason"] == "wake"


async def _capture_until_open(engine, heard):
    """Start a capture and wait until its live conversation is open."""
    engine.ear_event(listen.EarEvent("captured", 0.0, heard=heard, barge=heard.barge_in))
    assert await _until(lambda: engine.mode == "live" and engine.call is not None
                        and engine.call.live is not None
                        and engine.status()["history"][-1]["why"] == "user")


@pytest.mark.parametrize("reply", [
    "Which of the two drafts should I send?",
    "Done: a reminder at 6 p.m. Anything else?",
    "It's 64 degrees and sunny.",
])
async def test_a_reply_that_asks_them_something_is_answered_in_the_follow_up_window(
        store, monkeypatch, reply):
    """The maintainer, 8 October 2026: a question from the local assistant no longer
    opens a GPT-Live conversation ($0.05 a minute); the free ten-second
    window already covers answering it."""
    engine, _ = _desk(monkeypatch, _Session("Hey Steve, send the email to Dana", reply))
    _forbid_live(engine, monkeypatch)

    await _capture(engine, _heard())

    assert engine.mode == "follow" and engine.call is None


async def test_one_off_questions_in_a_row_stay_local_and_cost_nothing(store, monkeypatch):
    """7 October 2026: the weather, "okay, perfect" and a walking time
    opened a live conversation at $0.05 a minute that ran 7.6 minutes."""
    session = _Session()
    engine, _ = _desk(monkeypatch, session)
    _forbid_live(engine, monkeypatch)

    for said, woke in (("Hey Steve, what's the weather", True), ("okay, perfect", False),
                       ("how far is P&M", False), ("and the bank", False)):
        session.text = said
        await _capture(engine, _heard(woke=woke))
        assert engine.mode == "follow" and engine.call is None, said

    assert len(session.asked) == 4


async def test_a_follow_up_the_laptop_talked_over_is_dropped_and_logged(store, monkeypatch,
                                                                       caplog):
    engine, _ = _desk(monkeypatch, _Session("and tomorrow", "One."))
    with caplog.at_level("INFO", logger="desk"):
        await _capture(engine, _heard(woke=False, laptop=1.2))
    assert "a follow-up dropped: the laptop played 1.2 s" in caplog.text
    assert "and tomorrow" not in caplog.text, "never what she said"


async def test_when_the_days_minutes_are_spent_it_says_so_and_stays_local(store, monkeypatch):
    monkeypatch.setattr(handler, "live_minutes_today", lambda: 30.0)
    monkeypatch.setattr(handler, "LIVE_DAY_MIN", 30)
    session = _Session("Hey Steve, let's talk live")
    engine, _ = _desk(monkeypatch, session)
    _forbid_live(engine, monkeypatch)

    await _capture(engine, _heard())

    assert session.said and "used up" in session.said[0]
    assert engine.call is None and engine.mode == "follow" and session.asked == []


async def test_a_spending_cap_reached_keeps_the_live_voice_closed(store, monkeypatch):
    """The live voice is billed as it passes, and nothing asked the caps
    before it opened: it came from the phone, whose calls were billed on
    another machine. A month already spent could still open a conversation
    at the desk, minutes left in its own day or not."""
    from core import spend

    asked = []

    def refused(conn, *, unit=None, **kwargs):
        asked.append((unit.category, unit.part))
        return "the month's cap"

    monkeypatch.setattr(spend, "may_spend", refused)
    monkeypatch.setattr(handler, "live_minutes_today", lambda: 0.0)
    session = _Session("Hey Steve, let's talk live")
    engine, _ = _desk(monkeypatch, session)
    _forbid_live(engine, monkeypatch)

    await _capture(engine, _heard())

    assert ("chat", "live") in asked
    assert session.said and "used up" in session.said[0]
    assert engine.call is None and session.asked == []


# ---------------------------------------------------------------------------
# What their assistant may ask of the desk (agent/toolset/desk.talk_it_through)
# ---------------------------------------------------------------------------


class _InTurn:
    """`runtime.desk_turn` set to `turn`, as the engine sets it around their
    assistant's answer."""

    def __init__(self, turn):
        self.turn = turn

    def __enter__(self):
        self.token = runtime.desk_turn.set(self.turn)
        return self.turn

    def __exit__(self, *exc):
        runtime.desk_turn.reset(self.token)


async def test_a_live_conversation_is_not_something_their_assistant_can_choose_to_open():
    """The maintainer, 9 October 2026 (decision C): it opened two that repeated what
    the local voice had just said. The user asks for one by name; their assistant
    answers where it is."""
    assert engine_mod.LIVE_OPENED_BY == "user"
    turn = Turn(id=1, source="wake", started=0.0)

    with _InTurn(turn):
        said = await _talk_it_through()(_ctx("voice"))

    assert not said["ok"] and "by name" in said["error"] and "live" not in turn.asks


@pytest.mark.parametrize("source", ["wake", "follow"])
async def test_under_decision_a_it_is_accepted_in_their_own_spoken_turn(monkeypatch, source):
    monkeypatch.setattr(engine_mod, "LIVE_OPENED_BY", "user and assistant")
    turn = Turn(id=1, source=source, started=0.0)

    with _InTurn(turn):
        said = await _talk_it_through()(_ctx("voice"))

    assert said["ok"] and turn.asks == {"live": True}


@pytest.mark.parametrize(("source", "cut_in", "channel", "refused_with"), [
    ("wake", False, "signal", "only in a spoken turn"),
    ("wake", True, "voice", "cut in"),
    ("hotkey", False, "voice", "hey Steve"),
    ("handover", False, "voice", "hey Steve"),
    ("live", False, "voice", "hey Steve"),
    ("barge", False, "voice", "hey Steve"),
])
async def test_under_decision_a_a_turn_that_is_not_their_own_spoken_one_cannot_ask(
        monkeypatch, source, cut_in, channel, refused_with):
    """8 October 2026 audit, finding 6: a hotkey turn, or a live
    conversation's handoff, set the ask for a handler that was not reading
    it, and the next unrelated "hey Steve, what's the weather" opened a
    billed live conversation. And 9 October: a cut-in's capture holds the
    desk's own voice, and "stop" arrived as "I'll talk."."""
    monkeypatch.setattr(engine_mod, "LIVE_OPENED_BY", "user and assistant")
    turn = Turn(id=1, source=source, started=0.0, cut_in=cut_in)

    with _InTurn(turn):
        said = await _talk_it_through()(_ctx(channel))

    assert not said["ok"] and refused_with in said["error"] and turn.asks == {}


async def test_under_decision_a_a_tool_run_outside_any_desk_turn_cannot_ask(monkeypatch):
    monkeypatch.setattr(engine_mod, "LIVE_OPENED_BY", "user and assistant")
    assert runtime.desk_turn.get() is None

    said = await _talk_it_through()(_ctx("voice"))

    assert not said["ok"]


def test_talk_it_through_is_offered_only_where_their_assistant_may_open_one(monkeypatch):
    import asyncio as aio

    from pydantic_ai.tools import ToolDefinition

    from agent import toolset
    from agent.turn import Deps

    shown = [ToolDefinition(name="talk_it_through"), ToolDefinition(name="web_search")]

    def names(channel):
        ctx = type("C", (), {"deps": Deps(conn=None, session_id=1, channel=channel)})()
        return {t.name for t in aio.run(toolset.offered(ctx, list(shown)))}

    assert "talk_it_through" not in names("voice")
    monkeypatch.setattr(engine_mod, "LIVE_OPENED_BY", "user and assistant")
    assert "talk_it_through" in names("voice") and "talk_it_through" not in names("signal")


async def test_under_decision_a_their_assistant_opens_one_on_the_reply_it_has_said(store,
                                                                                 monkeypatch):
    """The conversation opens once the turn is over, on the reply just said,
    given as history: GPT-Live carries on from it and never says it again."""
    monkeypatch.setattr(engine_mod, "LIVE_OPENED_BY", "user and assistant")
    live = _Live()

    async def connect():
        return live

    session = _Asks("Hey Steve, help me plan a trip", reply="Sure. What's the trip for?")
    engine, _ = _desk(monkeypatch, session, connect=connect)

    engine.ear_event(listen.EarEvent("captured", 0.0, heard=_heard(), barge=False))
    assert await _until(lambda: engine.mode == "live" and engine.call is not None
                        and engine.call.live is not None)

    assert session.answer["ok"]
    start = live.sent[0]["session"]
    assert start["input"][-1]["role"] == "assistant"
    assert start["input"][-1]["content"][0]["text"] == "Sure. What's the trip for?"
    assert not any(e.get("content") == desk_call.ANSWER_NOW for e in live.sent), \
        "it was told to answer a question nobody had asked it"
    await _hear_him_say_hey_steve(engine)


async def test_a_turn_that_got_hold_of_the_ask_by_another_road_opens_nothing_by_default(
        store, monkeypatch):
    """The engine, as the second lock: with a live conversation theirs alone, it
    does not read a "live" ask a turn left on itself."""

    class Sneaky(_Session):
        async def respond(self, transcript, *, released, text=None, timing=None, out=None):
            runtime.desk_turn.get().asks["live"] = True
            return await super().respond(transcript, released=released, text=text,
                                         timing=timing, out=out)

    engine, _ = _desk(monkeypatch, Sneaky("Hey Steve, help with a trip to Boston"))
    _forbid_live(engine, monkeypatch)

    await _capture(engine, _heard())

    assert engine.call is None and engine.mode == "follow"


@pytest.mark.parametrize("channel", ["signal", "tray"])
async def test_a_turn_on_another_channel_cannot_ask_for_a_conversation(store, monkeypatch,
                                                                       channel):
    monkeypatch.setattr(engine_mod, "LIVE_OPENED_BY", "user and assistant")
    session = _Asks("Hey Steve, help with a trip", channel=channel)
    engine, _ = _desk(monkeypatch, session)
    _forbid_live(engine, monkeypatch)

    await _capture(engine, _heard())

    assert not session.answer["ok"] and engine.call is None


async def test_the_desk_turn_is_visible_to_the_tools_its_assistant_calls_and_gone_after(store):
    """What a tool asks of the desk is written on the turn it runs in, found
    through a context variable: `DeskShell.answer` sets it around their
    assistant's answer -- and nothing is left set when it is over."""
    seen = []

    class Probe(_Session):
        async def respond(self, transcript, *, released, text=None, timing=None, out=None):
            seen.append(runtime.desk_turn.get())
            return await super().respond(transcript, released=released, text=text,
                                         timing=timing, out=out)

    shell = handler.DeskShell(Probe("x"), blocking=_inline)
    turn = Turn(id=7, source="wake", started=0.0, request="what's the weather")

    await shell.answer(turn)

    assert seen == [turn] and runtime.desk_turn.get() is None


async def test_the_desk_turn_follows_the_turn_into_the_tools_an_agent_runs():
    """`talk_it_through` and `take_notes` trust a context variable set around
    the model's turn: a tool run by an agent, in its own task, has to see it
    -- and one run outside it must not."""
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    seen = []
    agent = Agent(TestModel())

    @agent.tool_plain
    async def probe() -> str:
        seen.append(runtime.desk_turn.get())
        return "ok"

    turn = Turn(id=1, source="wake", started=0.0)
    await agent.run("go")
    with _InTurn(turn):
        await agent.run("go")

    assert seen == [None, turn]


# ---------------------------------------------------------------------------
# What a conversation spent, and what the sound test can read afterwards
# (audit of 8 October 2026)
# ---------------------------------------------------------------------------


def _usage(conn):
    return conn.execute("SELECT u.id, u.cost_usd, u.latency_ms, s.channel FROM usage_events u"
                        " LEFT JOIN sessions s ON s.id = u.session_id").fetchall()


def _shell(monkeypatch, session=None):
    """The worker's side of the desk, with the engine it belongs to."""
    engine = Engine(handler.DeskShell(session or _Session(), blocking=_inline), Floor(_Player()))
    return engine.shell, engine


def _call(clock=None):
    """A live client with nothing connected, its clock its own."""
    clock = clock or _Clock()
    call = desk_call.LiveCall(floor=Floor(_Player(), clock=clock), words=desk_call.Words(),
                              ask=None, clock=clock)
    call.live = _Live()
    return call, clock


async def test_a_call_is_billed_while_it_runs_and_its_bills_add_up_to_it(store, monkeypatch):
    """Billed only at the end, a worker that died mid-call -- or a swap --
    left a spent conversation uncounted. Each beat bills only what passed
    since the last, so the bills add up to the call, not to a multiple of it."""
    from agent.models import LIVE_VOICE

    monkeypatch.setattr(handler, "LIVE_BEAT_S", 0.02)
    shell, _ = _shell(monkeypatch)
    call, clock = _call()
    clock.t += 90.0  # as if it had been open a minute and a half

    shell.live_started(call)
    assert await _until(lambda: _usage(store)), "billed while it runs"
    await asyncio.sleep(0.1)  # several beats, with nothing more passed

    call.seconds = 100.0  # what GPT-Live reports at the end
    shell.live_ended(call, "quiet", unanswered="")
    await asyncio.sleep(0.1)  # a beat left running would bill its clock again

    rows = _usage(store)
    assert [r["channel"] for r in rows] == ["voice", "voice"]
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(LIVE_VOICE.cost_usd(100.0))
    assert [tuple(r) for r in store.execute(
        "SELECT DISTINCT category, part FROM usage_events")] == [("chat", "live")]


async def test_a_call_that_never_reports_its_seconds_is_counted_by_its_own_clock(store,
                                                                                monkeypatch):
    shell, _ = _shell(monkeypatch)
    call, clock = _call()
    shell.live_started(call)
    clock.t += 60.0

    shell.live_ended(call, "voice lost", unanswered="")

    (row,) = _usage(store)
    assert row["latency_ms"] >= 59_000 and handler._state["last_call"]["seconds"] == 60


async def test_a_conversation_that_never_connected_leaves_no_minutes(store, monkeypatch):
    shell, _ = _shell(monkeypatch)
    call, _ = _call()
    call.live = None

    shell.live_ended(call, "unavailable", unanswered="")

    assert _usage(store) == []


async def test_a_conversation_with_nothing_handed_over_is_still_counted(store, monkeypatch):
    """"Let's talk live" with nothing yet said to the desk leaves no voice
    message in the history window, so `recent_session` was None and the
    minutes were never written: the day's cap and the spend report both
    missed them."""
    from agent.models import LIVE_VOICE

    shell, _ = _shell(monkeypatch)
    call, _ = _call()
    assert store.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    shell.live_started(call)
    call.seconds = 120.0
    shell.live_ended(call, "quiet", unanswered="")

    (row,) = _usage(store)
    assert row["channel"] == "voice"
    assert row["cost_usd"] == pytest.approx(LIVE_VOICE.cost_usd(120.0))
    assert handler.live_minutes_today() == pytest.approx(2.0)


def test_what_was_said_is_kept_even_when_the_desk_thread_has_no_message_yet(store):
    talked = desk_call.Session()
    talked.note("them", "let's plan the week")

    handler._keep_transcript(talked)

    row = store.execute("SELECT m.tool_name, s.channel FROM messages m JOIN sessions s"
                        " ON s.id = m.session_id").fetchone()
    assert (row["tool_name"], row["channel"]) == ("live_conversation", "voice")


async def test_a_live_conversation_is_kept_on_the_desks_thread_but_not_replayed(store,
                                                                               monkeypatch):
    """"It misses half of what I say" could not be checked: nothing of the
    conversation itself was kept. It is a tool row, which the desk's thread
    does not replay as turns -- the requests it handed over are there as
    turns already."""
    with store:
        store.execute("INSERT INTO sessions (id, channel) VALUES (40, 'voice')")
        store.execute("INSERT INTO messages (session_id, role, content) "
                      "VALUES (40, 'user', 'what''s the weather')")
    shell, _ = _shell(monkeypatch)
    call, _ = _call()
    call.session.note("them", "can you Google my Northwind contact")
    call.session.note("us", "Let me check.")

    shell.live_ended(call, "quiet", unanswered="")

    row = store.execute("SELECT role, tool_name, content FROM messages WHERE session_id = 40"
                        " ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["role"], row["tool_name"]) == ("tool", "live_conversation")
    assert "you: can you Google my Northwind contact" in row["content"]
    assert "me: Let me check." in row["content"]
    replayed = " ".join(text for _, text in handler._recent_turns())
    assert "Northwind" not in replayed and "weather" in replayed


async def test_a_live_conversation_leaves_one_log_line_with_numbers_and_no_words(store,
                                                                                monkeypatch,
                                                                                caplog):
    shell, _ = _shell(monkeypatch)
    call, _ = _call()
    call.session.note("them", "my secret plan")
    call.session.turns.append(desk_call.Turn(said="Your secret plan is ready.",
                                             heard="my secret plan", seconds=2.0))
    call.seconds = 42.0
    shell.live_started(call)

    with caplog.at_level("INFO", logger="desk"):
        shell.live_ended(call, "quiet", unanswered="my secret plan")

    (line,) = [r.getMessage() for r in caplog.records
               if "a live conversation ended:" in r.getMessage()]
    assert "reason=quiet" in line and "seconds=42" in line and "handed_over=1" in line
    assert "unanswered=yes" in line
    assert "secret" not in caplog.text


# ---------------------------------------------------------------------------
# Switching off
# ---------------------------------------------------------------------------


class _FakeCall:
    """A live conversation as far as the engine needs one to end it: it
    records that it was asked to close, and why."""

    live = object()
    closing = False
    ended_at = None

    def __init__(self):
        self.closed = []

    def feed(self, block):
        pass

    async def close(self, reason):
        self.closing = True
        self.closed.append(reason)


async def test_switching_hands_free_off_closes_a_live_conversation(store, monkeypatch):
    """Switching off -- also the swap's -- left the call open until its 20 s
    quiet rule and 5 s close wait ran out, on a worker that had given up the
    microphone. The engine ends it, with the reason."""
    engine, _ = _desk(monkeypatch, _Session())
    call = engine.call = _FakeCall()
    engine._to("live", "user")

    engine.switch_off()
    await engine.settle()

    assert call.closed == ["off"] and engine.mode == "off"


@pytest.mark.parametrize("reason", ["off", "swap"])
async def test_the_workers_stop_ends_a_live_conversation_with_its_reason(store, monkeypatch,
                                                                       reason):
    engine, _ = _desk(monkeypatch, _Session())
    call = engine.call = _FakeCall()
    engine._to("live", "user")

    handler.stop(reason=reason)
    await engine.settle()

    assert call.closed == [reason]
    assert (handler._state["engine"] is None) is (reason == "swap"), \
        "a swap leaves the new worker its own engine; going off keeps this one"


def test_stopping_with_no_loop_running_does_not_fail(monkeypatch):
    for key in ("engine", "ears", "ticker", "listener"):
        monkeypatch.setitem(handler._state, key, None)
    monkeypatch.setattr(runtime, "voice", None)

    handler.stop()  # nothing running: nothing to close, and nothing raised
    handler.stop(reason="swap")

    assert handler._state["ears"] is None and handler._state["engine"] is None


def test_stopping_with_an_engine_and_no_loop_running_does_not_fail(monkeypatch):
    engine = Engine(handler.DeskShell(_Session()), Floor(_Player()))
    monkeypatch.setitem(handler._state, "engine", engine)
    for key in ("ears", "ticker", "listener"):
        monkeypatch.setitem(handler._state, key, None)
    monkeypatch.setattr(runtime, "voice", None)

    handler.stop()

    assert engine.mode == "off" and engine.ear is None


# ---------------------------------------------------------------------------
# One line in the log for every capture, in numbers and never in words
# ---------------------------------------------------------------------------


async def test_a_turn_leaves_one_log_line_with_timings_and_no_words(store, monkeypatch, caplog):
    """The 7 October hands-free logs had no turn lines, so the end-of-turn
    constants and the first word's latency could not be tuned from them."""
    session = _Session("Hey Steve, what's the weather in Hartford", "Sixty and clear.")
    engine, _ = _desk(monkeypatch, session)
    heard = _heard(ended_by="judge", asks=((0.42, 0.31), (1.22, 0.77)), seconds=2.5)

    with caplog.at_level("INFO", logger="desk"):
        await _capture(engine, heard)

    (line,) = _turn_lines(caplog)
    for field in ("outcome=answered", "source=wake", "woke=True", "captured_s=2.50",
                  "ended_by=judge", "asks=0.42:0.31,1.22:0.77", "stt_ms=1", "agent_ms=0",
                  "first_audio_ms=0", "stall=False", "preamble=False", "cut=False", "error=-"):
        assert field in line, field
    assert "weather" not in caplog.text and "Hartford" not in caplog.text
    assert "Sixty" not in caplog.text


@pytest.mark.parametrize(("said", "woke", "laptop", "outcome"), [
    ("so what did you think of the game", True, 0.0, "false_wake"),
    ("and forward it to my backup address", False, 3.0, "follow_up_dropped"),
    ("Hey Steve", True, 0.0, "phrase_alone"),
    ("Hey Steve, never mind", True, 0.0, "never_mind"),
    ("Hey Steve, what's the weather in Hartford", True, 0.0, "answered"),
])
async def test_every_capture_leaves_a_line_saying_what_became_of_it(store, monkeypatch, caplog,
                                                                    said, woke, laptop, outcome):
    engine, _ = _desk(monkeypatch, _Session(said))

    with caplog.at_level("INFO", logger="desk"):
        await _capture(engine, _heard(woke=woke, laptop=laptop))

    (line,) = _turn_lines(caplog)
    assert f"outcome={outcome}" in line
    for words in ("backup address", "the game", "Hartford"):
        assert words not in caplog.text


async def test_a_turn_that_fails_is_a_line_too_and_leaves_the_desk_listening(store, monkeypatch,
                                                                            caplog):
    class Broken(_Session):
        def transcribe(self, audio):
            raise RuntimeError("whisper fell over")

    engine, _ = _desk(monkeypatch, Broken("x"))

    with caplog.at_level("INFO", logger="desk"):
        await _capture(engine, _heard())

    assert len(_turn_lines(caplog)) == 1
    assert "a step failed" in caplog.text
    assert engine.mode == "idle" and engine.turn is None, "a failed step left the desk deaf"


# ---------------------------------------------------------------------------
# "Hey Steve, stop", said over its voice (9 October 2026, the first sound test)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("said", [
    "Hey Steve, stop", "Hey Steve, stop.", "Hey Steve, stop it", "Hey Steve, stop, stop, stop",
    "Hey Steve, okay, thanks", "Hey Steve, thanks", "Hey Steve, thank you very much",
    "Hey Steve, be quiet", "Hey Steve, quiet", "Hey Steve, shut up", "Hey Steve, that's enough",
    "Hey Steve, enough", "Hey Steve, never mind", "Hey Steve, cancel", "Hey Steve, stop talking",
    "Hey Steve, okay stop please", "Hey Steve, hold on", "Hey Steve, that's all, thanks",
])
async def test_a_stop_said_over_its_voice_stops_it_and_is_answered_by_nothing(
        store, monkeypatch, caplog, said):
    """The maintainer, 9 October 2026: "it heard me say hey steve stop but usually
    would respond by jumping back to the beginning of what it said not
    stopping." The cut-in has already cut its voice; what is left for the
    model to say about "stop" is only more speech, and a billed
    conversation once. No model, no sound, no chime -- and the ear is not
    left open for a follow-up the user did not ask for."""
    session = _Session(said)
    engine, player = _desk(monkeypatch, session)
    _forbid_live(engine, monkeypatch)

    with caplog.at_level("INFO", logger="desk"):
        await _capture(engine, _cut_in())

    assert session.asked == [], "the model was asked about a stop"
    assert session.said == [] and player.sounds == [], "it spoke after being told to stop"
    assert engine.mode == "idle", "a follow-up window was opened for a stop"
    (line,) = _turn_lines(caplog)
    # "never mind" and "cancel" were answered by code before, as `never_mind`.
    assert "outcome=cut_in_stop" in line or "outcome=never_mind" in line
    assert "barge_in=True" in line


async def test_stop_said_when_nothing_was_talking_is_left_to_its_assistant(store, monkeypatch):
    """The rule is for a cut-in. Elsewhere "thanks" is a remark a person
    expects an answer to, and it already had one."""
    session = _Session("Hey Steve, thanks", "You're welcome.")
    engine, _ = _desk(monkeypatch, session)

    await _capture(engine, _heard())

    assert session.asked == ["thanks"]


@pytest.mark.parametrize("said", [
    "Hey Steve, stop reading and tell me the time",
    "Hey Steve, thanks, and what's the weather tomorrow",
    "Hey Steve, cancel my three o'clock",
    "Hey Steve, don't stop",
])
async def test_a_cut_in_that_only_starts_like_a_stop_is_still_a_request(store, monkeypatch, said):
    session = _Session(said, "Okay.")
    engine, _ = _desk(monkeypatch, session)

    await _capture(engine, _cut_in())

    assert len(session.asked) == 1 and engine.mode == "follow"


async def test_hey_steve_stop_heard_as_ill_talk_does_not_open_a_live_conversation(
        store, monkeypatch, caplog):
    """The incident itself: over the answer about Pittsburgh, the capture's
    words came back from Whisper as "I'll talk." -- two words, with nothing
    said after the wake (the voice detector heard none; the words were in the
    pre-roll with the desk's own voice) -- and the model chose
    `talk_it_through`: $0.05 a minute, and the answer started again."""
    monkeypatch.setattr(engine_mod, "LIVE_OPENED_BY", "user and assistant")
    session = _Asks("Hey Steve, I'll talk.")
    engine, _ = _desk(monkeypatch, session)
    _forbid_live(engine, monkeypatch)

    with caplog.at_level("INFO", logger="desk"):
        await _capture(engine, _cut_in(speech_s=0.0))

    assert session.answer is None and session.asked == [], "a model was asked"
    assert engine.call is None and engine.mode == "idle"
    (line,) = _turn_lines(caplog)
    assert "outcome=cut_in_noise" in line and "speech_after_wake_s=0.00" in line
    assert "talk" not in caplog.text, "never what she said"


async def test_whatever_whisper_made_of_a_cut_in_it_cannot_open_a_live_conversation(store,
                                                                                    monkeypatch):
    """Short and unspoken-after-the-wake is dropped above; this is the case
    that reaches the model -- the user did speak after the phrase -- and the model
    asks for a conversation, under the setting that lets it. A cut-in turn is
    answered where it is, and the user can say "let's talk live"."""
    monkeypatch.setattr(engine_mod, "LIVE_OPENED_BY", "user and assistant")
    session = _Asks("Hey Steve, help me with the trip to Boston")
    engine, _ = _desk(monkeypatch, session)
    _forbid_live(engine, monkeypatch)

    await _capture(engine, _cut_in(speech_s=1.2))

    assert session.answer["ok"] is False and "cut in" in session.answer["error"]
    assert engine.call is None and engine.mode == "follow"


async def test_a_cut_in_that_asks_for_a_live_conversation_by_name_opens_one(store, monkeypatch):
    live = _Live()

    async def connect():
        return live

    session = _Session("Hey Steve, actually let's talk live about this trip")
    engine, _ = _desk(monkeypatch, session, connect=connect)

    await _capture_until_open(engine, _cut_in(speech_s=1.0))

    assert session.asked == [] and engine.call.words.untaken().endswith("about this trip")
    await _hear_him_say_hey_steve(engine)


async def test_a_wake_over_the_desks_voice_with_no_request_still_gets_its_chime(store,
                                                                                monkeypatch):
    """"Hey Steve" alone, over an answer, is "wait, I have something to ask":
    the chime and the moment to ask are what the user had before and still gets."""
    engine, player = _desk(monkeypatch, _Session("Hey Steve"))

    await _capture(engine, _cut_in())

    assert len(player.sounds) == 1 and engine.mode == "follow"


@pytest.mark.parametrize(("said", "speech_s", "dropped"), [
    ("Hey Steve, hmm", 1.0, True),            # no word at all
    ("Hey Steve, uh, um", 1.0, True),
    ("Hey Steve, I'll talk", 0.0, True),      # a word or two, nothing after the wake
    ("Hey Steve, go on", 0.0, True),
    ("Hey Steve, go on", 0.8, False),         # the same words, said after it
    ("Hey Steve, what time is it", 0.0, False),  # more than a word or two
    ("Hey Steve, stop taking notes", 0.0, False),
])
def test_what_a_cuts_in_capture_came_to_is_told_from_noise(said, speech_s, dropped):
    request = rules.wake_phrase(said)
    assert handler.cut_in_noise(request, _cut_in(speech_s=speech_s)) is dropped


# ---------------------------------------------------------------------------
# A conversation that has gone quiet closes, whatever is fooling one detector
# (9 October 2026: open from 08:05 to 08:10, with nothing said to it after
# about 08:06, until hands-free was switched off; $0.05 a minute)
#
# The rules are the engine's (voice/engine.py `_live_tick`): after IDLE_S with
# nothing from either side -- its voice, them, work in hand -- or SILENT_S with
# no word from either and no work. Run here on a clock turned by hand, over
# the real live client on a fake socket.
# ---------------------------------------------------------------------------


class _RoomEar:
    """The two things the engine asks of the ear in a live conversation:
    when the microphone last heard speech, and what the laptop was playing."""

    def __init__(self):
        self.speech_at = 0.0
        self.laptop = 0.0
        self.mode = None
        self.loopback = self

    def speech_between(self, start, end):
        return self.laptop

    def set_mode(self, mode):
        self.mode = mode


class _Room:
    """A live conversation the desk opened at their request, and a clock the
    test turns a second at a time."""

    def __init__(self, engine, call, live, player, ear, clock):
        self.engine, self.call, self.live = engine, call, live
        self.player, self.ear, self.clock = player, ear, clock

    async def it_says(self, text, *, seconds=0.1, played=True):
        """It speaks: its transcript, and audio that the speakers play -- or,
        with `played` False, that sits in a queue nothing is taking."""
        self.live.push(type="session.output_transcript.delta", delta=text)
        self.live.push(type="session.output_audio.delta",
                       delta=_frame(0.2, int(24_000 * seconds)))
        assert await _until(lambda: self.player.queued)
        if played:
            self.player.play_all(self.engine.floor)

    async def seconds(self, n, *, user=False, room=False):
        """Let `n` seconds pass, one at a time, with the engine looking at its
        clocks each; returns how long it took to close, or None if it did not.
        `room`: GPT-Live's transcript of the room has a "mm" in it each second."""
        for second in range(1, n + 1):
            self.clock.t += 1.0
            if user:
                self.ear.speech_at = self.clock.t
            if room:
                self.live.push(type="session.input_transcript.delta", delta=" mm")
            self.engine.tick()
            for _ in range(8):
                await asyncio.sleep(0)
            if self.call.closing:
                await self.engine.settle()
                return second
        return None

    @property
    def ended_by(self):
        return handler._state["last_call"]["reason"]


async def _room(monkeypatch, *, reply=None):
    clock, live, player = _Clock(), _Live(), _Player()

    async def connect():
        return live

    if reply is not None:
        from agent.worker import answering

        monkeypatch.setattr(answering, "voice_reply", reply)
    shell = handler.DeskShell(_Session("Hey Steve, let's talk live"), clock=clock,
                              blocking=_inline, connect=connect)
    engine = Engine(shell, Floor(player, clock=clock), clock=clock)
    ear = _RoomEar()
    engine.attach(ear)
    engine.opened()
    monkeypatch.setitem(handler._state, "engine", engine)
    await _capture_until_open(engine, _heard())
    return _Room(engine, engine.call, live, player, ear, clock)


async def test_a_conversation_that_has_gone_quiet_closes(store, monkeypatch):
    room = await _room(monkeypatch)
    await room.it_says("Sure, go ahead.")

    closed_after = await room.seconds(60)

    assert closed_after is not None and closed_after <= engine_mod.IDLE_S + 1
    assert room.ended_by == "quiet" and room.engine.call is None


async def test_its_transcript_of_the_room_does_not_keep_it_open(store, monkeypatch):
    """7 October 2026: open at 12:33, one request handed over, still open at
    12:41 -- $0.38. GPT-Live's transcript of noise and its own echo kept
    counting as them. It is kept for the record and read by nobody."""
    room = await _room(monkeypatch)
    await room.it_says("Sure, go ahead.")

    closed_after = await room.seconds(60, room=True)

    assert closed_after is not None and closed_after <= engine_mod.IDLE_S + 1
    assert room.ended_by == "quiet"


async def test_a_player_stuck_on_queued_audio_does_not_keep_a_quiet_call_open(store,
                                                                              monkeypatch):
    """The status showed it speaking a minute apart while the user sat silent: a
    stream that stopped -- its device went away, its callback raised -- leaves
    its queue as it was for good, and the queue was taken for its voice. The
    floor counts a sound as audible only when the player says it played it,
    so a queue nothing takes is not its voice, and the call goes quiet."""
    room = await _room(monkeypatch)
    await room.it_says("Sure, go ahead.", played=False)
    assert room.player.queued, "the audio is waiting in the queue, and will never play"

    closed_after = await room.seconds(60)

    assert closed_after is not None and closed_after <= engine_mod.IDLE_S + 1
    assert room.ended_by == "quiet"


async def test_while_the_user_speaks_it_stays_open_and_when_the_user_stops_it_closes(store,
                                                                                     monkeypatch):
    room = await _room(monkeypatch)
    await room.it_says("Sure, go ahead.")

    assert await room.seconds(30, user=True) is None, "closed on her mid-sentence"
    closed_after = await room.seconds(60)  # the user stops

    # By the first rule at IDLE_S after the user stopped, or the second, which has
    # been counting since its last word (no word of their reached it).
    assert closed_after is not None and closed_after <= engine_mod.IDLE_S + 1
    assert room.ended_by in ("quiet", "silent")


async def test_speech_over_a_video_or_a_call_is_not_them(store, monkeypatch):
    """8 October 2026 audit, finding 7: the microphone's voice detector hears
    a video too, so anything with a voice in it kept a billed conversation
    open to its fifteen-minute limit ($0.75)."""
    room = await _room(monkeypatch)
    await room.it_says("Sure, go ahead.")
    room.ear.laptop = 1.0  # the laptop is playing speech, and the microphone hears it

    closed_after = await room.seconds(60, user=True)

    assert closed_after is not None and closed_after <= engine_mod.IDLE_S + 1
    assert room.ended_by == "quiet"


async def test_what_the_microphone_heard_inside_its_own_voice_is_not_them(store, monkeypatch):
    """On the speakers the microphone hears it: speech inside its own voice,
    and the room's echo of it, is not counted as them."""
    room = await _room(monkeypatch)
    await room.it_says("Sure, go ahead.")
    room.ear.speech_at = room.clock.t + 0.4  # inside its voice and its echo tail

    closed_after = await room.seconds(60)

    assert closed_after is not None and closed_after <= engine_mod.IDLE_S + 0.5
    assert room.ended_by == "quiet"


async def test_a_microphone_that_hears_speech_in_keyboard_noise_does_not_hold_a_silent_call(
        store, monkeypatch):
    """The voice detector firing on typing near the microphone kept the first
    rule satisfied -- the user was "speaking" -- while GPT-Live heard no word. The
    second rule does not look at the detector: no word from either side and
    no work for SILENT_S, and it closes."""
    room = await _room(monkeypatch)
    await room.it_says("Sure, go ahead.")

    closed_after = await room.seconds(120, user=True)

    assert closed_after is not None
    assert engine_mod.SILENT_S - 1 <= closed_after <= engine_mod.SILENT_S + 1
    assert room.ended_by == "silent"


async def test_a_call_in_which_the_user_is_talking_and_it_is_answering_stays_open(store,
                                                                                  monkeypatch):
    """A real back-and-forth: their words arriving, the microphone hearing them,
    its voice coming back. Both rules stay satisfied for as long as it goes
    on -- well past either rule's time -- then it closes when it stops."""
    room = await _room(monkeypatch)

    for _ in range(12):  # a minute of it, with a sentence from each every five seconds
        await room.it_says("And Friday looks free.")
        room.call.words.add("and what about Friday", room.clock.t - 1, room.clock.t)
        assert await room.seconds(5, user=True) is None, "closed in the middle of a conversation"

    closed_after = await room.seconds(120)  # they stop
    assert closed_after is not None and room.ended_by in ("quiet", "silent")


async def test_a_call_waiting_on_their_assistant_is_not_silent(store, monkeypatch):
    """A handoff can take a minute (research, a calendar, a model that is
    slow): while work is in hand, neither rule closes the call."""
    release = asyncio.Event()
    asked = []

    async def slow(text, channel="voice"):
        asked.append(text)
        await release.wait()
        return "Three meetings.", None

    room = await _room(monkeypatch, reply=slow)
    await room.it_says("Let me check.")
    room.call.words.add("what's on tomorrow", room.clock.t - 1, room.clock.t)
    room.live.push(type="session.delegation.created", delegation={"id": "d1"})
    assert await _until(lambda: asked)

    assert await room.seconds(90) is None, "closed on her assistant while it was working"
    release.set()
    assert await _until(lambda: any(e["type"] == "session.commentary.append"
                                    for e in room.live.sent))
    closed_after = await room.seconds(120)

    assert closed_after is not None and room.ended_by in ("quiet", "silent")
    assert asked == ["what's on tomorrow"]


async def test_a_conversation_does_not_run_past_what_is_left_of_the_days_minutes(store,
                                                                                monkeypatch):
    """Told to wrap up, first, and then closed at the limit: $0.05 a minute
    is capped at what the day has left (`LIVE_DAY_MIN`)."""
    monkeypatch.setattr(handler, "live_minutes_today", lambda: handler.LIVE_DAY_MIN - 0.7)
    room = await _room(monkeypatch)  # 42 seconds left
    await room.it_says("Sure, go ahead.")

    closed_after = await room.seconds(120, user=True)

    assert closed_after is not None and 41 <= closed_after <= 43
    assert room.ended_by == "time limit"
    wraps = [e for e in room.live.sent if e.get("content") == desk_call.WRAP_UP]
    assert len(wraps) == 1, "it was told to wrap up once, before the end"


async def test_gpt_live_is_given_no_waiting_cards_word_to_say(store):
    """GPT-Live speaks: told a waiting card's word, it could say a card of
    its own around it aloud, and a yes at the desk would answer the real
    one (agent/cardwords.py). Neither the history it starts from nor the
    answers it is given to say hold the word."""
    from agent import approvals
    from core.consent import classify

    email = {"to": "hugo@example.com", "subject": "Lunch", "body": "Thursday works."}
    receipt = await approvals.request(store, tool="send_email", args=email,
                                      preview="Email to hugo@example.com: Lunch",
                                      consent=classify("send_email", email))
    word = approvals.get(store, receipt["action_id"]).token
    call, live, *_ = await _opened(history=[("assistant", f"Reply YES {word} to go ahead.")])
    running = asyncio.ensure_future(call.run())

    await call.append("session.commentary.append", "d1", f"Tell them: reply yes {word.lower()}.")
    await _finish(call, running)

    sent = json.dumps(live.sent).lower()
    # As a word: a card's may be part of another ("ember" in "remember").
    assert not re.search(rf"(?<![a-z]){word.lower()}(?![a-z])", sent)
    assert "reply yes [the card's word] to go ahead" in sent
    assert "tell them: reply yes [the card's word]." in sent
