"""Notes of what the laptop plays -- a Zoom call, a video -- heard on its way
out rather than through a microphone (voice/listen.Tap). The maintainer, 5 October
2026. That none of it can reach a model is tests/gate/test_desk.py's.

The desk's side runs through the real engine (voice/engine.py) and the
worker's side of it (agent/worker/desk.DeskShell), as the gate's tests do,
with a small fake for the speech session and the ear. Until 9 October 2026
these called `desk._handle` with a fake listener and read the calls it
recorded; the engine owns the notes switch now (`Engine.notes_on`), and what
a tool asks lives on the turn (`runtime.desk_turn`)."""

from __future__ import annotations

import asyncio
import sys
import time

import numpy as np
import pytest

from agent import notes, runtime
from agent.worker import desk
from core import db
from tests.gate.extras import needs_voice
from voice import listen
from voice.engine import Engine, Turn
from voice.floor import Floor
from voice.stt import Transcript

BLOCK = 3200  # 200 ms, as the tap reads


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


def _talk(n):
    rng = np.random.default_rng(n)
    return [(0.2 * rng.standard_normal(BLOCK)).astype(np.float32) for _ in range(n)]


def _quiet(n):
    return [np.full(BLOCK, 0.001, np.float32) for _ in range(n)]


def test_what_the_laptop_plays_is_cut_into_stretches_of_speech():
    cut = listen.Segmenter(_Vad(), source="laptop")
    out, t = [], 0.0
    for block in _quiet(5) + _talk(10) + _quiet(5) + _talk(3) + _quiet(5):
        t += 0.2
        out += cut.push(block, t)
    assert len(out) == 2 and all(h.source == "laptop" and h.segment for h in out)
    assert 2.0 <= len(out[0].audio) / 16_000 <= 2.0 + 0.3 + 0.8 + 0.2


def test_someone_who_never_pauses_is_still_written_down():
    cut = listen.Segmenter(_Vad(), source="laptop")
    out, t = [], 0.0
    for block in _talk(int(listen.SEGMENT_MAX_S / 0.2) * 2 + 5):
        t += 0.2
        out += cut.push(block, t)
    assert len(out) >= 2
    assert all(len(h.audio) / 16_000 <= listen.SEGMENT_MAX_S + 0.6 for h in out)


def test_the_tap_hands_on_only_stretches_of_speech():
    handed = []
    tapped = listen.Tap(handed.append, vad=_Vad(), clock=lambda: 0.0)
    for i, block in enumerate(_quiet(3) + _talk(5) + _quiet(5)):
        tapped.push(block, now=i * 0.2)
    assert len(handed) == 1 and handed[0].source == "laptop"


# ---------------------------------------------------------------------------
# The desk's side
# ---------------------------------------------------------------------------


class _Session:
    """The warm speech session, as the desk's worker side sees it: Whisper's
    answer, and what reached their assistant or was said."""

    def __init__(self, text=""):
        self.text = text
        self.asked, self.said = [], []
        self.transcriber = self.player = self

    def transcribe(self, audio):
        return Transcript(text=self.text, language="en", duration_s=1.0, transcribe_ms=1,
                          device="cpu")

    async def respond(self, transcript, *, released, text=None, timing=None, out=None):
        from voice.pipeline import TurnTiming, VoiceTurn

        self.asked.append(text)
        return VoiceTurn(transcript=transcript, reply_text="ok", timing=TurnTiming())

    async def say(self, text, *, out=None):
        self.said.append(text)

    def interrupt(self):
        pass

    def enqueue(self, *a):
        pass

    def wait(self, *a):
        return True


class _Ear:
    """The ear as the engine and the worker's side see it: what the laptop
    played, and the last mode the engine set."""

    def __init__(self, laptop_speech=0.0):
        self.mode = None
        self.speech_at = 0.0
        self.loopback = type("L", (), {"speech_between": lambda s, a, b: laptop_speech})()

    def set_mode(self, mode):
        self.mode = mode


class _Player:
    """The speakers, as the floor sees them. Nothing queued is ever played
    here, so nothing is ever still being said: a chime is over at once."""

    device_rate = 24_000
    is_playing = False
    queued_seconds = 0.0

    def enqueue(self, audio, rate, owner=None):
        pass

    def take(self):
        return []


_REAL_TAP = listen.Tap


class _Tap:
    def __init__(self, on_segment):
        self.ok, self.error, self.last_speech = True, None, 0.0
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


async def _inline(fn, *args):
    return fn(*args)


def _desk(monkeypatch, session, *, laptop=0.0):
    """The desk's engine and worker side, listening, over `session`."""
    engine = Engine(desk.DeskShell(session, blocking=_inline), Floor(_Player()))
    monkeypatch.setitem(desk._state, "engine", engine)
    engine.attach(_Ear(laptop))
    engine.opened()
    return engine


async def _handle(engine, heard):
    """A capture, run through the real desk to the end of its turn."""
    engine.ear_event(listen.EarEvent("captured", 0.0, heard=heard, barge=heard.barge_in))
    await engine.settle()


@pytest.fixture
def desk_state(tmp_path, monkeypatch):
    conn = db.open_store(tmp_path / "desk.db")
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(runtime, "voice", _Session())
    monkeypatch.setattr(notes, "notes_dir", lambda: tmp_path / "notes")
    monkeypatch.setattr(listen, "Tap", _Tap)
    for key, value in (("ears", None), ("listener", None), ("tap", None), ("watch", None),
                       ("engine", None), ("ticker", None), ("note_heard", time.time())):
        monkeypatch.setitem(desk._state, key, value)
    yield conn
    if desk._state.get("watch") is not None:
        desk._state["watch"].cancel()
    conn.close()


def _heard(source="laptop", laptop=0.0, segment=True):
    return listen.Heard(audio=np.zeros(8000, np.float32), woke=False, laptop_speech_s=laptop,
                        started=0.0, ended=1.0, segment=segment, source=source, kind="segment")


def _woke():
    """A capture that began with the phrase, ended just now on the desk's clock."""
    now = time.monotonic()
    return listen.Heard(audio=np.zeros(16_000, np.float32), woke=True, laptop_speech_s=0.0,
                        started=now - 1.0, ended=now, kind="wake")


async def test_a_call_needs_no_microphone_and_writes_them_and_me(desk_state):
    said = desk.tap(True, title="Call, test")
    assert said["ok"] and desk._state["tap"].started
    note_id = said["note"]

    runtime.voice.text = "Let's move the deadline to Friday."
    await desk._tap_segment(_heard("laptop"))
    runtime.voice.text = "Friday works for me."
    await desk._segment(_heard("mic"), None, runtime.voice)
    runtime.voice.text = "Let's move the deadline to Friday."  # the call, through their speakers
    await desk._segment(_heard("mic", laptop=0.9), None, runtime.voice)

    assert notes.transcript(desk_state, note_id).splitlines()[0].endswith(
        "Them: Let's move the deadline to Friday.")
    lines = [line.split("] ", 1)[1] for line in notes.transcript(desk_state, note_id).splitlines()]
    assert lines == ["Them: Let's move the deadline to Friday.", "Me: Friday works for me."]


async def test_stopping_writes_it_up_without_a_word_aloud(desk_state, monkeypatch):
    """Started from the app, perhaps in public: stopping must not make the
    laptop speak."""
    written = []

    async def summarize(conn, note_id):
        written.append(note_id)
        return {"summary": "x", "doc_url": "https://docs/x", "title": "Call"}

    monkeypatch.setattr(notes, "summarize", summarize)
    tapped = desk.tap(True)
    stopped = desk.tap(False)
    assert stopped["summarizing"]
    for _ in range(50):
        if written:
            break
        await asyncio.sleep(0.01)
    assert written == [tapped["note"]] and runtime.voice.said == []
    assert notes.open_note(desk_state) is None


async def test_a_call_that_goes_quiet_is_written_up_and_the_user_is_told(desk_state, monkeypatch):
    from agent.worker import links

    told = []

    async def notify(text, **kwargs):
        told.append(text)
        return True

    async def summarize(conn, note_id):
        return {"summary": "x", "doc_url": "https://docs/x", "title": "Budget call"}

    monkeypatch.setattr(links, "notify_user", notify)
    monkeypatch.setattr(notes, "summarize", summarize)
    monkeypatch.setattr(desk, "WATCH_S", 0.01)
    desk.tap(True)
    desk._state["tap"].last_speech = time.monotonic() - notes.CALL_IDLE_S - 1
    for _ in range(200):
        if told:
            break
        await asyncio.sleep(0.01)
    assert told and "Budget call" in told[0] and notes.open_note(desk_state) is None


async def test_it_is_not_started_while_speech_is_loading(desk_state, monkeypatch):
    monkeypatch.setattr(runtime, "voice", None)
    assert desk.tap(True) == {"ok": False, "error": "speech is still loading"}


async def test_a_call_being_noted_carries_on_in_the_next_worker(desk_state):
    note_id = notes.start(desk_state, "call", "Call")
    desk.resume_tap()
    assert desk._state["tap"] is not None and desk._state["tap"].started
    assert notes.open_note(desk_state)["id"] == note_id


@pytest.mark.parametrize("said, laptop, kind", [
    ("Hey Steve, take notes on this zoom call", 0.0, "call"),
    ("Hey Steve, take notes on this video", 0.0, "call"),
    ("Hey Steve, take notes on this lecture", 0.0, "lecture"),
    ("Hey Steve, take notes on this meeting", 0.0, "lecture"),
    ("Hey Steve, take notes on this meeting", 6.0, "call"),
])
async def test_the_call_on_the_screen_is_told_from_the_room(desk_state, monkeypatch, said,
                                                            laptop, kind):
    desk._state["ears"] = object()  # hands-free on, as for a spoken request
    session = _Session(said)
    engine = _desk(monkeypatch, session, laptop=laptop)

    await _handle(engine, _woke())

    assert notes.open_note(desk_state)["kind"] == kind and session.asked == []


@pytest.mark.parametrize("said, kind", [
    # Their words, 7 October 2026, minutes before a Zoom meeting: missed, and
    # the model had no way to start notes.
    ("Hey Steve, I'm going to need you to turn on the meeting transcription", "lecture"),
    ("Hey Steve, transcribe this zoom call", "call"),
    ("Hey Steve, start the meeting notes", "lecture"),
    ("Hey Steve, can you take minutes for this meeting", "lecture"),
    ("Hey Steve, record notes on this Google Meet call", "call"),
])
async def test_notes_are_started_however_the_user_asks(desk_state, monkeypatch, said, kind):
    desk._state["ears"] = object()
    session = _Session(said)
    engine = _desk(monkeypatch, session)

    await _handle(engine, _woke())

    assert notes.open_note(desk_state)["kind"] == kind and session.asked == []


@pytest.mark.parametrize("said", [
    "Hey Steve, did you take notes in the meeting?",
    "Hey Steve, stop transcribing the meeting",
    "Hey Steve, what's on my calendar after the meeting?",
    # 8 October 2026 audit: each of these began recording -- the room, or
    # what the laptop plays -- for up to three or four hours.
    "Hey Steve, remind me to take notes on the lecture",
    "Hey Steve, what's the best way to transcribe a zoom call",
    "Hey Steve, will you take notes on the call tomorrow",
    "Hey Steve, can you transcribe the meeting later",
    "Hey Steve, I'm going to take notes on the lecture myself",
    "Hey Steve, how do I take notes on a zoom call?",
    "Hey Steve, do you take notes on videos?",
    "Hey Steve, should I take notes on this lecture?",
    "Hey Steve, take notes on the meeting at 3 pm",
    "Hey Steve, if I ask you to take notes on the call will you?",
])
@needs_voice
async def test_talk_about_notes_starts_none(desk_state, monkeypatch, said):
    desk._state["ears"] = object()
    session = _Session(said)
    engine = _desk(monkeypatch, session)

    await _handle(engine, _woke())

    assert notes.open_note(desk_state) is None and session.asked, "it went to her assistant"
    assert not engine.notes_on


@pytest.mark.parametrize("said", [
    "Hey Steve, can you take notes on this lecture",
    "Hey Steve, could you start transcribing this zoom call",
    "Hey Steve, please take notes on this meeting",
])
async def test_a_polite_request_for_notes_now_still_starts_them(desk_state, monkeypatch, said):
    """The frames refused above are about notes, not requests for them: "can
    you ..." is how the user asks."""
    desk._state["ears"] = object()
    session = _Session(said)
    engine = _desk(monkeypatch, session)

    await _handle(engine, _woke())

    assert notes.open_note(desk_state) is not None and session.asked == []


async def test_a_dictation_left_idle_saves_itself(desk_state, monkeypatch):
    """8 October 2026 audit: the idle watcher was started for lectures and
    calls and never for a dictation, so a forgotten "take a note" left the
    ear writing everything said in the room into the note until the user said "end
    note"."""
    monkeypatch.setattr(desk, "WATCH_S", 0.01)
    monkeypatch.setattr(notes, "DICTATION_IDLE_S", 0.2)
    engine = _desk(monkeypatch, _Session("Hey Steve, take a note"))

    await _handle(engine, _woke())
    assert notes.open_note(desk_state)["kind"] == "dictation"
    assert engine.notes_on and engine.ear.mode.segments

    for _ in range(300):
        if notes.open_note(desk_state) is None:
            break
        await asyncio.sleep(0.01)
    assert notes.open_note(desk_state) is None, "it is still transcribing the room"
    assert not engine.notes_on and not engine.ear.mode.segments


async def test_a_dictation_with_words_in_it_stays_open(desk_state, monkeypatch):
    """The other side: what the user says counts as not being idle."""
    monkeypatch.setattr(desk, "WATCH_S", 0.01)
    monkeypatch.setattr(notes, "DICTATION_IDLE_S", 0.3)
    engine = _desk(monkeypatch, _Session("Hey Steve, take a note"))
    await _handle(engine, _woke())

    for _ in range(40):
        desk._state["note_heard"] = time.time()  # a segment was written
        await asyncio.sleep(0.01)

    assert notes.open_note(desk_state) is not None and engine.notes_on


@pytest.mark.parametrize("said", [
    "Hey Steve, stop the meeting transcription", "Hey Steve, turn off the notes",
    "Hey Steve, stop taking notes",
])
async def test_notes_are_stopped_however_the_user_asks(desk_state, monkeypatch, said):
    async def summarize(conn, note_id):
        return {"summary": "x", "doc_url": "https://docs/x", "title": "Meeting"}

    monkeypatch.setattr(notes, "summarize", summarize)
    desk.tap(True)
    session = _Session(said)
    engine = _desk(monkeypatch, session)

    await _handle(engine, _woke())

    assert notes.open_note(desk_state) is None and session.asked == []
    assert not engine.notes_on


# ---------------------------------------------------------------------------
# Asked of their assistant (agent/toolset/desk.take_notes)
# ---------------------------------------------------------------------------


def _take_notes():
    from agent.toolset import desk as tools

    found: dict = {}
    tools.register(lambda fn: found.setdefault(fn.__name__, fn))
    return found["take_notes"]


def _ctx(channel, asked=""):
    from types import SimpleNamespace

    return SimpleNamespace(deps=SimpleNamespace(tools_used=[], channel=channel, asked=asked))


@pytest.fixture
def loaded(monkeypatch):
    from agent.worker import links

    async def ensure_voice():
        return runtime.voice

    monkeypatch.setattr(links, "ensure_voice", ensure_voice)


@pytest.fixture
def at_the_desk():
    """A desk turn in the context, as the engine sets one around their
    assistant's answer: where a tool records what it asks of the desk."""
    turn = Turn(id=1, source="wake", started=0.0)
    token = runtime.desk_turn.set(turn)
    yield turn
    runtime.desk_turn.reset(token)


async def test_their_assistant_can_take_notes_on_a_call_from_their_phone(desk_state, loaded):
    said = await _take_notes()(_ctx("signal"), source="laptop", title="Consulting club CTM")

    assert said["ok"] and desk._state["tap"].started
    assert notes.open_note(desk_state)["title"] == "Consulting club CTM"


async def test_notes_of_the_room_need_the_microphone_on(desk_state, loaded, at_the_desk):
    said = await _take_notes()(_ctx("voice", "take notes on the room"), source="room")

    assert not said["ok"] and "hands-free" in said["error"]
    assert notes.open_note(desk_state) is None and "notes" not in at_the_desk.asks


@pytest.mark.parametrize("channel", ["signal", "telegram", "tray", "mailbox", "reminder"])
async def test_notes_of_the_room_are_refused_from_anywhere_but_their_desk(
        desk_state, loaded, at_the_desk, monkeypatch, channel):
    """8 October 2026 audit, finding 10: `take_notes` is a free tool offered
    on every channel the user is present on, so a Signal turn that had read a mail
    could be told to open their microphone on the room."""
    engine = _desk(monkeypatch, _Session())
    desk._state["ears"] = object()
    desk._state["listener"] = engine.ear

    said = await _take_notes()(_ctx(channel, "take notes of the room"), source="room")

    assert not said["ok"] and "aloud at their desk" in said["error"]
    assert "notes" not in at_the_desk.asks and notes.open_note(desk_state) is None
    assert not engine.notes_on


async def test_notes_of_the_room_need_user_words_to_ask_for_them(desk_state, loaded, at_the_desk,
                                                                monkeypatch):
    """At the desk, but the user asked what's on tomorrow, and something the
    assistant read says to start notes."""
    engine = _desk(monkeypatch, _Session())
    desk._state["ears"] = object()
    desk._state["listener"] = engine.ear

    said = await _take_notes()(_ctx("voice", "what's on my calendar tomorrow"), source="room")

    assert not said["ok"] and "notes" not in at_the_desk.asks and not engine.notes_on


async def test_notes_of_the_room_need_a_desk_turn_to_ask_of(desk_state, loaded, monkeypatch):
    """The ask is written on the turn the desk is running. Called with none --
    a tool run outside any desk turn -- there is nothing to start them after,
    and nothing is started."""
    engine = _desk(monkeypatch, _Session())
    desk._state["ears"] = object()
    desk._state["listener"] = engine.ear
    assert runtime.desk_turn.get() is None

    said = await _take_notes()(_ctx("voice", "take notes of the room"), source="room")

    assert not said["ok"] and notes.open_note(desk_state) is None and not engine.notes_on


async def test_notes_of_what_the_laptop_plays_stay_free_from_their_phone(desk_state, loaded):
    said = await _take_notes()(_ctx("signal", "take notes on my zoom"), source="laptop")

    assert said["ok"] and notes.open_note(desk_state)["kind"] == "call"


@needs_voice
async def test_notes_of_the_room_asked_aloud_start_once_its_reply_is_said(desk_state, loaded,
                                                                          monkeypatch):
    """What the speakers say, the microphone hears: the reply first. What the
    tool asked is on the turn, and nothing of it outlives the turn."""
    asked_of = []

    class Asks(_Session):
        async def respond(self, transcript, *, released, text=None, timing=None, out=None):
            asked_of.append(runtime.desk_turn.get())
            said = await _take_notes()(_ctx("voice", text), source="room", title="CTM")
            assert said["ok"] and notes.open_note(desk_state) is None, "not yet: it is talking"
            assert not engine.notes_on
            return await super().respond(transcript, released=released, text=text)

    engine = _desk(monkeypatch, Asks("Hey Steve, could you write down what we say"))
    desk._state["ears"] = object()
    desk._state["listener"] = engine.ear

    await _handle(engine, _woke())

    assert notes.open_note(desk_state)["title"] == "CTM"
    # Straight to the note, with no follow-up window: one would hand the
    # room's next sentence to their assistant (found 9 October 2026).
    assert engine.notes_on and engine.mode == "idle"
    assert engine.ear.mode.segments and not engine.ear.mode.follow_until, \
        "the room is not being written down"
    assert len(asked_of) == 1 and asked_of[0].asks == {"notes": "CTM"}
    assert runtime.desk_turn.get() is None


async def test_notes_a_hotkey_turn_asked_for_start_when_it_is_over_and_no_later(desk_state,
                                                                                loaded,
                                                                                monkeypatch):
    """The reply promised "notes of the room start as soon as you have
    finished speaking"; nobody was reading that flag after a hotkey turn, so
    they started on the next wake, if at all. A push-to-talk turn runs through
    the engine (`Engine.hotkey_turn`), and what it asked is on its turn."""
    engine = _desk(monkeypatch, _Session())
    desk._state["ears"] = object()
    desk._state["listener"] = engine.ear
    asked_in = []

    async def run(out):
        asked_in.append(runtime.desk_turn.get())
        said = await _take_notes()(_ctx("voice", "take notes of the room"), source="room",
                                   title="Meeting, Oct 8")
        assert said["ok"] and notes.open_note(desk_state) is None, "not before it is over"
        return "her reply"

    result = await engine.hotkey_turn(run)

    assert result == "her reply" and asked_in[0].source == "hotkey"
    assert notes.open_note(desk_state)["title"] == "Meeting, Oct 8" and engine.notes_on
    assert runtime.desk_turn.get() is None


async def test_stopping_by_asking_writes_them_up_and_sends_them_the_link(desk_state, loaded,
                                                                         monkeypatch):
    from agent.worker import links

    told = []

    async def notify(text, **kwargs):
        told.append(text)
        return True

    async def summarize(conn, note_id):
        return {"summary": "x", "doc_url": "https://docs/x", "title": "Consulting club CTM"}

    monkeypatch.setattr(links, "notify_user", notify)
    monkeypatch.setattr(notes, "summarize", summarize)
    desk._state["ears"] = object()  # hands-free on: it used to tell them only when off
    await _take_notes()(_ctx("signal"), source="laptop")

    said = await _take_notes()(_ctx("signal"), on=False)

    assert said["ok"] and said["stopped"] == "call"
    for _ in range(100):
        if told:
            break
        await asyncio.sleep(0.01)
    assert told and "Consulting club CTM" in told[0]


class _LiveCall:
    """A live conversation, as far as the engine needs one to end it: it
    records that it was asked to close, and why."""

    live = object()
    closing = False
    ended_at = None
    notes_after = None

    def __init__(self):
        self.closed = []

    def feed(self, block):
        pass

    async def close(self, reason):
        self.closing = True
        self.closed.append(reason)


@needs_voice
async def test_notes_asked_for_in_a_live_conversation_end_it_first(desk_state, loaded,
                                                                  monkeypatch):
    """Its own voice would be written into them. Their assistant, answering
    what a live conversation handed over, starts notes of the call: the desk
    schedules the conversation's end (`Engine.end_live_soon`) and the notes
    are begun at once."""
    from agent.worker import answering
    from voice import engine as engine_mod

    engine = _desk(monkeypatch, _Session())
    call = engine.call = _LiveCall()

    async def voice_reply(text, channel):
        await _take_notes()(_ctx("voice"), source="laptop")
        return "Taking notes on the call.", None

    monkeypatch.setattr(answering, "voice_reply", voice_reply)
    monkeypatch.setattr(engine_mod, "NOTES_AFTER_LIVE_S", 0.01)
    ask = engine.shell.live_call().ask

    reply = await ask("start transcribing this zoom", heard_at=time.monotonic())
    await engine.settle()

    assert reply == "Taking notes on the call." and call.closed == ["notes"]
    assert notes.open_note(desk_state)["kind"] == "call"


@needs_voice
async def test_room_notes_asked_for_in_a_live_conversation_start_after_it_has_ended(
        desk_state, loaded, monkeypatch):
    """The same for notes of the room, which a live conversation's hand-over
    may ask for too: the turn it runs is its own (source "live"), the ask is
    on it, and the desk ends the conversation and keeps the title for when it
    has. They are not started under its voice."""
    from agent.worker import answering
    from voice import engine as engine_mod

    engine = _desk(monkeypatch, _Session())
    call = engine.call = _LiveCall()
    desk._state["ears"] = object()
    desk._state["listener"] = engine.ear
    turns = []

    async def voice_reply(text, channel):
        turns.append(runtime.desk_turn.get())
        said = await _take_notes()(_ctx("voice", text), source="room", title="CTM")
        assert said["ok"], said
        return "Taking notes of the room.", None

    monkeypatch.setattr(answering, "voice_reply", voice_reply)
    monkeypatch.setattr(engine_mod, "NOTES_AFTER_LIVE_S", 0.01)
    ask = engine.shell.live_call().ask

    reply = await ask("take notes on the room", heard_at=time.monotonic())
    await engine.settle()

    assert reply == "Taking notes of the room." and call.closed == ["notes"]
    assert len(turns) == 1 and turns[0].source == "live" and turns[0].asks == {"notes": "CTM"}
    assert call.notes_after == "CTM", "the title was not kept for when the call has ended"
    assert notes.open_note(desk_state) is None, "notes began under the conversation's own voice"
    assert runtime.desk_turn.get() is None


def test_notes_are_offered_in_their_conversations_and_never_to_unwatched_work():
    import asyncio as aio

    from pydantic_ai.tools import ToolDefinition

    from agent import toolset
    from agent.turn import Deps

    shown = [ToolDefinition(name="take_notes"), ToolDefinition(name="web_search")]

    def names(channel):
        ctx = type("C", (), {"deps": Deps(conn=None, session_id=1, channel=channel)})()
        return {t.name for t in aio.run(toolset.offered(ctx, list(shown)))}

    assert "take_notes" in names("signal") and "take_notes" in names("voice")
    for unwatched in ("initiative", "job", "routine", "night"):
        assert "take_notes" not in names(unwatched), unwatched


def test_the_desk_speaks_in_the_voice_the_user_chose():
    from voice import tts, tts_server

    assert tts.VOICE == tts_server.DEFAULT_VOICE == "am_echo"
    assert tts.Speaker().voice == "am_echo"


async def test_a_worker_giving_up_its_duties_stops_the_tap(desk_state, monkeypatch):
    """The worker taking over picks the call up (`resume_tap`); two taps
    would write every line twice."""
    from agent.worker import duties

    desk.tap(True)
    tapped = desk._state["tap"]
    monkeypatch.setitem(duties._duties, "state", "active")
    for name in ("jobs", "signal", "voice"):
        monkeypatch.setattr(runtime, name, None)
    await duties.give_up_duties("retired")
    assert tapped.stopped and desk._state["tap"] is None


# --------------------------------------------------------------------------
# Without the voice add-on (v0.1 ships without it)
# --------------------------------------------------------------------------


@pytest.fixture
def no_add_on(desk_state, monkeypatch):
    """An install with only core's extras: the real tap, whose detector
    can't be imported."""
    monkeypatch.setattr(listen, "Tap", _REAL_TAP)
    for name in ("faster_whisper", "onnxruntime", "soundcard"):
        monkeypatch.setitem(sys.modules, name, None)
    return desk_state


async def test_without_the_voice_add_on_a_call_is_refused_and_no_note_is_left_open(no_add_on):
    """Settings offers call notes on every install. On one without the add-on
    the note was written, then the tap failed on its import: the note read
    "still listening" for good (the review of steps 7 to 9, finding 15)."""
    said = desk.tap(True, title="Call, test")

    assert said == {"ok": False, "error": desk.NO_ADD_ON}
    assert notes.open_note(no_add_on) is None and desk._state["tap"] is None


async def test_a_call_left_open_without_the_add_on_is_closed_not_retried_at_each_start(
        no_add_on):
    """A call open when the add-on went away was retried by each worker that
    took up the desk, and failed each time, with the note still listening."""
    note_id = notes.start(no_add_on, "call", "Call")

    desk.resume_tap()

    assert notes.open_note(no_add_on) is None and desk._state["tap"] is None
    assert no_add_on.execute("SELECT status FROM notes WHERE id = ?",
                             (note_id,)).fetchone()[0] == "done"


def test_the_desk_says_whether_the_voice_add_on_that_call_notes_need_is_installed(monkeypatch):
    """Without the add-on the window offered the call-notes switch, and
    pressing it brought only a refusal. The desk says whether every package
    the notes need is there, so the window offers the switch only where it
    works."""
    monkeypatch.setattr(desk, "TAP_NEEDS", ("json", "sqlite3"))
    assert desk.tap_status()["add_on"] is True
    monkeypatch.setattr(desk, "TAP_NEEDS", ("json", "steve_no_such_package"))
    assert desk.tap_status()["add_on"] is False
