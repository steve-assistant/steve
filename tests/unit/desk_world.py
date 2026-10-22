"""A world for the desk to run in: a room, a laptop and GPT-Live, on a virtual clock.

The desk's failures on 9 October 2026 were all in how its parts met --
the local voice and GPT-Live answering one turn, a call that could not
close because GPT-Live streams silent frames, a watchdog hanging up as the user
finished speaking -- and every unit test had passed, because each fake
behaved as its author assumed: the fake GPT-Live sent audio only when it
spoke. So this runs the real engine (voice/engine.py), the real ear
(voice/listen.Ear), the real floor (voice/floor.py), the real routing and
hand-over (agent/worker/desk.py) and the real live client
(voice/desk_call.py), against fakes that behave like the real things:

- **The room** is a script of what the user says, when, and what Whisper makes of
  it (`Say`), and what else the laptop plays (`Video`). It is rendered into
  100 ms blocks of microphone and loopback audio with markers the fake wake
  detector and voice detector read, as tests/gate/test_desk.py's do.
- **The acoustics**: what the desk plays comes back through the microphone,
  less the canceller (`residual`), and Whisper writes down what was in a
  capture -- their words, and the desk's own if enough of it leaked
  (`echo_words`).
- **GPT-Live** sends a frame every 100 ms, always: silence when it is not
  talking (zeros, or a faint noise floor), speech with its transcript when
  it is. It does what its instructions say -- told to say a line, it says
  it -- delegates a few seconds after the user stops, and says what comes back.
- **Time** is the event loop's own (`VirtualLoop`): `asyncio.sleep` and every
  timeout run on it, so a two-minute scenario takes milliseconds and never
  depends on the machine's load.

No microphone, no speaker, no model, no network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import selectors
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from voice.stt import Transcript
from voice.tts import Speech

SR = 16_000
STEP = 0.1
BLOCK = int(SR * STEP)
WAKE = 0.4321  # a block starting with this is "hey Steve" to the fake detector
NEAR = 0.1234  # ...and this, a "hey Steve" it scores under any bar (0.6)
# ...and this, a "hey Steve" the echo canceller ducked under the desk's own
# voice: 0.75, over the bar while it talks (voice/listen.WAKE_OVER_VOICE),
# under the usual one.
MUFFLED = 0.2468
TALK = 0.2
QUIET = 0.001


# ---------------------------------------------------------------------------
# Virtual time
# ---------------------------------------------------------------------------


class _Selector(selectors.SelectSelector):
    """A selector that never waits: when nothing is ready, the loop's clock
    jumps to the next timer instead."""

    loop: VirtualLoop | None = None

    def select(self, timeout=None):
        ready = super().select(0)
        if not ready and timeout and self.loop is not None:
            self.loop._now += timeout
        return ready


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        selector = _Selector()
        super().__init__(selector)
        selector.loop = self
        self._now = 1000.0

    def time(self) -> float:
        return self._now


def run(main):
    """Run `main()` -- a coroutine function -- on virtual time."""
    loop = VirtualLoop()
    try:
        return loop.run_until_complete(main())
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()


async def inline(fn, *args):
    """Blocking work, done at once: the simulator has no threads to wait for."""
    return fn(*args)


# ---------------------------------------------------------------------------
# The room
# ---------------------------------------------------------------------------


@dataclass
class Say:
    """Something the user says. `at` is seconds into the scenario. `wake`: it
    starts with "hey Steve" and the detector hears it; `near`: the detector
    scores it just under its line. `heard_as` is what Whisper makes of it,
    if not its words."""

    at: float
    text: str
    wake: bool = False
    near: bool = False
    muffled: bool = False
    heard_as: str | None = None
    secs: float | None = None

    @property
    def end(self) -> float:
        return self.at + (self.secs if self.secs is not None
                          else max(0.8, 0.32 * len(self.text.split())))


@dataclass
class Video:
    """Speech the laptop plays: a video, a call."""

    at: float
    secs: float
    text: str = "and that is the story of the quarter"
    wake: bool = False


# ---------------------------------------------------------------------------
# Fakes of the parts
# ---------------------------------------------------------------------------


class Detector:
    """Scores a block by its marker, and fires at its bar (`threshold`,
    which the ear lowers while the desk's voice is audible)."""

    threshold = 0.9

    def __init__(self) -> None:
        self.last_score = 0.0

    def push(self, audio):
        first = np.float32(audio[0]) if len(audio) else np.float32(0.0)
        score = (0.95 if first == np.float32(WAKE) else 0.6 if first == np.float32(NEAR)
                 else 0.75 if first == np.float32(MUFFLED) else 0.0)
        self.last_score = score
        return score if score >= self.threshold else None

    def reset(self) -> None:
        pass


class Vad:
    """Speech is a 32 ms step louder than room tone."""

    STEP = 512

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._pending = np.zeros(0, np.float32)

    def push(self, audio):
        self._pending = np.concatenate([self._pending, np.asarray(audio, np.float32)])
        out = []
        while len(self._pending) >= self.STEP:
            step, self._pending = self._pending[:self.STEP], self._pending[self.STEP:]
            out.append(float(np.sqrt((step ** 2).mean())) > 0.05)
        return out


class Player:
    """The speakers: a queue played 100 ms at a time on the world's clock,
    telling the floor what each piece was (voice/audio.Player.on_block)."""

    device_rate = 24_000
    running = True  # the stream says it is running, stalled or not

    def __init__(self, clock=None) -> None:
        self.chunks: deque = deque()  # [audio, owner]
        self.on_block = None
        self.played: list[tuple[float, object, float]] = []  # (when, owner, rms)
        self.clock = clock
        self.last_callback = 0.0  # when the stream last asked for sound

    def enqueue(self, audio, rate, owner=None):
        audio = np.asarray(audio, np.float32)
        if rate != self.device_rate and len(audio):
            n = int(len(audio) * self.device_rate / rate)
            audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)),
                              audio).astype(np.float32)
        self.chunks.append([audio, owner])

    def take(self):
        chunks = [c[0] for c in self.chunks]
        self.chunks.clear()
        return chunks

    def stop(self):
        return bool(self.take())

    @property
    def is_playing(self) -> bool:
        return bool(self.chunks)

    @property
    def queued_seconds(self) -> float:
        return sum(len(c[0]) for c in self.chunks) / self.device_rate

    def advance(self, now: float) -> list[tuple[object, float]]:
        """Play the next STEP; returns (owner, rms) for each piece played."""
        self.last_callback = now
        want = int(STEP * self.device_rate)
        out = []
        while want > 0 and self.chunks:
            audio, owner = self.chunks[0]
            piece, rest = audio[:want], audio[want:]
            want -= len(piece)
            if len(rest):
                self.chunks[0][0] = rest
            else:
                self.chunks.popleft()
            if not len(piece):
                continue
            rms = float(np.sqrt(np.mean(np.square(piece))))
            out.append((owner, rms))
            self.played.append((now, owner, rms))
            if self.on_block is not None:
                self.on_block(owner, rms, len(piece) / self.device_rate)
        return out


class Speaker:
    """Kokoro: speech as long as the text takes to say, about 15 characters
    a second."""

    def say(self, text: str) -> Speech:
        n = max(1, int(len(text) / 15.0 * 24_000))
        return Speech(audio=np.full(n, 0.1, np.float32), sample_rate=24_000, generate_ms=0,
                      device="sim")


@dataclass
class Reply:
    """What their assistant answers to a request matching `match`."""

    match: str
    text: str
    delay: float = 1.5
    tool: str | None = None
    live: bool = False  # it asks for a live conversation (decision A)
    notes: str | None = None  # it starts notes of the room, as `take_notes` does


# ---------------------------------------------------------------------------
# GPT-Live
# ---------------------------------------------------------------------------


class Live:
    """GPT-Live, as it behaves: a frame every 100 ms whether it is talking or
    not; its transcript paced with its speech; turn-taking of its own; and
    instructions followed. `first_response_s`: how long after the user stops it
    waits before answering or delegating; None, never. `noise`: the level
    of its silent frames."""

    def __init__(self, world: World, *, first_response_s: float | None = 2.0,
                 noise: float = 0.0, delegate: bool = True, close_delay: float = 0.0) -> None:
        self.world = world
        self.first_response_s = first_response_s
        self.noise = noise
        self.delegate = delegate
        # How long it takes to say `session.closed` after being asked to close.
        self.close_delay = close_delay
        self.events: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.history: list[dict] = []
        self.instructions = ""
        self.started = False
        self.closed = False
        self.speaking: deque = deque()  # words still to say
        self.said: list[str] = []
        self.user_until = float("-inf")  # when the speech it last heard ended
        self.answered_to = float("-inf")
        self.delegations = 0
        self.heard_during_its_voice = 0  # loud input samples while it talked
        self.muted = False

    # The socket ----------------------------------------------------------

    async def send(self, text: str) -> None:
        event = json.loads(text)
        self.sent.append(event)
        kind = event.get("type")
        if kind == "session.start":
            session = event["session"]
            self.instructions = session.get("instructions", "")
            self.history = list(session.get("input", []))
            self.started = True
            self._push({"type": "session.started"})
            self._obey(self.instructions)
        elif kind == "session.instructions.append":
            self._obey(event.get("content", ""))
        elif kind == "session.commentary.append":
            self._say(event.get("content", ""), delay=0.7)
        elif kind == "session.input_audio.mute":
            self.muted = True
        elif kind == "session.input_audio.unmute":
            self.muted = False
        elif kind == "session.input_audio.append":
            pcm = np.frombuffer(base64.b64decode(event["audio"]), dtype="<i2")
            if self.speaking and len(pcm) and np.abs(pcm).max() > 1000:
                self.heard_during_its_voice += 1
        elif kind == "session.close":
            def closed() -> None:
                self.closed = True
                self._push({"type": "session.closed", "reason": "close_requested",
                            "usage": {"seconds": self.world.now - self.world.live_opened}})

            if self.close_delay:
                self.world.later(self.close_delay, closed)
            else:
                closed()

    async def recv(self):
        return json.dumps(await self.events.get())

    async def close(self):
        self.closed = True
        self.events.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)

    def _push(self, event: dict) -> None:
        self.events.put_nowait(event)

    # Its behaviour ---------------------------------------------------------

    def _obey(self, text: str) -> None:
        """Told to say a line, it says it -- which is how the opening was
        said twice on 9 October 2026. Told to say now that it is listening,
        it does. Told to answer their last message, it answers -- unless that
        message was only "let's talk live": with nothing to answer, and its
        standing instructions never to fill a silence with "I'm here", the
        real one said nothing for eight seconds, nudge and all (17:29:22)."""
        text = text or ""
        match = re.search(r"(?:in close to these words|saying this[^:]*):\s*(.+)$", text, re.S)
        if match:
            self._say(match.group(1).strip(), delay=0.5)
        elif "Say now" in text:
            self._say("Sure, I'm listening. What's on your mind?", delay=0.8)
        elif (("Answer it now" in text or "Answer her last message now" in text)
              and not self._nothing_to_answer()):
            self.user_until = max(self.user_until, self.world.now)

    def _nothing_to_answer(self) -> bool:
        from agent.worker.desk import live_asked

        mine = [item for item in self.history if item.get("role") == "user"]
        last = mine[-1]["content"][0]["text"] if mine else ""
        return live_asked(last) == "" and self.user_until == float("-inf")

    def _say(self, text: str, *, delay: float = 0.0) -> None:
        self.world.later(delay, lambda: self.speaking.extend(text.split()))

    def heard(self, start: float, end: float) -> None:
        """The user spoke (it hears everything the microphone sends it)."""
        self.user_until = max(self.user_until, end)

    def step(self, now: float) -> None:
        if not self.started or self.closed:
            return
        talking = bool(self.speaking)
        self._steps = getattr(self, "_steps", 0) + 1
        if talking:
            if self._steps % 3 == 0:  # about three words a second
                text = self.speaking.popleft() + " "
                self.said.append(text)
                self._push({"type": "session.output_transcript.delta", "delta": text,
                            "start_ms": 0, "end_ms": 0, "event_id": f"o{len(self.said)}"})
            frame = np.full(2400, 3000, "<i2")
        else:
            level = int(self.noise * 32767)
            frame = (np.random.default_rng(int(now * 10)).integers(-level, level + 1, 2400)
                     if level else np.zeros(2400)).astype("<i2")
        self._push({"type": "session.output_audio.delta",
                    "delta": base64.b64encode(frame.tobytes()).decode()})
        # Its turn-taking: it answers, or delegates, a while after the user stops.
        if (self.first_response_s is not None and self.user_until > self.answered_to
                and now - self.user_until >= self.first_response_s and not talking):
            self.answered_to = self.user_until
            if self.delegate:
                self.delegations += 1
                self._say("Checking that now.")
                self._push({"type": "session.delegation.created",
                            "delegation": {"id": f"d{self.delegations}", "target": "client",
                                           "type": "delegation"}, "offset_ms": 0,
                            "event_id": f"d{self.delegations}"})
            else:
                self._say("Sure, happy to talk that through.")


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclass
class World:
    """The desk, wired with its real parts and these fakes. Build it inside
    the virtual loop (`build`)."""

    says: list[Say] = field(default_factory=list)
    videos: list[Video] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)
    residual: float = 0.0  # how much of the desk's own sound the canceller leaves
    echo_words: bool = False  # whether Whisper writes down what leaked
    live_behaviour: dict = field(default_factory=dict)
    connect_delay: float = 0.0  # how long GPT-Live takes to answer the connection
    # How long each transcription takes, in order, the last repeated: (2.4,
    # 0.45) is a cold Whisper and then a warm one (9 October 2026 retest).
    # Empty: at once.
    stt_s: tuple = ()
    # The end-of-turn judge (voice/turns.TurnJudge): their chance of having
    # finished, given the capture. None: the timer alone.
    judge: object = None
    # When the speaker stream stops calling back, and for how long: (at,
    # seconds). It says it is running throughout, as a late WASAPI stream
    # does, and plays its queue on when it resumes.
    stalls: list = field(default_factory=list)

    async def build(self, monkeypatch, store) -> World:
        from agent import runtime
        from agent.worker import answering
        from agent.worker import desk as handler
        from voice import engine as engine_mod
        from voice import listen
        from voice.floor import Floor
        from voice.pipeline import VoiceSession

        loop = asyncio.get_running_loop()
        self.loop = loop
        self.t0 = loop.time()
        self.clock = loop.time
        self.player = Player(clock=self.clock)
        self.speaker = Speaker()
        self.asked: list[str] = []
        self.replied: list[str] = []
        self.transcribed: list[str] = []
        self.captures: dict[int, tuple[float, float, str]] = {}
        self.lives: list[Live] = []
        self.live: Live | None = None
        self.live_opened = 0.0
        self.turn_lines: list[str] = []
        self._later: list[tuple[float, object]] = []
        self.events: list[str] = []

        session = VoiceSession(self.reply, transcriber=self, speaker=self.speaker, streams=True)
        session.player = self.player
        session.blocking = inline
        self.session = session
        monkeypatch.setattr(runtime, "voice", session)
        monkeypatch.setattr(runtime, "conversations", {})
        monkeypatch.setattr(answering, "voice_reply", self.reply)
        self.floor = Floor(self.player, clock=self.clock)
        self._stt_calls = 0
        shell = handler.DeskShell(session, clock=self.clock, blocking=self._blocking,
                                  connect=self.connect)
        original_log = shell.log_turn

        def log_turn(turn):
            self.turn_lines.append(turn.outcome)
            original_log(turn)

        shell.log_turn = log_turn
        original_ended = shell.live_ended
        self.live_ends: list[tuple[float, str, str]] = []  # (when, why, unanswered)

        def live_ended(call, reason, *, unanswered):
            self.live_ends.append((call.ended_at - self.t0, reason, unanswered))
            original_ended(call, reason, unanswered=unanswered)

        shell.live_ended = live_ended
        self.engine = engine_mod.Engine(shell, self.floor, clock=self.clock)
        self.loopback = listen.Loopback(Detector, Vad(), clock=self.clock)
        self.ear = listen.Ear(detector=Detector(), loopback=self.loopback, vad=Vad(),
                              on_event=self._heard, clock=self.clock, judge=self.judge)
        monkeypatch.setitem(handler._state, "engine", self.engine)
        monkeypatch.setitem(handler._state, "listener", self.ear)
        monkeypatch.setitem(handler._state, "ears", _Ears())
        monkeypatch.setitem(handler._state, "echo", None)
        self.engine.attach(self.ear)
        self.engine.opened()
        return self

    @property
    def now(self) -> float:
        return self.clock() - self.t0

    def later(self, delay: float, fn) -> None:
        self._later.append((self.now + delay, fn))

    # Whisper -------------------------------------------------------------

    async def _blocking(self, fn, *args):
        """The desk's blocking work: Whisper takes the time `stt_s` says, on
        the world's clock; anything else is done at once."""
        if getattr(fn, "__func__", None) is World.transcribe and self.stt_s:
            n, self._stt_calls = self._stt_calls, self._stt_calls + 1
            await asyncio.sleep(self.stt_s[min(n, len(self.stt_s) - 1)])
        return fn(*args)

    def transcribe(self, audio):
        started, ended, _ = self.captures.get(id(audio), (0.0, 0.0, ""))
        parts = []
        for say in self.says:
            overlap = min(say.end, ended) - max(say.at, started)
            if overlap >= min(0.3, (say.end - say.at) / 2):
                parts.append(say.heard_as if say.heard_as is not None else say.text)
        if self.echo_words:
            own = self.floor.said_between(started + self.t0, ended + self.t0)
            if own:
                parts.append(own)
        text = " ".join(parts).strip()
        self.transcribed.append(text)
        return Transcript(text=text, language="en", duration_s=ended - started,
                          transcribe_ms=1, device="sim")

    def _heard(self, event) -> None:
        if event.heard is not None:
            heard = event.heard
            self.captures[id(heard.audio)] = (heard.started - self.t0, heard.ended - self.t0,
                                              heard.kind)
        self.events.append(event.kind)
        self.engine.ear_event(event)

    # Their assistant -------------------------------------------------------------

    async def reply(self, text, channel="voice", *, on_text=None, on_tool=None,
                    on_restart=None):
        self.asked.append(text)
        from agent import runtime

        turn = runtime.desk_turn.get()
        script = next((r for r in self.replies if re.search(r.match, text, re.I)),
                      Reply(".", "Okay."))
        self.replied.append(script.text)
        if script.tool and on_tool is not None:
            on_tool(script.tool)
        await asyncio.sleep(script.delay)
        if script.live and turn is not None:
            turn.asks["live"] = True
        if script.notes and turn is not None:
            turn.asks["notes"] = script.notes
        if on_text is not None:
            for sentence in re.findall(r"[^.?!]+[.?!]?\s*", script.text):
                on_text(sentence)
                await asyncio.sleep(0.05)
        return script.text, {}

    # GPT-Live -----------------------------------------------------------------

    async def connect(self):
        await asyncio.sleep(self.connect_delay)
        live = Live(self, **self.live_behaviour)
        self.lives.append(live)
        self.live = live
        self.live_opened = self.now
        return live

    # The clock --------------------------------------------------------------

    async def run_until(self, until: float) -> None:
        while self.now < until - 1e-9:
            await asyncio.sleep(STEP)
            self.step()

    def step(self) -> None:
        now = self.now
        start = now - STEP
        due = [fn for at, fn in self._later if at <= now]
        self._later = [(at, fn) for at, fn in self._later if at > now]
        for fn in due:
            fn()
        stalled = any(at <= now < at + secs for at, secs in self.stalls)
        played = [] if stalled else self.player.advance(self.clock())
        loud = max((rms for _, rms in played), default=0.0)
        mic = np.full(BLOCK, QUIET, np.float32)
        loop = np.zeros(BLOCK, np.float32)
        for say in self.says:
            if say.at < now and say.end > start:
                mic = np.full(BLOCK, TALK, np.float32)
                mark = say.at + 0.6
                if (say.wake or say.near or say.muffled) and start < mark <= now:
                    mic[0] = WAKE if say.wake else NEAR if say.near else MUFFLED
                if self.live is not None and not self.live.closed:
                    self.live.heard(say.at, say.end)
        for video in self.videos:
            if video.at < now and video.at + video.secs > start:
                loop = np.full(BLOCK, TALK, np.float32)
                if video.wake and start < video.at + 0.6 <= now:
                    loop[0] = WAKE
                    if mic[0] != np.float32(WAKE):
                        mic = mic.copy()
                        mic[0] = WAKE  # the room hears the laptop say it
        if loud >= 0.003:
            loop = np.maximum(loop, np.full(BLOCK, TALK, np.float32))
            if self.residual > 0:
                mic = np.maximum(mic, np.full(BLOCK, TALK * self.residual, np.float32))
        now_clock = self.clock()
        self.loopback.push(loop, now=now_clock)
        self.ear.push(mic, now=now_clock)
        if self.live is not None:
            self.live.step(now)
        self.engine.tick()
        self.floor.check()

    # What happened ----------------------------------------------------------

    def spoken(self) -> list[str]:
        """Every line the desk queued, any owner, in order -- its own
        voice's sentences and a live conversation's transcript."""
        return [text for _, text, _, _ in self.floor._said]

    def sounded(self, owner_kind: str, since: float, until: float) -> float:
        """Seconds in which an owner of this kind was audible."""
        from voice.floor import AUDIBLE_RMS

        kinds = {"live": "LiveCall", "turn": "Turn"}
        return sum(STEP for at, owner, rms in self.player.played
                   if since + self.t0 <= at <= until + self.t0 and rms >= AUDIBLE_RMS
                   and type(owner).__name__ == kinds.get(owner_kind, owner_kind))


class _Ears:
    """The hardware's face for `agent/worker/desk.status`: open, no device."""

    paused_for = None
    open = True
    device = "sim"
    error = None
