"""Push-to-talk, rehearsed: the key pressed and let go on a clock, the user's
words in between, and OpenAI faked at its two calls.

The room rig (devtools/rehearse/rig.py) rehearses hands-free: a simulated
room, the add-on's Whisper and Kokoro, the wake detector. Push-to-talk needs
none of that, and core holds no local model, so a push-to-talk scenario was
rehearsed nowhere: the one turn every v0.1 user takes. Here the scenario's
`key` lines press and let go of the key, sent as the app sends them, to the
worker's socket (agent/worker/routes.py `ws_session`), and the real
`VoiceSession` -- OpenAI's ear and voice (voice/cloud.py), billed, through
core/hearing.py and core/voicing.py -- runs the turn. Driven on the session
itself, as it was, the rig could not see what the route does on a press and
a release: a microphone left open after the release while the last reply
was still being written passed it (review of steps 7 to 9, findings 1 and
31). What is faked:
- **the microphone** takes `OPEN_S` to open on a press, and hears the words
  of the user's lines said while it is live, each one it was live for at
  least half of: a word begun before the press is lost, as it is on a real
  desk, and a press with nothing said is room tone. Opened at once, it kept
  a word begun at the press by construction;
- **OpenAI's speech-to-text** answers, after `HEAR_S`, with those words;
- **OpenAI's voice** answers, after `SAY_S`, with a tone as long as the line
  would take to say;
- **the speakers** play it on the clock, and are silenced when stopped.

It needs no model, no device and no network, so the unit tests rehearse
every push-to-talk scenario (tests/unit/test_rehearse.py), at the
scenario's own pace. `pace` scales every wait and length, and the seconds
the checks measure back, but not the worker's own time to answer a key: at
a quarter pace, tried to save the suite about 30 seconds, 40 ms to open the
microphone on a busy computer read as 160, over the 150 a scenario allows,
and the first word was lost (CI and the laptop, 22 October 2026).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from devtools.rehearse import fakes
from devtools.rehearse.script import Line, Scenario, compare

# How long OpenAI takes to hear a clip and to make a line, as faked; the
# rehearsal checks the desk adds little to them.
HEAR_S = 0.3
SAY_S = 0.15
# How fast the user talks, and how fast the voice does.
WORD_S = 0.3
CHARS_PER_S = 15.0
# How long the microphone takes to open on a press. A word is heard when the
# microphone was live for at least half of it, so one begun at the press is
# kept while the press to a live microphone takes under half a word: 0.15 s,
# 07-voice.md's bar.
OPEN_S = 0.05
# The checks a push-to-talk scenario may use.
CHECKS = ("asked", "not-asked", "heard", "transcribed", "first-audio", "silenced",
          "mic-live", "mic-shut", "tick-live")


def pushes(scenario: Scenario) -> bool:
    """Whether a scenario is push-to-talk's: it has the key in it."""
    return any(line.who == "key" for line in scenario.lines)


@dataclass
class Said:
    """One of the user's lines, as it was placed: each word's start."""

    line: Line
    words: list[tuple[float, str]]
    word_s: float = WORD_S

    @property
    def end(self) -> float:
        return self.words[-1][0] + self.word_s if self.words else 0.0


@dataclass
class Run:
    """A stretch the speakers played: the tick, or speech."""

    start: float
    end: float
    kind: str


@dataclass
class Result:
    scenario: Scenario
    pace: float = 1.0  # every time of the scenario's, scaled by it
    ts: float = 0.0
    said: list[Said] = field(default_factory=list)
    keys: list[tuple[float, str, bool]] = field(default_factory=list)  # at, press|release, marked
    heard: list[str] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    asked: list = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    # When the microphone went live, and when it closed, on the clock.
    lives: list[float] = field(default_factory=list)
    shuts: list[float] = field(default_factory=list)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


class _Speakers:
    """The speakers, on the clock: what is queued plays after what is already
    playing, and `stop` silences it at once."""

    device_rate = 24_000

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.runs: list[Run] = []
        self._lock = threading.Lock()

    def enqueue(self, samples, rate, owner=None) -> None:
        from voice import audio

        kind = "tick" if samples is audio.TICK[0] else "speech"
        with self._lock:
            now = self.clock()
            start = max([now, *(r.end for r in self.runs)])
            self.runs.append(Run(start, start + len(samples) / rate, kind))

    def _cut(self, now: float) -> bool:
        playing = False
        kept = []
        for run in self.runs:
            if run.start >= now:
                continue  # never played
            if run.end > now:
                playing = playing or run.kind == "speech"
                run.end = now
            kept.append(run)
        self.runs[:] = kept
        return playing

    def stop(self) -> bool:
        with self._lock:
            return self._cut(self.clock())

    @property
    def queued_seconds(self) -> float:
        with self._lock:
            now = self.clock()
            return sum(r.end - max(r.start, now) for r in self.runs if r.end > now)

    @property
    def is_playing(self) -> bool:
        return self.queued_seconds > 0

    def wait(self, timeout: float | None = None) -> bool:
        ends = time.monotonic() + (timeout or 0)
        while self.is_playing and time.monotonic() < ends:
            time.sleep(0.01)
        return not self.is_playing

    def take(self) -> list:
        return []

    def close(self) -> None:
        pass


class _Microphone:
    """Hears the words said while it is live, and nothing else."""

    def __init__(self, result: Result, clock=time.monotonic) -> None:
        self.result, self.clock = result, clock
        self.live: float | None = None
        self.live_ms: int | None = None
        self.recording = False
        self.words: list[str] = []

    def start(self, pressed: float | None = None) -> None:
        """Open, which takes `OPEN_S` and holds up whoever called, as a real
        stream's start does. Live is timed from `pressed`, the press's
        `time.perf_counter()`, as voice/audio.py's `Recorder` times it."""
        began = time.perf_counter()
        time.sleep(OPEN_S * self.result.pace)
        self.live, self.recording = self.clock(), True
        self.live_ms = round((time.perf_counter() - (pressed or began)) * 1000)
        self.result.lives.append(self.live)

    def stop(self):
        from voice import audio

        released = self.clock()
        live = self.live if self.live is not None else released
        self.live, self.recording = None, False
        self.result.shuts.append(released)
        half = WORD_S * self.result.pace / 2
        self.words = [word for said in self.result.said for at, word in said.words
                      if at + half >= live and at < released]
        n = int(max(0.0, released - live) * audio.STT_SAMPLE_RATE)
        level = 0.3 if self.words else 0.001
        samples = (level * np.sin(np.arange(n) / 8)).astype(np.float32)
        return audio.Recording(audio=samples, live_ms=self.live_ms)


class _OpenAI:
    """OpenAI's two speech calls, faked where the fences refuse them."""

    def __init__(self, mic: _Microphone, result: Result) -> None:
        self.mic, self.result = mic, result

    def hear(self, name: str, data: bytes, key: str) -> dict:
        time.sleep(HEAR_S * self.result.pace)
        text = " ".join(self.mic.words)
        self.result.heard.append(text)
        return {"text": text, "usage": {"type": "duration", "seconds": 1.0}}

    def say(self, text: str, voice: str, speed: float, key: str) -> bytes:
        from core import voicing

        time.sleep(SAY_S * self.result.pace)
        self.result.spoken.append(text)
        seconds = max(0.3, len(text) / CHARS_PER_S) * self.result.pace
        samples = (0.2 * np.sin(np.arange(int(seconds * voicing.SAMPLE_RATE)) / 5) * 32767)
        return samples.astype("<i2").tobytes()


# What the socket sends when a release's turn is over.
ANSWERS = ("voice_turn", "voice_empty", "voice_unavailable", "error")


class _Socket:
    """The app's end of the worker's socket: what it sends, and each thing
    it is sent. On the rehearsal's own loop, so the turn bills where the
    rehearsal does (`spend.spending`), which a test client's thread would
    not."""

    scope: dict = {"subprotocols": []}

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict] = []

    def send(self, kind: str) -> None:
        self.inbox.put_nowait(json.dumps({"type": kind}))

    def answers(self) -> list[dict]:
        return [m for m in self.sent if m["type"] in ANSWERS]

    async def accept(self, subprotocol: str | None = None) -> None:
        pass

    async def receive_text(self) -> str:
        text = await self.inbox.get()
        if text is None:
            from starlette.websockets import WebSocketDisconnect

            raise WebSocketDisconnect(1000)
        return text

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        pass


async def rehearse(scenario: Scenario, *, tmp, pace: float = 1.0) -> Result:
    """Run one push-to-talk scenario, and check it. `tmp` is a folder of its
    own, for the billing database and the voice's kept lines; `pace` scales
    the scenario's every time."""
    from dataclasses import replace

    from core import db, hearing, spend, voicing
    from voice import cloud, pipeline
    from voice.speaking import PREAMBLE_LINES

    unknown = [e.check for e in scenario.expects if e.check not in CHECKS]
    if unknown:
        raise ValueError(f"{scenario.name}: push-to-talk has no check {unknown}: {CHECKS}")
    result = Result(scenario=scenario, pace=pace)
    mic = _Microphone(result)
    openai = _OpenAI(mic, result)
    speakers = _Speakers()
    assistant = fakes.Assistant([replace(r, delay=r.delay * pace) for r in scenario.replies],
                                sentence_s=0.15 * pace)
    from core import secrets

    real = hearing._call, voicing._call, secrets.get
    hearing._call, voicing._call = openai.hear, openai.say
    # A stand-in for the key, so the user's own is never read for a rehearsal.
    secrets.get = lambda name, default=None: "a rehearsal's key"
    conn = db.open_store(tmp / "rehearsal.db")
    socket = _Socket()
    worker = _Worker()
    ours = asyncio.all_tasks()
    releases = 0
    try:
        session = pipeline.VoiceSession(
            assistant.reply, streams=True,
            speaker=cloud.CloudSpeaker(kept=(*pipeline.STALLS, *PREAMBLE_LINES),
                                       lines_dir=tmp / "lines"))
        session.recorder, session.player = mic, speakers
        with spend.spending("chat", conn=conn):
            await asyncio.to_thread(session.warm)
            speakers.runs.clear()
            result.spoken.clear()
            worker.serve(session, socket)
            result.ts = time.monotonic()
            previous = result.ts
            for line in scenario.lines:
                at = await _when(line, previous, result, speakers)
                await asyncio.sleep(max(0.0, at - time.monotonic()))
                if line.who == "key":
                    at = time.monotonic()
                    result.keys.append((at, line.text, line.mark))
                    socket.send("voice_start" if line.text == "press" else "voice_stop")
                    releases += line.text == "release"
                    previous = at
                else:
                    # From when it was due, not from when this loop came to
                    # it: the microphone's open holds the loop up, as it
                    # holds up the worker's, and the user talks on meanwhile.
                    said = Said(line, [(at + i * WORD_S * pace, word)
                                       for i, word in enumerate(line.text.split())],
                                WORD_S * pace)
                    result.said.append(said)
                    previous = said.end
            ends = result.ts + scenario.length * pace
            while time.monotonic() < ends and not (
                    len(socket.answers()) >= releases and not speakers.is_playing):
                await asyncio.sleep(0.05)
    finally:
        socket.inbox.put_nowait(None)
        # The socket's turns and the desk's engine, cut off at the scenario's
        # end if still going, as the old rig cancelled its turns.
        left = asyncio.all_tasks() - ours
        for task in left:
            task.cancel()
        await asyncio.gather(*left, return_exceptions=True)
        worker.restore()
        hearing._call, voicing._call, secrets.get = real
        conn.close()
    result.asked = list(assistant.asked)
    result.runs = list(speakers.runs)
    result.turns = [{k: v for k, v in m.items() if k != "type"} for m in socket.answers()]
    check(result)
    return result


class _Worker:
    """The worker's state its socket reads, set for a rehearsal and put back:
    the warm session, no listeners or turns, and no desk engine yet, which
    the first turn makes over the session's speakers (agent/worker/desk.py
    `engine`), as the worker's does."""

    def __init__(self) -> None:
        from agent import runtime
        from agent.worker import desk

        self.was = (runtime.voice, runtime.listeners, runtime.turning,
                    desk._state.get("engine"), desk._state.get("ticker"))

    def serve(self, session, socket: _Socket) -> None:
        from agent import runtime
        from agent.worker import desk, routes

        runtime.voice, runtime.listeners, runtime.turning = session, set(), set()
        desk._state.update(engine=None, ticker=None)
        asyncio.ensure_future(routes.ws_session(socket))

    def restore(self) -> None:
        from agent import runtime
        from agent.worker import desk

        (runtime.voice, runtime.listeners, runtime.turning,
         desk._state["engine"], desk._state["ticker"]) = self.was


async def _when(line: Line, previous: float, result: Result, speakers: _Speakers) -> float:
    when = line.when
    offset = when.offset * result.pace
    if when.anchor is None:
        return result.ts + offset
    if when.anchor == "+":
        return previous + offset
    if when.anchor not in ("voice", "quiet"):
        raise ValueError(f"push-to-talk has no anchor {when.anchor!r}: voice, quiet")
    while True:
        speech = [r for r in list(speakers.runs) if r.kind == "speech" and r.end > previous]
        if when.anchor == "voice" and speech and speech[0].start <= time.monotonic():
            return max(previous, speech[0].start) + offset
        if when.anchor == "quiet" and speech:
            last = max(r.end for r in speech)
            if time.monotonic() - last >= 0.5 * result.pace:
                return last + offset
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# What happened, checked
# ---------------------------------------------------------------------------

def _marked(result: Result, kind: str) -> float | None:
    return next((at for at, what, mark in result.keys if mark and what == kind), None)


def first_audio(result: Result) -> float | None:
    """From the marked release to the reply's first sound."""
    released = _marked(result, "release")
    if released is None:
        return None
    runs = [r for r in result.runs if r.kind == "speech" and r.start >= released]
    return (runs[0].start - released) / result.pace if runs else None


def silenced(result: Result) -> float | None:
    """From the marked press to the silence of the reply it was made over."""
    pressed = _marked(result, "press")
    if pressed is None:
        return None
    over = [r for r in result.runs if r.kind == "speech" and r.start < pressed]
    if not over or max(r.end for r in over) < pressed:
        return None  # nothing was speaking: no barge-in to measure
    return (max(r.end for r in over) - pressed) / result.pace


def mic_live(result: Result) -> float | None:
    """From a press to its live microphone, the slowest of them, as the
    turns report it: the worker's own clock, started at the press."""
    lives = [t["timing"]["live_ms"] for t in result.turns
             if (t.get("timing") or {}).get("live_ms") is not None]
    return max(lives) / 1000 / result.pace if lives else None


def mic_shut(result: Result) -> float | None:
    """From the marked release to the microphone closed."""
    released = _marked(result, "release")
    if released is None:
        return None
    shut = next((at for at in sorted(result.shuts) if at >= released), None)
    return None if shut is None else (shut - released) / result.pace


def tick_live(result: Result) -> tuple[bool, str]:
    """Each press's tick sounded once its microphone was live: a tick before
    it told the user to talk into a microphone not yet open."""
    ticks = sorted(r.start for r in result.runs if r.kind == "tick")
    lives = sorted(result.lives)
    pairs = list(zip(ticks, lives, strict=False))  # unequal counts fail below
    ok = len(ticks) == len(lives) and all(tick >= live for tick, live in pairs)
    early = [round((live - tick) / result.pace, 3) for tick, live in pairs]
    return ok, f"{len(ticks)} ticks for {len(lives)} opens; before live by {early} s"


def check(result: Result) -> None:
    out = result.checks
    asked = [text for _, text in result.asked]
    for expect in result.scenario.expects:
        name, args = expect.check, expect.args
        label = f"{name} {' '.join(str(a) for a in args)}".strip()
        if name == "asked":
            out.append((label, any(re.search(args[0], t, re.I) for t in asked), f"asked: {asked}"))
        elif name == "not-asked":
            out.append((label, not asked, f"asked: {asked}"))
        elif name == "heard":
            out.append((label, any(re.search(args[0], t, re.I) for t in result.heard),
                        f"heard: {result.heard}"))
        elif name == "transcribed":
            out.append((label, compare(len(result.heard), *args), f"{len(result.heard)} heard"))
        elif name == "tick-live":
            out.append((label, *tick_live(result)))
        else:
            value = {"first-audio": first_audio, "silenced": silenced, "mic-live": mic_live,
                     "mic-shut": mic_shut}[name](result)
            out.append((label, compare(value, *args),
                        "never" if value is None else f"{value:.2f} s"))


def report(result: Result) -> str:
    sc = result.scenario
    lines = [f"== {sc.name}: {sc.about}", ""]
    events = [(at, f"key {what}{'!' if mark else ''}") for at, what, mark in result.keys]
    events += [(s.words[0][0], f"user: {s.line.text}") for s in result.said if s.words]
    events += [(r.start, f"desk {r.kind} for {r.end - r.start:.2f} s") for r in result.runs]
    for at, text in sorted(events):
        lines.append(f"  {at - result.ts:6.2f}  {text}")
    lines.append(f"  heard: {result.heard}")
    lines.append("")
    for label, ok, detail in result.checks:
        lines.append(f"  {'PASS' if ok else 'FAIL'}  {label}  ({detail})")
    return "\n".join(lines)


def summary(result: Result) -> dict:
    return {
        "scenario": result.scenario.name, "passed": result.passed,
        "checks": [{"check": c, "ok": ok, "detail": d} for c, ok, d in result.checks],
        "heard": result.heard, "turns": result.turns,
        "first_audio_s": first_audio(result), "silenced_s": silenced(result),
        "mic_live_s": mic_live(result), "mic_shut_s": mic_shut(result),
    }
