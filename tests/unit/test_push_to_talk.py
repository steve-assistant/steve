"""Push-to-talk, core's voice: hold the key, talk, let go, hear the answer --
on OpenAI, with no local model.

Before step 7 the desk heard with Whisper and spoke with Kokoro, both on the
laptop; a product laptop has no graphics chip for them, and on its CPU
Whisper took 8.9 s to hear a short request. Now the clip goes to OpenAI on
release (core/hearing.py) and the reply is said by OpenAI's voice
(core/voicing.py), through `voice/cloud.py`. What the user must be able to
count on:
- a press opens the microphone, and a tick says so once it is open;
- what was said is shown on release, before the reply, and billed;
- a press during a reply silences it at once;
- an empty press sends nothing, and costs nothing;
- a release before the session is up still sends what was heard;
- no local model is loaded on the way;
- every turn's timings are logged, never its words.

OpenAI is faked at the two calls the fences refuse; the microphone and the
speakers are fakes too. Nothing here opens either.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time

import numpy as np
import pytest

from core import hearing, spend, voicing
from voice import audio, cloud, pipeline
from voice.speaking import PREAMBLE_LINES
from voice.tts import TTSUnavailable

WORDS = "what's on my calendar tomorrow"
REPLY = "You have two meetings tomorrow. The first is at nine."


class _Recorder:
    """The microphone: a second of speech, heard 20 ms after the press."""

    def __init__(self, audio_s: float = 1.0, loud: bool = True) -> None:
        self.events: list[tuple[str, float]] = []
        self.recording = False
        self.audio_s, self.loud = audio_s, loud

    def start(self, pressed: float | None = None) -> None:
        self.events.append(("start", time.perf_counter()))
        self.recording = True

    def stop(self) -> audio.Recording:
        self.recording = False
        n = int(self.audio_s * audio.STT_SAMPLE_RATE)
        level = 0.3 if self.loud else 0.001
        samples = (level * np.sin(np.arange(n) / 8)).astype(np.float32)
        return audio.Recording(audio=samples, live_ms=20)


class _Player:
    """The speakers: what was queued, in order, and when it was silenced."""

    device_rate = 24_000

    def __init__(self) -> None:
        self.queued: list[tuple[str, float]] = []
        self.stopped: list[float] = []
        self.queued_seconds = 0.0
        self.is_playing = False

    def take(self) -> list:
        return []

    def enqueue(self, samples, rate, owner=None) -> None:
        kind = "tick" if samples is audio.TICK[0] else "speech"
        self.queued.append((kind, len(samples) / rate))
        self.queued_seconds += len(samples) / rate
        self.is_playing = True

    def stop(self) -> bool:
        playing = self.is_playing
        self.stopped.append(time.perf_counter())
        self.queued_seconds, self.is_playing = 0.0, False
        return playing

    def wait(self, timeout=None) -> bool:
        self.is_playing = False
        return True

    def close(self) -> None:
        pass


class _OpenAI:
    """OpenAI's two speech calls, as the fences leave them to be faked."""

    def __init__(self, monkeypatch, *, heard: str = WORDS) -> None:
        self.heard: list[tuple[str, bytes]] = []
        self.said: list[str] = []
        self.order: list[str] = []
        self.units: list[str | None] = []  # the unit each call was billed to
        self.text = heard
        monkeypatch.setattr(hearing, "_call", self._hear)
        monkeypatch.setattr(voicing, "_call", self._say)

    def _hear(self, name: str, data: bytes, key: str) -> dict:
        self.heard.append((name, data))
        self.order.append("heard")
        self.units.append(getattr(spend.current(), "category", None))
        return {"text": self.text, "usage": {"type": "duration", "seconds": 1.0}}

    def _say(self, text: str, voice: str, speed: float, key: str) -> bytes:
        self.said.append(text)
        self.units.append(getattr(spend.current(), "category", None))
        return bytes(int(0.5 * voicing.BYTES_PER_SECOND))


def _session(tmp_path, order: list[str] | None = None, *, recorder=None):
    async def reply(text: str):
        if order is not None:
            order.append("replied")
        return REPLY, {}

    session = pipeline.VoiceSession(
        reply, speaker=cloud.CloudSpeaker(kept=(*pipeline.STALLS, *PREAMBLE_LINES),
                                          lines_dir=tmp_path / "lines"))
    session.recorder = recorder or _Recorder()
    session.player = _Player()
    return session


async def _turn(session, **kwargs):
    session.start_listening()
    return await session.finish_turn(**kwargs)


# ---------------------------------------------------------------------------
# Press, talk, release, answer
# ---------------------------------------------------------------------------

async def test_a_press_talk_and_release_is_heard_answered_and_said_on_openai(
        tmp_path, conn, monkeypatch):
    """The whole turn on core: the words from OpenAI's speech-to-text, the
    reply in OpenAI's voice, each billed as chat."""
    openai = _OpenAI(monkeypatch)
    session = _session(tmp_path)
    session.warm()
    openai.said.clear()

    with spend.spending("chat", conn=conn):
        turn = await _turn(session)

    assert turn.transcript.text == WORDS and turn.reply_text == REPLY
    [(name, wav)] = openai.heard
    assert name == "clip.wav" and wav[:4] == b"RIFF"
    assert " ".join(openai.said) == REPLY
    rows = conn.execute("SELECT category, model FROM usage_events ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("chat", "gpt-transcribe")] + [
        ("chat", "gpt-4o-mini-tts")] * len(openai.said)


async def test_the_microphone_opens_on_the_press_and_the_tick_comes_once_it_is_open(
        tmp_path, monkeypatch):
    """A tick before the microphone is open would tell the user to talk
    into nothing; none at all, and a word begun at the press may be lost
    without their knowing."""
    _OpenAI(monkeypatch)
    session = _session(tmp_path)
    recorder, player = session.recorder, session.player

    live_at_tick: list[bool] = []
    enqueue = player.enqueue

    def enqueue_noting(samples, rate, owner=None):
        live_at_tick.append(recorder.recording)
        enqueue(samples, rate, owner)

    player.enqueue = enqueue_noting
    session.start_listening()

    assert [event for event, _ in recorder.events] == ["start"]
    assert player.queued == [("tick", pytest.approx(0.06))]
    assert live_at_tick == [True]


async def test_the_words_are_shown_on_release_before_the_reply_is_written(
        tmp_path, monkeypatch):
    """Shown only with the reply, a slow answer looked like one unheard."""
    openai = _OpenAI(monkeypatch)
    order = openai.order
    session = _session(tmp_path, order)
    shown: list[str] = []

    async def on_heard(text: str) -> None:
        shown.append(text)
        order.append("shown")

    await _turn(session, on_heard=on_heard)

    assert shown == [WORDS]
    assert order == ["heard", "shown", "replied"]


async def test_the_turns_timings_are_measured_from_the_press_and_the_release(
        tmp_path, monkeypatch):
    _OpenAI(monkeypatch)
    session = _session(tmp_path)
    session.warm()

    turn = await _turn(session)

    timing = turn.timing.as_dict()
    assert timing["live_ms"] == 20
    assert timing["captured_s"] == 1.0
    assert timing["first_audio_ms"] >= timing["stt_ms"] >= 0
    assert timing["total_ms"] >= timing["first_audio_ms"]


# ---------------------------------------------------------------------------
# Barge-in, and the press that says nothing
# ---------------------------------------------------------------------------

async def test_a_press_during_a_reply_silences_it_at_once(tmp_path, monkeypatch):
    """Section 1's bar: a press during a reply to silence in at most 150 ms.
    The speakers are stopped before the microphone is even asked to open."""
    _OpenAI(monkeypatch)
    session = _session(tmp_path)
    session.player.enqueue(np.zeros(24_000 * 4, np.float32), 24_000)

    pressed = time.perf_counter()
    session.start_listening()

    silenced = session.player.stopped[0]
    assert (silenced - pressed) * 1000 <= 150
    assert silenced <= session.recorder.events[0][1]
    assert session._unplayed == pytest.approx(4.0)


async def test_an_empty_press_sends_nothing_and_costs_nothing(tmp_path, conn, monkeypatch):
    """A tap with nothing said: sent, OpenAI would be paid to hear a breath,
    and might hear "Thank you." in it, which the agent would answer."""
    openai = _OpenAI(monkeypatch)
    session = _session(tmp_path, recorder=_Recorder(loud=False))

    with spend.spending("chat", conn=conn):
        turn = await _turn(session)

    assert not turn.transcript.ok and turn.reply_text == ""
    assert openai.heard == []
    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0


async def test_a_press_too_short_to_hold_a_word_sends_nothing(tmp_path, monkeypatch):
    openai = _OpenAI(monkeypatch)
    session = _session(tmp_path, recorder=_Recorder(audio_s=0.2))

    turn = await _turn(session)

    assert turn.transcript.rejected == "too short or silent" and openai.heard == []


async def test_what_openai_cannot_hear_is_said_with_its_reason_not_answered(
        tmp_path, monkeypatch):
    """No guess is answered: the app shows why, and asks nothing of the agent."""
    order: list[str] = []
    session = _session(tmp_path, order)

    def unreachable(name, data, key):
        raise hearing.HearingUnavailable("OpenAI could not be reached")

    monkeypatch.setattr(hearing, "_heard", lambda *a: unreachable(*a[:3]))

    turn = await _turn(session)

    assert turn.transcript.rejected == "Couldn't hear it: OpenAI could not be reached"
    assert order == []


# ---------------------------------------------------------------------------
# The release before the session is up
# ---------------------------------------------------------------------------

def test_a_release_before_the_session_is_up_still_sends_what_was_heard(monkeypatch):
    """The first press of a worker's life finds no session: the recording
    starts on the press, before it is made, and that recording is what is
    heard once it is up -- even when the key is let go first."""
    from types import SimpleNamespace

    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.testclient import TestClient

    from agent import runtime
    from agent.worker import desk, links, routes

    heard: list[object] = []
    up = asyncio.Event()

    class _Early(_Recorder):
        def stop(self):
            return "what was said before the session was up"

    class _Session:
        player = _Player()

        async def finish_turn(self, recording=None, *, out=None, on_heard=None):
            heard.append(recording)
            await on_heard("what time is it")
            transcript = SimpleNamespace(ok=True, rejected=None)
            return SimpleNamespace(transcript=transcript, error=None, as_dict=lambda: {})

    async def _load():
        # Still loading when the key is let go: the stop waits for it.
        up.set()
        await asyncio.sleep(0.05)
        runtime.voice = _Session()
        return runtime.voice

    monkeypatch.setattr(audio, "Recorder", _Early)
    monkeypatch.setattr(links, "ensure_voice", _load)
    monkeypatch.setattr(runtime, "voice", None)
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setitem(desk._state, "engine", None)
    monkeypatch.setitem(desk._state, "ticker", None)
    app = Starlette(routes=[WebSocketRoute("/ws", routes.ws_session)])

    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})
        assert ws.receive_json()["type"] == "listening"
        ws.send_json({"type": "voice_stop"})
        shown = ws.receive_json()
        done = ws.receive_json()

    assert heard == ["what was said before the session was up"]
    assert shown == {"type": "voice_heard", "heard": "what time is it"}
    assert done["type"] == "voice_turn"


def test_the_microphones_first_sample_is_timed_from_the_press(monkeypatch):
    """The press-to-live bar is measured, not assumed: from `start` to the
    first sample of the first block the microphone sends, and that block is
    kept."""
    streams: list[object] = []

    class _Stream:
        def __init__(self, *, callback, blocksize, **kwargs):
            self.callback, self.blocksize = callback, blocksize
            streams.append(self)

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(audio.sd, "InputStream", _Stream)
    monkeypatch.setattr(audio.sd, "query_devices", lambda device: {"default_samplerate": 16_000})
    recorder = audio.Recorder(device=0)

    recorder.start()
    assert recorder.live_ms is None
    [stream] = streams
    block = np.full((stream.blocksize, 1), 0.2, np.float32)
    stream.callback(block, stream.blocksize, None, None)
    recording = recorder.stop()

    assert recording.duration_s == pytest.approx(stream.blocksize / 16_000)
    assert recording.live_ms is not None and recording.live_ms <= 150


# ---------------------------------------------------------------------------
# No local model, and the voice's own lines
# ---------------------------------------------------------------------------

async def test_no_local_model_is_loaded_for_a_turn(tmp_path, monkeypatch):
    """Core holds no local model: a session built without the add-on's must
    hear and speak through OpenAI, and never reach for Whisper or Kokoro."""
    for name in ("faster_whisper", "ctranslate2", "kokoro_onnx", "onnxruntime"):
        monkeypatch.setitem(sys.modules, name, None)  # any import of it raises
    _OpenAI(monkeypatch)

    async def reply(text):
        return REPLY, {}

    session = pipeline.VoiceSession(reply)
    session.recorder, session.player = _Recorder(), _Player()
    report = session.warm()
    turn = await _turn(session)

    assert isinstance(session.transcriber, cloud.CloudTranscriber)
    assert isinstance(session.speaker, cloud.CloudSpeaker)
    assert report["stt"]["device"] == report["tts"]["device"] == "openai"
    assert turn.reply_text == REPLY and turn.timing.spoken_s > 0


async def test_the_stalls_are_fetched_once_and_kept(tmp_path, monkeypatch):
    """Fetched at every warm-up, each worker start would pay for a dozen
    lines and wait on them."""
    openai = _OpenAI(monkeypatch)

    _session(tmp_path).warm()
    first = len(openai.said)
    _session(tmp_path).warm()

    assert first == len({*pipeline.STALLS, *PREAMBLE_LINES})  # "Let me check." is both
    assert len(openai.said) == first


async def test_a_line_that_cannot_be_made_at_warm_up_leaves_the_voice_on(
        tmp_path, monkeypatch):
    """OpenAI not answering once must not leave the desk mute until the
    worker restarts: the replies are still said, without the stalls."""
    openai = _OpenAI(monkeypatch)

    def unreachable(text, voice, speed, key):
        raise ConnectionError("down")

    monkeypatch.setattr(voicing, "_call", unreachable)
    session = _session(tmp_path)
    session.warm()
    monkeypatch.setattr(voicing, "_call", openai._say)

    turn = await _turn(session)

    assert session.speak and session._stalls == []
    assert turn.timing.spoken_s > 0


async def test_without_a_key_the_reply_is_shown_not_said(tmp_path, monkeypatch):
    openai = _OpenAI(monkeypatch)
    monkeypatch.setattr("core.secrets.get", lambda name, default=None: None)
    session = _session(tmp_path)

    report = session.warm()

    assert not session.speak and report["tts"]["error"] == "no OpenAI key is stored"
    assert openai.said == []


async def test_a_line_said_while_the_voice_cannot_answer_goes_unsaid_and_raises_nothing(
        tmp_path, monkeypatch):
    """`say` speaks the desk's own lines and the alerts. A reply's sentences
    go on without the voice; a line raised, into the desk's turn."""
    _OpenAI(monkeypatch)
    session = _session(tmp_path)
    session.warm()

    def outage(*args, **kwargs):
        raise voicing.VoicingUnavailable("OpenAI answered 500")

    monkeypatch.setattr(voicing, "pcm", outage)

    assert await session.say("Taking notes on this call.") == 0.0
    assert [kind for kind, _ in session.player.queued] == []


def test_a_voice_that_cannot_answer_mid_reply_is_unavailable_not_a_crash(tmp_path,
                                                                         monkeypatch):
    def outage(text, voice, speed, key):
        raise voicing.VoicingUnavailable("OpenAI answered 500")

    monkeypatch.setattr(voicing, "pcm", lambda *a, **k: outage(*a, None, None, None))
    speaker = cloud.CloudSpeaker(lines_dir=tmp_path)
    speaker.start()

    with pytest.raises(TTSUnavailable, match="OpenAI answered 500"):
        speaker.say("Done.")


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

async def test_each_turn_is_logged_with_its_timings_and_never_its_words(
        tmp_path, monkeypatch, caplog):
    """Session E is scored from these lines (devtools/voicebench): without
    them a real desk's waits could not be measured; with the words in them,
    the log would keep what the user said."""
    from agent.worker import desk

    _OpenAI(monkeypatch)
    session = _session(tmp_path)
    session.warm()
    turn = await _turn(session)

    with caplog.at_level(logging.INFO, logger="desk"):
        desk.log_hotkey_turn(turn)

    [line] = [r.getMessage() for r in caplog.records if "push-to-talk turn" in r.getMessage()]
    assert "outcome=answered" in line and "live_ms=20" in line
    for field in ("held_s=1.00", "stt_ms=", "first_token_ms=", "first_audio_ms=", "total_ms="):
        assert field in line
    for words in (*WORDS.split(), "meetings", "nine"):
        assert words not in line


async def test_a_turn_outside_any_unit_of_work_is_billed_as_chat(tmp_path, monkeypatch):
    """The desk's route opens no unit for a turn: billed to no category, or
    to the background's, a voice request would escape chat's cap."""
    openai = _OpenAI(monkeypatch)
    session = _session(tmp_path)

    turn = await _turn(session)

    assert turn.reply_text == REPLY
    assert openai.units and set(openai.units) == {"chat"}


def test_the_ear_push_to_talk_and_call_notes_share_hears_nothing_past_the_cap(
        conn, monkeypatch):
    """Push-to-talk and notes on a call hear through one ear,
    `voice/cloud.CloudTranscriber`. Past the cap it asks OpenAI nothing and
    says why where the words would have been, so neither door spends past
    it: notes on a call run for an hour, a stretch at a time."""
    openai = _OpenAI(monkeypatch)
    spend.bill(conn, 200.0, model="m", unit=spend.Unit("chat"))

    with spend.spending("chat", conn=conn):
        heard = cloud.CloudTranscriber().transcribe(np.zeros(16_000, dtype=np.float32))

    assert openai.heard == []
    assert heard.text == ""
    assert str(heard.rejected).startswith("Couldn't hear it: a spending cap is reached"), heard


# ---------------------------------------------------------------------------
# The app's socket: the microphone, the tick and a press during a reply
# ---------------------------------------------------------------------------

def _socket(monkeypatch, session):
    """The worker's socket route, with `session` as the warm session (None:
    none yet) and no desk engine."""
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.testclient import TestClient

    from agent import runtime
    from agent.worker import desk, routes

    monkeypatch.setattr(runtime, "voice", session)
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setattr(runtime, "turning", set())
    monkeypatch.setitem(desk._state, "engine", None)
    monkeypatch.setitem(desk._state, "ticker", None)
    return TestClient(Starlette(routes=[WebSocketRoute("/ws", routes.ws_session)]))


def test_the_first_press_after_a_start_ticks_and_a_second_press_keeps_one_microphone(
        monkeypatch):
    """The first press of a worker's life finds no session, so it played no
    tick: the first press of every day was the one a user could not tell had
    been heard. And a second press before the release (the button and the
    key) replaced the first recording without stopping it, leaving its
    microphone open for good."""
    from agent.worker import links

    made: list[_Recorder] = []
    ticks: list[int] = []

    class _Counted(_Recorder):
        def __init__(self):
            super().__init__()
            made.append(self)

    async def _never():
        await asyncio.sleep(3600)

    monkeypatch.setattr(audio, "Recorder", _Counted)
    monkeypatch.setattr(audio, "tick_alone", lambda: ticks.append(1))
    monkeypatch.setattr(links, "ensure_voice", _never)

    with _socket(monkeypatch, None).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})
        assert ws.receive_json()["type"] == "listening"
        ws.send_json({"type": "voice_start"})
        assert ws.receive_json()["type"] == "listening"

    assert ticks == [1], "the first press played no tick"
    assert len(made) == 1, "a second press opened a second microphone"
    assert not made[0].recording, "the socket closed and the microphone stayed open"


def test_a_socket_gone_mid_press_closes_the_microphone_and_sends_nothing(monkeypatch):
    """The app's socket can drop while the key is held (a reload, a swap).
    Only a cold press's recorder was let go: a warm session's went on
    recording, and the next release sent minutes of the room to be
    transcribed and billed."""
    openai = _OpenAI(monkeypatch)

    async def reply(text):
        return REPLY, {}

    session = pipeline.VoiceSession(reply)
    session.recorder, session.player = _Recorder(), _Player()

    with _socket(monkeypatch, session).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})
        assert ws.receive_json()["type"] == "listening"
        assert session.recorder.recording

    assert not session.recorder.recording, "the microphone stayed open"
    assert session.status()["listening"] is False
    assert openai.heard == []


def test_a_press_during_a_reply_is_read_while_the_reply_plays(monkeypatch):
    """The barge-in, through the app's socket. The release's turn was
    awaited in the socket's loop until its reply had played, so a press
    during the reply was read only once it was over -- and then, with its
    release queued behind it, was a turn too short to hear. Here the reply
    plays until it is cut, as a real one does: the press must cut it."""
    from types import SimpleNamespace

    cut = asyncio.Event()

    class _Session:
        player = _Player()
        presses = turns = 0

        def start_listening(self, pressed=None):
            self.presses += 1
            if self.presses > 1:
                cut.set()

        def cancel_listening(self):
            pass

        def stop_listening(self):
            return None

        def interrupt(self):  # the desk's engine, stopping what it says
            return False

        async def finish_turn(self, recording=None, *, out=None, on_heard=None):
            self.turns += 1
            transcript = SimpleNamespace(ok=True, rejected=None)
            if self.turns == 1:
                with contextlib.suppress(TimeoutError):  # the reply, playing until cut
                    await asyncio.wait_for(cut.wait(), 2.0)
            done = {"turn": self.turns, "cut": cut.is_set()}
            return SimpleNamespace(transcript=transcript, error=None, as_dict=lambda: done)

    with _socket(monkeypatch, _Session()).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})
        assert ws.receive_json()["type"] == "listening"
        ws.send_json({"type": "voice_stop"})
        ws.send_json({"type": "voice_start"})  # while the reply plays
        said = [ws.receive_json(), ws.receive_json()]

    assert said[0]["type"] == "listening", "the press waited for the reply to end"
    assert said[1] == {"type": "voice_turn", "turn": 1, "cut": True}


async def test_the_tick_heard_back_in_the_microphone_is_not_a_request(tmp_path, monkeypatch):
    """The tick plays as the microphone opens, and a laptop's speakers sit
    beside it. Heard back, it lifted an empty press over the silence bar,
    and the press was sent to be transcribed and billed."""
    openai = _OpenAI(monkeypatch)
    session = _session(tmp_path)
    clip = np.zeros(audio.STT_SAMPLE_RATE, np.float32)
    clip[:int(0.06 * audio.STT_SAMPLE_RATE)] = 0.05  # the tick, at its own level

    session.start_listening()
    turn = await session.finish_turn(audio.Recording(clip))

    assert turn.transcript.rejected == "too short or silent"
    assert openai.heard == []


async def test_a_voice_that_fails_mid_reply_is_tried_once_and_logged(tmp_path, monkeypatch,
                                                                     caplog):
    """In an outage each sentence waited out its own timeout, silently, and
    the reply's text reached the app only after the last: minutes of
    nothing. Once failed, the rest of the turn is shown, not tried again."""
    openai = _OpenAI(monkeypatch)
    pieces = [f"This is sentence number {n} of a reply that comes in pieces. "
              for n in range(1, 4)]
    long_reply = "".join(pieces).strip()

    async def reply(text, *, on_text=None, on_tool=None, on_restart=None):
        for piece in pieces:  # streamed, as the model writes it
            on_text(piece)
            await asyncio.sleep(0.05)
        return long_reply, {}

    session = pipeline.VoiceSession(
        reply, streams=True, speaker=cloud.CloudSpeaker(kept=(*pipeline.STALLS, *PREAMBLE_LINES),
                                          lines_dir=tmp_path / "lines"))
    session.recorder, session.player = _Recorder(), _Player()
    session.warm()  # its own lines made while OpenAI answers
    tries: list[str] = []

    def outage(text, voice, speed, key):
        tries.append(text)
        raise voicing.VoicingUnavailable("OpenAI answered 500")

    monkeypatch.setattr(voicing, "_call", outage)
    with caplog.at_level(logging.WARNING):
        turn = await _turn(session)

    assert turn.reply_text == long_reply and openai.heard
    assert len(tries) == 1, f"the voice was tried {len(tries)} times in one turn"
    assert "the voice failed" in caplog.text


class _Timed(_Recorder):
    """The microphone, with when it closed as well as when it opened."""

    def stop(self) -> audio.Recording:
        self.events.append(("stop", time.perf_counter()))
        return super().stop()


def test_a_press_while_the_last_reply_is_still_being_written_closes_the_mic_on_release(
        monkeypatch):
    """A press while the model is still writing the last answer (a lookup),
    then a release. The release's turn waits for the one before it, and the
    microphone was closed by the turn: it stayed open until the lookup
    ended, and what it heard went to OpenAI with the turn. The microphone
    is the user's to open and close; it closes on the release."""
    _OpenAI(monkeypatch)

    async def lookup(text):
        await asyncio.sleep(1.5)
        return REPLY, {}

    session = pipeline.VoiceSession(lookup, speak=False)
    recorder = _Timed()
    session.recorder, session.player = recorder, _Player()

    with _socket(monkeypatch, session).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})
        assert ws.receive_json()["type"] == "listening"
        ws.send_json({"type": "voice_stop"})  # the first turn: the lookup starts
        assert ws.receive_json()["type"] == "voice_heard"
        ws.send_json({"type": "voice_start"})  # pressed while it works
        assert ws.receive_json()["type"] == "listening"
        time.sleep(0.2)
        ws.send_json({"type": "voice_stop"})
        time.sleep(0.4)
        open_after_release = recorder.recording
        ws.receive_json()
        ws.receive_json()

    assert not open_after_release, "the microphone stayed open after the release"


def test_a_second_press_after_a_cold_load_leaves_no_microphone_open(monkeypatch):
    """The button and the key both pressed, the models loaded between the
    two: the first press took the cold path, the second the warm one, which
    opened the session's microphone too. The release handed on the first
    recording and never closed the second, and the next press sent what it
    had heard of the room since."""
    from agent import runtime
    from agent.worker import links

    _OpenAI(monkeypatch)
    made: list[_Recorder] = []

    class _Early(_Recorder):
        def __init__(self):
            super().__init__()
            made.append(self)

    async def reply(text):
        return REPLY, {}

    session = pipeline.VoiceSession(reply, speak=False)
    warm = _Recorder()
    session.recorder, session.player = warm, _Player()

    async def load():
        await asyncio.sleep(0.05)
        runtime.voice = session
        return session

    monkeypatch.setattr(audio, "Recorder", _Early)
    monkeypatch.setattr(audio, "tick_alone", lambda: None)
    monkeypatch.setattr(links, "ensure_voice", load)

    with _socket(monkeypatch, None).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})  # the button: cold
        assert ws.receive_json()["type"] == "listening"
        time.sleep(0.3)  # the models are up
        ws.send_json({"type": "voice_start"})  # the key
        assert ws.receive_json()["type"] == "listening"
        ws.send_json({"type": "voice_stop"})
        ws.receive_json()
        ws.receive_json()
        left_open = warm.recording

    assert not made[0].recording
    assert not left_open, "the turn was answered and the session's microphone is still open"


def test_a_warm_press_with_no_microphone_is_answered_not_a_dropped_socket(monkeypatch):
    """A microphone that can't open is an answer on the first press of the
    day, and the app says so. On every later press it raised in the socket's
    loop and dropped the socket."""
    _OpenAI(monkeypatch)

    async def reply(text):
        return REPLY, {}

    class _NoMic(_Recorder):
        def start(self, pressed=None):
            raise RuntimeError("Error opening InputStream: Device unavailable")

    session = pipeline.VoiceSession(reply, speak=False)
    session.recorder, session.player = _NoMic(), _Player()

    with _socket(monkeypatch, session).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})
        said = ws.receive_json()
        ws.send_json({"type": "ping"})  # the socket is still there
        assert ws.receive_json()["type"] == "pong"

    assert said["type"] == "voice_unavailable"
    assert said["warm"]["error"].startswith("microphone: ")


def test_a_microphone_unplugged_while_held_can_be_opened_by_the_next_press(monkeypatch):
    """A headset unplugged while the key is held: PortAudio's stop fails.
    The recorder stayed marked as recording, so every later press returned
    early, with no microphone, until the worker restarted. A fake
    sounddevice; nothing is opened."""
    opened: list[object] = []

    class _Stream:
        def __init__(self, **kwargs):
            self.dead = self.closed = False
            opened.append(self)

        def start(self):
            pass

        def stop(self):
            if self.dead:
                raise RuntimeError("Error stopping stream: Unanticipated host error")

        def close(self):
            self.closed = True

    class _Sd:
        InputStream = _Stream

        @staticmethod
        def query_devices(device):
            return {"default_samplerate": 48_000}

    monkeypatch.setattr(audio, "sd", _Sd)
    recorder = audio.Recorder(device=0)
    recorder.start()
    opened[0].dead = True
    with pytest.raises(RuntimeError):
        recorder.stop()
    recorder.start()  # the next press, on the laptop's own microphone

    assert len(opened) == 2, "the next press opened no microphone"
    assert opened[0].closed and recorder.recording


def test_the_live_microphone_is_timed_from_the_press_not_from_the_recorders_open(monkeypatch):
    """Session E's bar is the press to a live microphone. Its clock started
    inside the recorder, so whatever ran between the press and the open (a
    reply stopped, the desk's conversation ended) went unseen: a press that
    waited 0.4 s for the microphone still read as a quick one."""
    from agent.worker import desk

    streams: list = []

    class _Stream:
        def __init__(self, *, callback, blocksize, **kwargs):
            self.callback, self.blocksize = callback, blocksize
            streams.append(self)

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(audio.sd, "InputStream", _Stream)
    monkeypatch.setattr(audio.sd, "query_devices", lambda device: {"default_samplerate": 16_000})
    monkeypatch.setattr(desk, "hotkey_down", lambda: time.sleep(0.4))
    _OpenAI(monkeypatch)

    async def reply(text):
        return REPLY, {}

    session = pipeline.VoiceSession(reply, speak=False)
    session.recorder, session.player = audio.Recorder(device=0), _Player()

    with _socket(monkeypatch, session).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})
        assert ws.receive_json()["type"] == "listening"
        [stream] = streams
        block = np.full((stream.blocksize, 1), 0.2, np.float32)
        stream.callback(block, stream.blocksize, None, None)
        # The block's first sample was heard its length before it arrived.
        assert session.recorder.live_ms >= 400 - stream.blocksize * 1000 / 16_000

    # And the first press of a worker's life, with no session yet.
    from agent.worker import links

    given: list[float | None] = []

    class _Cold(_Recorder):
        def start(self, pressed: float | None = None) -> None:
            given.append(pressed)
            super().start(pressed)

    async def _never():
        await asyncio.sleep(3600)

    monkeypatch.setattr(audio, "Recorder", _Cold)
    monkeypatch.setattr(audio, "tick_alone", lambda: None)
    monkeypatch.setattr(links, "ensure_voice", _never)
    with _socket(monkeypatch, None).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "voice_start"})
        assert ws.receive_json()["type"] == "listening"
    assert given and given[0] is not None, "the first press's clock started in the recorder"


def _press(monkeypatch, message: dict, *, locked: bool = False,
           warm: bool = False) -> tuple[dict, list]:
    """One press on the app's socket, cold or with the voice session up:
    what the socket answered, and each time a microphone was started."""
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.testclient import TestClient

    from agent import runtime
    from agent.worker import desk, links, routes
    from voice import listen

    started: list[str] = []

    class _Counted(_Recorder):
        def start(self, pressed: float | None = None) -> None:
            started.append("cold")
            super().start(pressed)

    class _Warm:
        player = _Player()

        def start_listening(self, pressed: float | None = None) -> None:
            started.append("warm")

        def stop_listening(self) -> audio.Recording:
            return audio.Recording(audio=np.zeros(0, dtype=np.float32))

        def cancel_listening(self) -> None:
            pass

    async def _load():
        return None

    monkeypatch.setattr(audio, "Recorder", _Counted)
    monkeypatch.setattr(audio, "tick_alone", lambda: None)
    monkeypatch.setattr(links, "ensure_voice", _load)
    monkeypatch.setattr(listen, "screen_locked", lambda: locked)
    monkeypatch.setattr(runtime, "voice", _Warm() if warm else None)
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setitem(desk._state, "engine", None)
    monkeypatch.setitem(desk._state, "ticker", None)
    monkeypatch.setattr(desk, "hotkey_down", lambda: None)
    app = Starlette(routes=[WebSocketRoute("/ws", routes.ws_session)])

    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json(message)
        answered = ws.receive_json()
    return answered, started


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_a_press_opens_no_microphone_unless_the_user_is_signed_in_at_an_unlocked_screen(
        monkeypatch, warm):
    """The app hides the button signed out, but whatever held the token
    could press on the socket, and each press held the microphone open up
    to two minutes (step 11's audit, C3). The backend asks too: once a
    passphrase is set, the press carries the app's live session; and never
    while the screen is locked."""
    from agent.worker import signin
    from core import passphrase

    monkeypatch.setattr(signin, "_session", {"digest": None, "seen": 0.0})
    assert passphrase.choose("correct horse battery") is None
    session = signin.start()

    for message, locked in (({"type": "voice_start"}, False),
                            ({"type": "voice_start", "session": "not-the-one"}, False),
                            ({"type": "voice_start", "session": session}, True)):
        answered, started = _press(monkeypatch, message, locked=locked, warm=warm)
        assert started == [], message
        assert answered["type"] == "voice_unavailable"
        assert answered["warm"]["state"] == "refused"

    answered, started = _press(monkeypatch, {"type": "voice_start", "session": session},
                               warm=warm)
    assert answered == {"type": "listening"}
    assert started == ["warm" if warm else "cold"]


def test_before_a_passphrase_is_set_a_press_needs_no_session(monkeypatch):
    """During setup there is nothing to sign in with yet; the screen is
    still asked."""
    answered, started = _press(monkeypatch, {"type": "voice_start"})
    assert answered == {"type": "listening"} and started == ["cold"]

    answered, started = _press(monkeypatch, {"type": "voice_start"}, locked=True)
    assert answered["type"] == "voice_unavailable" and started == []
