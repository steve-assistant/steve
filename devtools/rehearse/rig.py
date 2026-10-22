"""The desk, switched on in a room that is software, and what it did there.

`Rig` loads the speech models once (Whisper on the GPU, Kokoro, as the
worker does), then for each scenario switches the desk on through its own
path -- `agent/worker/desk.switch`, which builds the ear with `_build_ear`:
the trained wake detector and its raw-microphone twin, Silero, the
end-of-turn judge, the loopback detector and AEC3 -- with three seams
swapped, all in this process and nowhere else:

- `voice.listen.Ears`, the hardware, is `VirtualEars`: its microphone and
  loopback threads read the room (devtools/rehearse/room.py) on the wall
  clock instead of WASAPI. Everything after them -- the canceller, `Ear`,
  the engine -- is the worker's code, on its threads.
- the player's device is a clock (devtools/rehearse/speaker.py);
- the assistant (`answering.voice_reply`) and GPT-Live's socket
  (`voice.live.connect`) are the scripted fakes of
  devtools/rehearse/fakes.py, unless `--live` asks for the real GPT-Live.

The database is a scratch one, made per scenario; the switch's state file
too. Real devices are refused before anything is built (`no_devices`).

What it watches is read from the desk's own record -- the engine's
transitions, the turn lines, the player's blocks, the wake detectors'
scores (`ScoreTap`), Whisper's transcripts -- and checked against the
scenario's expectations (`check`). After the run, while the GPU is idle,
each "hey Steve" the user said is played to the wake detector and Whisper again,
from the raw microphone and from the cancelled one (`_wake_lines`): what
the desk would have heard had it listened to the other.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import types
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soxr

from core import paths
from devtools.rehearse import fakes
from devtools.rehearse import room as rooms
from devtools.rehearse import speaker as speakers
from devtools.rehearse.script import Line, Scenario, compare

ROOT = Path(__file__).resolve().parents[2]


def out_dir() -> Path:
    return paths.data_dir() / "rehearsals"


SR = rooms.SR
# The user's voice and the room's: Kokoro voices the wake detector never trained on
# (voice/wake_train.HELD_OUT_VOICES), so it hears them as it would hear the user
# -- not the desk's own am_echo, which it has also never heard.
VOICES = {"user": "am_michael", "other": "bm_lewis", "video": "af_bella"}
# Where each sits against Kokoro's own level (about -23 dBFS): the user is across
# the desk, the video is the laptop's own output at full level.
LEVELS = {"user": -6.0, "other": -10.0, "video": -3.0}
# GPT-Live's stand-in: a voice unlike the desk's.
LIVE_VOICE = "bm_george"
# Real GPT-Live: $0.05 a minute (agent.models.LIVE_VOICE), each run capped.
LIVE_CAP_S = 180.0
LIVE_USD_PER_MIN = 0.05
# After the microphone opens: its first SETTLE_S are dropped, and the wake
# detector needs a window of sound before it can judge one.
WARM_S = 2.5

log = logging.getLogger("rehearse")


# ---------------------------------------------------------------------------
# No real device, ever
# ---------------------------------------------------------------------------


def _refuse(*args, **kwargs):
    raise RuntimeError("the rehearsal rig never opens a real microphone or speaker")


class Patches:
    """Attributes set for a while, and put back."""

    _MISSING = object()

    def __init__(self) -> None:
        self._undo: list = []

    def set(self, obj, name: str, value) -> None:
        old = getattr(obj, name, self._MISSING)
        setattr(obj, name, value)
        self._undo.append((obj, name, old))

    def undo(self) -> None:
        while self._undo:
            obj, name, old = self._undo.pop()
            if old is self._MISSING:
                with contextlib.suppress(AttributeError):
                    delattr(obj, name)
            else:
                setattr(obj, name, old)


def no_devices(patches: Patches) -> None:
    """Every way this process could open a sound device, refused: sounddevice's
    streams and calls (the player, push-to-talk's recorder) and soundcard
    (the ear's WASAPI microphone and loopback, the tap), whose module is
    replaced before anything imports it."""
    import sounddevice as sd

    for name in ("InputStream", "OutputStream", "Stream", "RawInputStream", "RawOutputStream",
                 "RawStream", "rec", "play", "playrec"):
        patches.set(sd, name, _refuse)
    stub = types.ModuleType("soundcard")
    for name in ("default_microphone", "default_speaker", "get_microphone", "get_speaker",
                 "all_microphones", "all_speakers"):
        setattr(stub, name, _refuse)
    stub.__rehearsal__ = True
    patches.set(sys.modules.get("soundcard") or types.ModuleType("soundcard"), "__rehearsal__",
                True)
    sys.modules["soundcard"] = stub


class _NoRecorder:
    """Push-to-talk's microphone, which a rehearsal never opens."""

    recording = False

    def start(self, pressed: float | None = None) -> None:
        _refuse()

    def stop(self):
        _refuse()


# ---------------------------------------------------------------------------
# Watching the desk
# ---------------------------------------------------------------------------


class ScoreTap:
    """A wake detector, with every score it gave kept: (when, name, score,
    fired, bar)."""

    def __init__(self, inner, name: str, keep: list, clock=time.monotonic) -> None:
        self.inner = inner
        self.name = name
        self.keep = keep
        self.clock = clock

    @property
    def threshold(self):
        return self.inner.threshold

    @threshold.setter
    def threshold(self, value) -> None:
        self.inner.threshold = value

    @property
    def last_score(self):
        return self.inner.last_score

    def push(self, audio):
        fired = self.inner.push(audio)
        self.keep.append((self.clock(), self.name, float(self.inner.last_score or 0.0),
                          fired is not None, float(self.inner.threshold)))
        return fired

    def reset(self) -> None:
        self.inner.reset()

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _Block(np.ndarray):
    """A microphone block that knows where it sits on the room's clock, so the
    cancelled copy can be filed beside the raw one."""

    at: int = 0


def virtual_ears(room: rooms.Room, t0: float, stats: dict):
    """`voice.listen.Ears` with the room for its devices."""
    from voice import listen

    def wait_for_desk(upto: int, timeout: float = 0.3) -> None:
        deadline = time.monotonic() + timeout
        while room.desk.n < upto and time.monotonic() < deadline:
            time.sleep(0.002)
        if room.desk.n < upto:
            stats["desk_late"] = stats.get("desk_late", 0) + 1

    def next_index(frames: int) -> int:
        return int((time.monotonic() - t0) * SR // frames) + 1

    class VirtualEars(listen.Ears):
        def __init__(self, listener, *, locked=None, canceller=None, on_state=None) -> None:
            super().__init__(listener, locked=lambda: False, canceller=canceller,
                             on_state=on_state)
            if canceller is not None and getattr(canceller, "available", False):
                near = canceller.near

                def cleaned(block):
                    out = near(block)
                    at = getattr(block, "at", None)
                    if at is not None:
                        room.cleaned.put(int(at), out)
                    return out

                canceller.near = cleaned

        def _mic(self) -> None:
            frames = int(SR * self.MIC_READ_S)
            while self._running:
                if not self._want_mic:
                    self.device = None
                    time.sleep(0.05)
                    continue
                self.device = "the rehearsal room"
                settled = time.monotonic() + listen.SETTLE_S
                k = next_index(frames)
                while self._running and self._want_mic:
                    start = k * frames
                    due = t0 + (start + frames) / SR
                    wait = due - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    elif -wait > 0.5:
                        stats["mic_skipped"] = stats.get("mic_skipped", 0) + 1
                        k = next_index(frames)
                        continue
                    wait_for_desk(start + frames)
                    block = room.mic(start, frames).view(_Block)
                    block.at = start
                    if room.canceller_missing:
                        room.cleaned.put(start, block)
                    if time.monotonic() >= settled:
                        try:
                            self._queue.put_nowait(block)
                        except Exception:  # noqa: BLE001 - queue.Full, as the real one counts
                            self.dropped += 1
                    k += 1

        def _loopback(self) -> None:
            frames = int(SR * listen.LOOPBACK_READ_S)
            self.listener.loopback.wait_s = listen.LOOPBACK_READ_S + 0.15
            self.loopback_ok = True
            k = next_index(frames)
            while self._running:
                start = k * frames
                due = t0 + (start + frames) / SR
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                wait_for_desk(start + frames)
                if self.open:
                    self.listener.loopback.push(room.loopback(start, frames))
                k += 1
            self.loopback_ok = False

    return VirtualEars


class _Capture(logging.Handler):
    """The desk's log lines, as the worker would have written them."""

    def __init__(self, keep: list) -> None:
        super().__init__(level=logging.INFO)
        self.keep = keep

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self.keep.append((time.monotonic(), record.name, record.levelname,
                              record.getMessage()))


# ---------------------------------------------------------------------------
# One scenario's record
# ---------------------------------------------------------------------------


@dataclass
class Placed:
    line: Line
    start: float
    end: float
    voice: str
    wake_end: float | None = None  # where "hey Steve" ends in it


@dataclass
class Record:
    scenario: Scenario
    t0: float = 0.0       # the room's clock
    ts: float = 0.0       # the scenario's start: the desk idle and warm
    end: float = 0.0
    lines: list[Placed] = field(default_factory=list)
    unsaid: list[Line] = field(default_factory=list)
    scores: list = field(default_factory=list)
    transcripts: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    live_ends: list[dict] = field(default_factory=list)
    calls: list = field(default_factory=list)
    sockets: list = field(default_factory=list)
    fakes: list = field(default_factory=list)
    logs: list = field(default_factory=list)
    history: list = field(default_factory=list)
    asked: list = field(default_factory=list)
    played: list = field(default_factory=list)
    glitches: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    said: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    wake_lines: list[dict] = field(default_factory=list)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    live_capped: bool = False
    folder: Path | None = None

    def rel(self, at: float | None) -> float | None:
        return None if at is None else at - self.ts

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


def _owner_kind(owner) -> str:
    return speakers.kind(owner)


# ---------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------


class Rig:
    """The speech models, loaded once; a fresh desk for each scenario."""

    def __init__(self, *, live: bool = False, save_audio: bool = False, out: Path | None = None,
                 verbose: bool = False) -> None:
        self.live = live
        self.save_audio = save_audio
        self.out = (out or out_dir()) / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.verbose = verbose
        self.patches = Patches()
        self.session = None
        self.assistant: fakes.Assistant | None = None
        self._logs: list = []
        self._capture = _Capture(self._logs)
        self._voices: dict[tuple[str, str], np.ndarray] = {}
        self._scratch = tempfile.TemporaryDirectory(prefix="rehearsal-")

    # -- loading ------------------------------------------------------------

    async def load(self) -> dict:
        """Refuse every device, point the models at the live checkout's cache
        (STEVE_MODEL_CACHE), and warm Whisper and Kokoro as the worker does."""
        no_devices(self.patches)
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from agent import approvals, runtime
        from voice.pipeline import VoiceSession

        scratch = Path(self._scratch.name)
        self.patches.set(paths, "db_path", lambda: scratch / "never.db")
        self.patches.set(approvals, "_notify", None)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(self._capture)
        for noisy in ("faster_whisper", "httpx", "websockets"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        # The add-on's models, named: hands-free is rehearsed on them, and the
        # session's own defaults are OpenAI's, which this rig -- run outside
        # the test fences -- would pay to voice every line of the room.
        from voice.stt import Transcriber
        from voice.tts import Speaker

        session = VoiceSession(self._reply, streams=True, transcriber=Transcriber(),
                               speaker=Speaker())
        session.recorder = _NoRecorder()
        session.player = speakers.VirtualPlayer()
        started = time.perf_counter()
        report = await asyncio.to_thread(session.warm)
        report["load_s"] = round(time.perf_counter() - started, 1)
        self.patches.set(runtime, "voice", session)
        self.session = session
        return report

    async def _reply(self, text, channel="voice", **hooks):
        return await self.assistant.reply(text, channel, **hooks)

    def close(self) -> None:
        if self.session is not None:
            with contextlib.suppress(Exception):
                self.session.speaker.stop()
        logging.getLogger().removeHandler(self._capture)
        self.patches.undo()
        with contextlib.suppress(Exception):
            self._scratch.cleanup()

    # -- voices --------------------------------------------------------------

    def voice_of(self, text: str, voice: str, rate: int = SR) -> np.ndarray:
        """Kokoro saying `text` in `voice`, at `rate`. Blocking; made once."""
        key = (text, voice, rate)
        if key not in self._voices:
            speech = self.session.speaker.say(text, voice=voice)
            audio = np.asarray(speech.audio, np.float32)
            if speech.sample_rate != rate:
                audio = soxr.resample(audio, speech.sample_rate, rate).astype(np.float32)
            self._voices[key] = audio
        return self._voices[key]

    def _line_audio(self, scenario: Scenario, line: Line) -> tuple[np.ndarray, str, float | None]:
        from core import desk as rules

        voice = scenario.voices.get(line.who, VOICES[line.who])
        level = rooms.gain(scenario.levels.get(line.who, LEVELS[line.who]))
        audio = self.voice_of(line.text, voice) * level
        wake_s = None
        if (phrase := rules._PHRASE.search(line.text)) is not None and phrase.start() < 3:
            # Where the phrase ends in the line: as long as Kokoro takes to
            # say the phrase alone, less the silence it leaves after it.
            alone = self.voice_of(line.text[:phrase.end()] + ".", voice)
            loud = np.nonzero(np.abs(alone) > 0.02)[0]
            wake_s = (loud[-1] + 1) / SR if len(loud) else len(alone) / SR
        return audio.astype(np.float32), voice, wake_s

    # -- one scenario -------------------------------------------------------

    async def rehearse(self, scenario: Scenario) -> Record:
        from agent import runtime
        from agent.worker import answering
        from agent.worker import desk as handler
        from core import db
        from voice import desk_call, listen
        from voice import live as gpt_live

        record = Record(scenario=scenario)
        length = min(scenario.length, LIVE_CAP_S) if self.live else scenario.length
        acoustics = rooms.Acoustics.of(scenario.room)
        room = rooms.Room(acoustics, seconds=length + 90.0)
        room.canceller_missing = False
        # Every voice it will need, made before the clock starts.
        audio = {id(line): await asyncio.to_thread(self._line_audio, scenario, line)
                 for line in scenario.lines}
        live_lines = [fakes.GREETING, fakes.FILLER, fakes.GOODBYE, "Checking that now.",
                      *scenario.live.get("says", [])]
        for text in live_lines:
            await asyncio.to_thread(self.voice_of, text, LIVE_VOICE, fakes.LIVE_RATE)

        patches = Patches()
        scratch = Path(tempfile.mkdtemp(prefix=f"{scenario.name}-", dir=self._scratch.name))
        conn = db.open_store(scratch / "desk.db")
        patches.set(runtime, "conn", conn)
        patches.set(runtime, "conversations", {})
        patches.set(handler, "STATE_FILE", scratch / "listening.json")
        self.assistant = fakes.Assistant(scenario.replies)
        patches.set(answering, "voice_reply", self.assistant.reply)
        self.session.reply_fn = self.assistant.reply
        player = speakers.VirtualPlayer()
        self.session.player = player
        # The laptop's speakers, not headphones: the echo canceller is all
        # that keeps its own voice out of the microphone.
        patches.set(desk_call, "duplex_default", lambda: False)

        stats: dict = {}
        t0 = time.monotonic()
        record.t0 = t0
        patches.set(listen, "Ears", virtual_ears(room, t0, stats))
        patches.set(gpt_live, "connect", self._connector(record))
        clock = speakers.SpeakerClock(player, room.write_desk)
        clock.start(t0)
        logs_from = len(self._logs)
        try:
            await handler.switch(True, by="rehearsal")
            engine = handler._state.get("engine")
            ear = handler._state.get("listener")
            if engine is None or ear is None:
                raise RuntimeError(f"the desk did not switch on: {handler._state.get('error')}")
            if handler._state.get("echo") is None:
                room.canceller_missing = True
            self._watch(record, engine, ear)
            deadline = time.monotonic() + 15.0
            while engine.mode != "idle" and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if engine.mode != "idle":
                raise RuntimeError(f"the desk never listened: it is {engine.mode}")
            await asyncio.sleep(WARM_S)
            record.ts = time.monotonic()
            clock.stalls = tuple((record.ts - t0 + at, seconds)
                                 for at, seconds in scenario.stalls)
            script = asyncio.ensure_future(self._script(record, room, engine, player, audio))
            ends = record.ts + length
            while time.monotonic() < ends and not record.live_capped:
                await asyncio.sleep(0.1)
            record.end = time.monotonic()
            script.cancel()
            record.counts = dict(engine.counts)
            record.history = list(engine.history)
            record.said = list(engine.floor._said)
            record.stats = {"ear": dict(vars(ear.counts)), "rig": stats,
                            "speaker": {"callbacks": clock.callbacks,
                                        "dry_s": round(clock.dry_s, 2),
                                        "late": engine.floor.late,
                                        "reopened": engine.floor.reopened},
                            "end_mode": engine.mode, "floor_dropped": engine.floor.dropped,
                            "echo": bool(handler._state.get("echo"))}
            handler.stop("rehearsal over")
            ticker, handler._state["ticker"] = handler._state.get("ticker"), None
            if ticker is not None:
                ticker.cancel()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(engine.settle(), 20.0)
            handler._state.update(engine=None, last=None, last_call=None, false_wakes=0,
                                  dropped_follow_ups=0)
        finally:
            clock.stop()
            record.played = list(player.played)
            record.glitches = list(clock.glitches)
            record.logs = self._logs[logs_from:]
            patches.undo()
            conn.close()
        record.asked = list(self.assistant.asked)
        await asyncio.to_thread(self._wake_lines, record, room)
        check(record)
        self._save(record, room)
        return record

    def _connector(self, record: Record):
        """GPT-Live's socket: the fake, or with `--live` the real one, every
        event's kind and loudness kept."""
        from voice import live as gpt_live

        real = gpt_live.connect
        scenario = record.scenario

        async def fake():
            live = fakes.Live(lambda text: self.voice_of(text, LIVE_VOICE, fakes.LIVE_RATE),
                              says=scenario.live.get("says", []),
                              first_s=scenario.live.get("first", 1.0),
                              noise_db=scenario.live.get("noise"),
                              vad_db=scenario.live.get("vad", -42.0),
                              delegate=scenario.live.get("delegate", False))
            record.fakes.append(live)
            return live

        async def tapped():
            if sum(1 for _ in record.sockets) >= 2:
                raise RuntimeError("a rehearsal opens at most two real GPT-Live sessions")
            socket = fakes.SocketTap(await real())
            record.sockets.append(socket)
            asyncio.ensure_future(self._cap(record))
            return socket

        return tapped if self.live else fake

    async def _cap(self, record: Record) -> None:
        """Real GPT-Live is billed: whatever the scenario says, the run ends
        LIVE_CAP_S after the first session opened."""
        from agent.worker import desk as handler

        started = time.monotonic()
        first = getattr(record, "_live_started", None)
        if first is None:
            record._live_started = started
        else:
            return
        await asyncio.sleep(LIVE_CAP_S)
        engine = handler._state.get("engine")
        if engine is not None and engine.call is not None:
            engine._end_live("rehearsal cap")
        record.live_capped = True

    def _watch(self, record: Record, engine, ear) -> None:
        """Taps on the desk's own record: scores, transcripts, turns, calls."""
        ear.detector = ScoreTap(ear.detector, "clean", record.scores)
        if ear.raw_detector is not None:
            ear.raw_detector = ScoreTap(ear.raw_detector, "raw", record.scores)
        make = ear.loopback.detector
        ear.loopback.detector = lambda: ScoreTap(make(), "laptop", record.scores)
        shell = engine.shell
        transcribe, log_turn, live_ended, live_call = (shell.transcribe, shell.log_turn,
                                                       shell.live_ended, shell.live_call)

        async def transcribed(heard):
            entry = {"kind": heard.kind, "started": heard.started, "ended": heard.ended,
                     "at": time.monotonic()}
            result = await transcribe(heard)
            entry.update(done=time.monotonic(), text=result.text, ok=result.ok,
                         ms=result.transcribe_ms)
            record.transcripts.append(entry)
            return result

        def logged(turn):
            record.turns.append({"at": time.monotonic(), "id": turn.id, "source": turn.source,
                                 "outcome": turn.outcome, "request": turn.request,
                                 "cut_in": turn.cut_in, **turn.heard, **turn.facts})
            log_turn(turn)

        def ended(call, reason, *, unanswered):
            record.live_ends.append({"at": time.monotonic(), "reason": reason,
                                     "unanswered": unanswered, "frames_s": call.audio_s,
                                     "audible_s": call.loud_s, "events": dict(call.events),
                                     "opened": call.opened_at, "ended": call.ended_at,
                                     "said_word_at": call.said_word_at,
                                     "talking_s": engine.floor.audible_s.get("livecall", 0.0)})
            live_ended(call, reason, unanswered=unanswered)

        def made():
            call = live_call()
            record.calls.append(call)
            return call

        shell.transcribe, shell.log_turn, shell.live_ended, shell.live_call = (
            transcribed, logged, ended, made)

    async def _script(self, record: Record, room: rooms.Room, engine, player,
                      audio: dict) -> None:
        """The user's lines and the video's, placed in the room as their times come."""
        previous = record.ts
        for line in record.scenario.lines:
            at = await self._when(line, previous, record, engine, player)
            if at is None:
                record.unsaid.append(line)
                continue
            at = max(at, time.monotonic() + 0.05)
            sound, voice, wake_s = audio[id(line)]
            start = int(round((at - record.t0) * SR))
            (room.video if line.who == "video" else room.people).place(sound, start)
            placed = Placed(line, at, at + len(sound) / SR, voice,
                            None if wake_s is None else at + wake_s)
            record.lines.append(placed)
            if line.who != "video":
                previous = placed.end

    async def _when(self, line: Line, previous: float, record: Record, engine,
                    player) -> float | None:
        when = line.when
        if when.anchor is None:
            return record.ts + when.offset
        if when.anchor == "+":
            return previous + when.offset
        from voice.floor import AUDIBLE_RMS

        while time.monotonic() < previous:
            await asyncio.sleep(0.05)
        while True:
            seen = None
            if when.anchor in ("voice", "live"):
                want = "turn" if when.anchor == "voice" else "livecall"
                seen = next((at for at, owner, level, _ in list(player.played)
                             if at >= previous and level >= AUDIBLE_RMS
                             and _owner_kind(owner) == want), None)
            elif when.anchor in ("idle", "follow"):
                seen = next((h["at"] for h in list(engine.history)
                             if h["to"] == when.anchor and h["at"] >= previous - 0.05), None)
            elif when.anchor == "quiet":
                audible = [at + s for at, owner, level, s in list(player.played)
                           if level >= AUDIBLE_RMS and at + s >= previous]
                last = max(audible, default=previous)
                if time.monotonic() - last >= 0.5 and engine.mode not in (
                        "capture", "transcribe", "local"):
                    seen = last
            if seen is not None:
                return seen + when.offset
            await asyncio.sleep(0.05)

    # -- after the run --------------------------------------------------------

    def _wake_lines(self, record: Record, room: rooms.Room) -> None:
        """Each "hey Steve" the user said, heard again: its score alone, what the
        desk's detectors scored it over the room, and what Whisper makes of
        the raw microphone and of the cancelled one -- whether the phrase is
        there for `core/desk.wake_phrase` to find."""
        from core import desk as rules
        from voice import wake

        detector = next((s for s in (getattr(self, "_frontend", None),) if s), None)
        if detector is None:
            frontend = wake.Frontend()
            head = wake.Head.load(wake.model_dir() / wake.HEAD_FILE)
            self._frontend = (frontend, head)
        frontend, head = self._frontend

        def best(audio: np.ndarray) -> float:
            found = wake.Detector(frontend, head, threshold=2.0)
            top = 0.0
            for i in range(0, len(audio) - 1279, 1280):
                found.push(audio[i:i + 1280])
                top = max(top, float(found.last_score))
            return top

        for placed in record.lines:
            if placed.line.who == "video" or placed.wake_end is None:
                continue
            voice = self.voice_of(placed.line.text, placed.voice)
            alone = best(np.concatenate([np.zeros(SR, np.float32), voice,
                                         np.zeros(SR // 2, np.float32)]))
            lo, hi = placed.start - 0.2, placed.end + 1.5
            scores = {name: max((s for at, n, s, _, _ in record.scores
                                 if n == name and lo <= at <= hi), default=None)
                      for name in ("clean", "raw")}
            fired = {name: any(f for at, n, _, f, _ in record.scores
                               if n == name and lo <= at <= hi) for name in ("clean", "raw")}
            laptop = max((s for at, n, s, _, _ in record.scores
                          if n == "laptop" and lo <= at <= hi), default=None)
            start = int((placed.start - record.t0 - 0.3) * SR)
            n = int((placed.end - placed.start + 0.8) * SR)
            heard, replay = {}, {}
            for name, tape in (("raw", room.heard), ("clean", room.cleaned)):
                clip = tape.read(start, n)
                loud = bool(rooms.rms(clip))
                text = self.session.transcriber.transcribe(clip).text if loud else ""
                heard[name] = {"text": text, "phrase": rules.wake_phrase(text) is not None}
                # The detector played the tape again, as `alone` is scored. The
                # live raw tap runs only while the desk is audible and read 0.00
                # on every line of the first runs, where Whisper heard "hey
                # Steve" plainly on the same raw audio: what the raw microphone
                # would score is read here, not taken from the tap.
                # A second and a half of silence first: the detector needs a
                # window of sound before it can score one, as `alone` gets.
                lead = np.zeros(int(1.5 * SR), np.float32)
                replay[name] = best(np.concatenate([lead, clip])) if loud else 0.0
            record.wake_lines.append({"line": placed.line.text, "at": record.rel(placed.start),
                                      "alone": alone, "scores": scores, "fired": fired,
                                      "replay": replay, "laptop": laptop, "whisper": heard})

    def _save(self, record: Record, room: rooms.Room) -> None:
        folder = self.out / record.scenario.name
        folder.mkdir(parents=True, exist_ok=True)
        record.folder = folder
        (folder / "report.txt").write_text(report(record) + "\n", encoding="utf-8", newline="\n")
        (folder / "record.json").write_text(json.dumps(summary(record), indent=1, default=str)
                                            + "\n", encoding="utf-8", newline="\n")
        if not self.save_audio:
            return
        start = int((record.ts - record.t0 - 1.0) * SR)
        n = int((record.end - record.ts + 2.0) * SR)
        mic, out, clean = (room.heard.read(start, n), room.desk.read(start, n),
                           room.cleaned.read(start, n))
        _wav(folder / "room.wav", np.stack([mic, out], axis=1))
        _wav(folder / "cleaned.wav", clean[:, None])


def _wav(path: Path, audio: np.ndarray) -> None:
    """Synthetic sound only: the room of a rehearsal, never the user's."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(audio.shape[1])
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes(pcm.tobytes())


# ---------------------------------------------------------------------------
# What happened, checked
# ---------------------------------------------------------------------------


def _audible(record: Record) -> list[dict]:
    from voice.floor import AUDIBLE_RMS

    return speakers.audible_runs(record.played, floor=AUDIBLE_RMS)


def first_audio(record: Record) -> float | None:
    """From the end of the marked line to the desk's voice."""
    marked = next((p for p in record.lines if p.line.mark), None)
    if marked is None:
        return None
    runs = [r for r in _audible(record) if _owner_kind(r["owner"]) in ("turn", "livecall")
            and r["start"] >= marked.end]
    return runs[0]["start"] - marked.end if runs else None


# How far before the user's line a voice counts as one they were speaking over.
LOOK_BACK_S = 2.0


def cut_in(record: Record) -> float | None:
    """From the end of "hey Steve" in the marked line to the moment the voice
    it was said over stopped being audible."""
    marked = next((p for p in record.lines if p.line.mark), None)
    if marked is None or marked.wake_end is None:
        return None
    runs = [r for r in _audible(record) if _owner_kind(r["owner"]) in ("turn", "livecall")]
    # The voices the user spoke over: audible while they said the line up to the
    # phrase's end, or in the LOOK_BACK_S before it -- a live voice that fell
    # quiet just before they spoke (GPT-Live yields to speech, and in a loud
    # room to its own echo) was still the one they were stopping. One that
    # carried on afterwards is measured to where it really ended.
    over = [r for r in runs if r["start"] <= marked.wake_end
            and r["end"] >= marked.start - LOOK_BACK_S]
    if not over:
        return None
    # Until that voice was last heard, not until its first pause: a stop
    # that was never heard let the answer run on, sentence after sentence,
    # and the end of the sentence it was said over read as a quick cut-in
    # (10 October 2026, the rig's first run).
    owners = {id(r["owner"]) for r in over}
    last = max(r["end"] for r in runs if id(r["owner"]) in owners)
    return max(0.0, last - marked.wake_end)


def silent_after(record: Record) -> float | None:
    """How long after the marked line ended the desk's last voice stopped
    (0 if it said nothing more)."""
    marked = next((p for p in record.lines if p.line.mark), None)
    if marked is None:
        return None
    later = [r["end"] for r in _audible(record)
             if _owner_kind(r["owner"]) in ("turn", "livecall", "nobody")
             and r["end"] > marked.end]
    return max(later, default=marked.end) - marked.end


def _said_twice(record: Record) -> list[str]:
    seen: dict[str, int] = {}
    for owner, text, _, _ in record.said:
        key = " ".join(re.findall(r"[a-z0-9']+", text.lower()))
        if len(key.split()) >= 4 and _owner_kind(owner) == "turn":
            seen[key] = seen.get(key, 0) + 1
    return [k for k, n in seen.items() if n > 1]


def check(record: Record) -> None:
    out = record.checks
    outcomes = [t["outcome"] for t in record.turns]
    for expect in record.scenario.expects:
        name, args = expect.check, expect.args
        label = f"{name} {' '.join(str(a) for a in args)}".strip()
        if name == "asked":
            hit = [text for _, text in record.asked if re.search(args[0], text, re.I)]
            out.append((label, bool(hit), f"asked: {[t for _, t in record.asked]}"))
        elif name == "not-asked":
            out.append((label, not record.asked, f"asked: {[t for _, t in record.asked]}"))
        elif name == "outcome":
            want = set(args[0].split("|"))
            out.append((label, bool(want & set(outcomes)), f"outcomes: {outcomes}"))
        elif name == "no-outcome":
            want = set(args[0].split("|"))
            out.append((label, not (want & set(outcomes)), f"outcomes: {outcomes}"))
        elif name in ("first-audio", "cut-in", "silent-after"):
            value = {"first-audio": first_audio, "cut-in": cut_in,
                     "silent-after": silent_after}[name](record)
            out.append((label, compare(value, *args),
                        "never" if value is None else f"{value:.2f} s"))
        elif name == "live":
            want = set(args[0].split("|"))
            reasons = [e["reason"] for e in record.live_ends]
            out.append((label, bool(want & set(reasons)), f"ended: {reasons}"))
        elif name == "no-live":
            out.append((label, not record.calls, f"{len(record.calls)} opened"))
        elif name == "live-voice":
            value = sum(e["talking_s"] for e in record.live_ends) if record.live_ends else None
            out.append((label, compare(value, *args),
                        "never" if value is None else f"{value:.1f} s"))
        elif name == "one-voice":
            runs = _audible(record)
            nobody = [r for r in runs if r["owner"] is None]
            twice = _said_twice(record)
            clash = speakers.voices_at_once(runs)
            ok = not nobody and not twice and not clash
            out.append((label, ok, f"ownerless={len(nobody)} said_twice={twice} "
                                   f"run_into={len(clash)}"))
        elif name == "end-mode":
            mode = record.stats.get("end_mode")
            out.append((label, mode in args[0].split("|"), f"ended in {mode}"))
        elif name in ("wakes", "laptop-wakes", "transcribed"):
            value = {"wakes": record.counts.get("wakes", 0),
                     "laptop-wakes": record.counts.get("laptop_wakes", 0),
                     "transcribed": len(record.transcripts)}[name]
            out.append((label, compare(value, *args), str(value)))
    for line in record.unsaid:
        out.append((f"said: {line.text[:40]}", False,
                    f"its moment ({line.when.anchor}) never came"))


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _fmt(at: float | None) -> str:
    return "   -  " if at is None else f"{at:6.2f}"


def timeline(record: Record) -> list[tuple[float, str]]:
    rel = record.rel
    rows: list[tuple[float, str]] = []
    for p in record.lines:
        who = {"user": "user ", "other": "other", "video": "video"}[p.line.who]
        rows.append((rel(p.start), f"{who} \"{p.line.text}\" ({p.end - p.start:.1f} s, "
                                   f"{p.voice}){'  <- marked' if p.line.mark else ''}"))
    for h in record.history:
        if h["at"] >= record.ts - 0.5:
            rows.append((rel(h["at"]), f"desk  {h['from']} -> {h['to']} ({h['why'] or '-'})"))
    for run in _audible(record):
        k = _owner_kind(run["owner"])
        if run["end"] < record.ts:
            continue
        text = ""
        if k == "turn":
            text = " ".join(t for o, t, s, e in record.said if o is run["owner"]
                            and s <= run["end"] and e >= run["start"])
        label = {"turn": "it   ", "livecall": "live ", "nobody": "NOBODY"}.get(k, k[:5].ljust(5))
        rows.append((rel(run["start"]), f"{label} audible {run['end'] - run['start']:.1f} s"
                     + (f" \"{_short(text)}\"" if text else "")))
    for t in record.transcripts:
        rows.append((rel(t["done"]), f"stt   {t['kind']}: \"{t['text']}\" ({t['ms']} ms)"))
    for at, text in record.asked:
        rows.append((rel(at), f"asked \"{text}\""))
    for t in record.turns:
        rows.append((rel(t["at"]), f"turn  {t['outcome']} source={t['source']} "
                                   f"cut_in={t['cut_in']} stt_ms={t.get('stt_ms', '-')} "
                                   f"first_audio_ms={t.get('first_audio_ms', '-')}"))
    for e in record.live_ends:
        rows.append((rel(e["at"]), f"live  ended: {e['reason']} frames={e['frames_s']:.1f}s "
                                   f"audible={e['audible_s']:.1f}s "
                                   f"unanswered={'yes' if e['unanswered'] else 'no'}"))
    for at, _, _level, message in record.logs:
        if message.startswith(("ear:", "floor:", "live:", "a wake the laptop")) or (
                "near wake" in message):
            rows.append((rel(at), f"log   {message}"))
    for at, seconds, forced in record.glitches:
        rows.append((rel(record.t0 + at), f"GLITCH the speaker ran dry {seconds:.2f} s with "
                                          f"sound queued{' (stall)' if forced else ''}"))
    rows.sort(key=lambda r: (r[0] if r[0] is not None else -1e9))
    return rows


def _short(text: str, n: int = 70) -> str:
    return text if len(text) <= n else text[:n - 3] + "..."


def _live_measures(record: Record) -> list[str]:
    out = []
    for i, socket in enumerate(record.sockets, 1):
        frames = [e for e in socket.got if e["type"] == "session.output_audio.delta"]
        words = [e["at"] for e in socket.got if e["type"] == "session.output_transcript.delta"]
        if not frames:
            continue
        from voice.floor import AUDIBLE_RMS

        def near_words(at, s=1.5, words=words):
            return any(abs(at - w) <= s for w in words)

        silent = [e["rms"] for e in frames if not near_words(e["at"])]
        speech = [e["rms"] for e in frames if near_words(e["at"], 0.5)]
        gaps = np.diff([e["at"] for e in frames]) if len(frames) > 1 else np.zeros(1)
        n = np.array([e["n"] for e in frames])
        out.append(
            f"real GPT-Live session {i}: {len(frames)} frames, {n.sum() / 24000:.1f} s of audio "
            f"in {frames[-1]['at'] - frames[0]['at']:.1f} s; frame length median "
            f"{np.median(n) / 24:.0f} ms; arrival gap median {np.median(gaps) * 1000:.0f} ms, "
            f"max {gaps.max() * 1000:.0f} ms")
        if silent:
            s = np.array(silent)
            out.append(f"  frames away from its words: {len(s)}; rms max {s.max():.5f}, "
                       f"p99 {np.percentile(s, 99):.5f}, median {np.median(s):.5f}; "
                       f"{(s == 0).mean() * 100:.0f}% digital zeros; "
                       f"{(s >= AUDIBLE_RMS).mean() * 100:.1f}% at or over AUDIBLE_RMS "
                       f"({AUDIBLE_RMS})")
        if speech:
            s = np.array(speech)
            out.append(f"  frames among its words: {len(s)}; rms median {np.median(s):.4f} "
                       f"({20 * np.log10(max(np.median(s), 1e-9)):.1f} dBFS), "
                       f"{(s < AUDIBLE_RMS).mean() * 100:.0f}% under AUDIBLE_RMS")
        kinds: dict[str, int] = {}
        for e in socket.got:
            kinds[e["type"]] = kinds.get(e["type"], 0) + 1
        out.append(f"  events: {json.dumps(kinds)}")
        closed = [e for e in socket.got if e["type"] == "session.closed"]
        if closed:
            out.append(f"  closed: {closed[-1].get('reason')} after "
                       f"{closed[-1].get('seconds')} billed s")
    return out


def report(record: Record) -> str:
    sc = record.scenario
    lines = [f"== {sc.name}: {sc.about}", ""]
    for at, text in timeline(record):
        lines.append(f"  {_fmt(at)}  {text}")
    lines.append("")
    if record.wake_lines:
        lines.append("  hey Steve, heard again (alone | live clean | live raw | laptop twin "
                     "|| replayed clean | replayed raw; Whisper raw / cleaned):")
        for w in record.wake_lines:
            sc_, rp = w["scores"], w.get("replay", {})
            lines.append(
                f"    {_fmt(w['at'])} \"{_short(w['line'], 40)}\": {w['alone']:.2f} | "
                f"{_n(sc_['clean'])}{'*' if w['fired']['clean'] else ' '} | "
                f"{_n(sc_['raw'])}{'*' if w['fired']['raw'] else ' '} | {_n(w['laptop'])} || "
                f"{_n(rp.get('clean'))} | {_n(rp.get('raw'))}")
            for name in ("raw", "clean"):
                h = w["whisper"][name]
                lines.append(f"        whisper {name:5}: \"{_short(h['text'], 60)}\" "
                             f"phrase={'yes' if h['phrase'] else 'NO'}")
        lines.append("        (* the detector fired)")
    for line in _live_measures(record):
        lines.append(f"  {line}")
    for fake in record.fakes:
        lines.append(f"  fake GPT-Live: said {len(fake.said)} lines, talked over "
                     f"{fake.interrupted} times, input {fake.input_s:.1f} s of which loud "
                     f"{fake.loud_input_s:.1f} s")
    s = record.stats
    lines.append(f"  speaker: {s.get('speaker')}; ear: wakes={s.get('ear', {}).get('wakes')} "
                 f"near={s.get('ear', {}).get('near_wakes')} "
                 f"laptop_ignored={s.get('ear', {}).get('laptop_wakes_ignored')}; "
                 f"echo canceller={'on' if s.get('echo') else 'OFF'}; rig={s.get('rig')}")
    lines.append(f"  first audio: {_secs(first_audio(record))}; cut-in: {_secs(cut_in(record))}")
    lines.append("")
    for label, ok, detail in record.checks:
        lines.append(f"  {'PASS' if ok else 'FAIL'}  {label}  ({detail})")
    lines.append(f"  => {'PASS' if record.passed else 'FAIL'}")
    return "\n".join(lines)


def _n(value) -> str:
    return " -  " if value is None else f"{value:.2f}"


def _secs(value) -> str:
    return "-" if value is None else f"{value:.2f} s"


def summary(record: Record) -> dict:
    """The record as numbers and text, for a file beside the report."""
    return {
        "scenario": record.scenario.name, "passed": record.passed,
        "checks": [{"check": c, "ok": ok, "detail": d} for c, ok, d in record.checks],
        "lines": [{"who": p.line.who, "text": p.line.text, "at": record.rel(p.start),
                   "end": record.rel(p.end), "wake_end": record.rel(p.wake_end)}
                  for p in record.lines],
        "history": [{**h, "at": record.rel(h["at"])} for h in record.history],
        "turns": [{k: v for k, v in t.items() if k != "at"} | {"at": record.rel(t["at"])}
                  for t in record.turns],
        "transcripts": [{**t, "started": record.rel(t["started"]), "ended": record.rel(t["ended"]),
                         "at": record.rel(t["at"]), "done": record.rel(t["done"])}
                        for t in record.transcripts],
        "live_ends": [{**e, "at": record.rel(e["at"])} for e in record.live_ends],
        "wake_lines": record.wake_lines, "counts": record.counts, "stats": record.stats,
        "first_audio_s": first_audio(record), "cut_in_s": cut_in(record),
        "glitches": record.glitches,
        "live_sockets": [s.got for s in record.sockets],
    }


__all__ = ["Rig", "Record", "check", "report", "no_devices", "deque", "threading"]
