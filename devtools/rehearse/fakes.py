"""The assistant and GPT-Live, scripted, in real time.

A rehearsal must never call a model or let a tool near the user's mail or
calendar: it runs often, by hand, beside the live agent. So the assistant
is a script (`Assistant`): an answer for each request it recognises, after
a delay as long as the real one's first words take (1.5 s for the weather,
3 s and a lookup's preamble for a history), streamed a sentence at a time
as the model streams them -- the desk speaks a reply as it is written, and
a fake that handed over the whole text at once would hide every bug of
that path.

GPT-Live (`Live`) behaves as the real one did on 9 and 10 October 2026, as
far as the record shows: a frame every 100 ms whether it is talking or not
(silence as zeros, or faint noise), its transcript paced with its speech,
its own turn-taking -- it stops when it hears speech over it, and answers a
while after the speech stops -- and its instructions obeyed: told to say it
is listening, it says so. Its voice is Kokoro's, in a voice unlike the
desk's, so the echo canceller has real speech to cancel.

Neither fake decides anything the desk decides; they only answer.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
from collections.abc import Callable

import numpy as np

from devtools.rehearse.script import Reply

LIVE_RATE = 24_000
FRAME = 2400  # 100 ms at 24 kHz, as GPT-Live sends them
GREETING = "Sure, I'm listening. What's on your mind?"
FILLER = "Mm-hm."
GOODBYE = "I have to stop here. Say hey Steve to start again."


class Assistant:
    """The assistant, as the desk's `answering.voice_reply` is called."""

    def __init__(self, replies: list[Reply], *, clock: Callable[[], float] = time.monotonic,
                 sentence_s: float = 0.15) -> None:
        self.replies = replies
        self.clock = clock
        self.sentence_s = sentence_s
        self.asked: list[tuple[float, str]] = []

    def answer_for(self, text: str) -> Reply:
        return next((r for r in self.replies if re.search(r.match, text, re.I)),
                    Reply(".", 1.2, None, "Okay."))

    async def reply(self, text: str, channel: str = "voice", *, on_text=None, on_tool=None,
                    on_restart=None) -> tuple[str, dict]:
        self.asked.append((self.clock(), text))
        script = self.answer_for(text)
        if script.tool and on_tool is not None:
            on_tool(script.tool)
        await asyncio.sleep(script.delay)
        if on_text is not None:
            for sentence in re.findall(r"[^.?!]+[.?!]?\s*", script.text):
                on_text(sentence)
                await asyncio.sleep(self.sentence_s)
        return script.text, {}


class Live:
    """GPT-Live's socket, faked, in real time. `speech(text)` makes its voice
    (24 kHz); `says` is what it answers, in order, each time the user has spoken.

    `first_s`: how long after the user's speech ends it answers. `noise_db`: the
    level of its silent frames (None: zeros). `vad_db`: how loud its input
    must be, for 200 ms, to count as speech -- its own turn-taking, which
    stops it talking (`Let them interrupt, and stop when they do`)."""

    def __init__(self, speech: Callable[[str], np.ndarray], *, says: list[str] | None = None,
                 first_s: float = 1.0, noise_db: float | None = None, vad_db: float = -42.0,
                 delegate: bool = False, clock: Callable[[], float] = time.monotonic) -> None:
        self.speech = speech
        self.says = list(says or [])
        self.first_s = first_s
        self.noise = None if noise_db is None else 10 ** (noise_db / 20)
        self.vad = 10 ** (vad_db / 20)
        self.delegate = delegate
        self.clock = clock
        self.events: asyncio.Queue = asyncio.Queue()
        self.sent: list[tuple[float, str]] = []
        self.started_at: float | None = None
        self.closed = False
        self.muted = False
        self._talk: list[np.ndarray] = []  # frames still to send
        self._words: list[tuple[int, str]] = []  # (frame index, word) still to transcribe
        self._frame = 0
        self._user_frames = 0   # consecutive loud input frames
        self._quiet_frames = 0
        self._user_turn = False  # the user is speaking, by its own detector
        self._owed_at: float | None = None  # when it answers the user
        self._task: asyncio.Task | None = None
        self.said: list[tuple[float, str]] = []
        self.interrupted = 0
        self.input_s = 0.0
        self.loud_input_s = 0.0
        self.delegations = 0

    # -- the socket ----------------------------------------------------------

    async def send(self, text: str) -> None:
        event = json.loads(text)
        kind = str(event.get("type"))
        self.sent.append((self.clock(), kind))
        if kind == "session.start":
            self.started_at = self.clock()
            self._push({"type": "session.started"})
            self._task = asyncio.ensure_future(self._run())
        elif kind == "session.instructions.append":
            self._obey(str(event.get("content") or ""))
        elif kind == "session.commentary.append":
            self._say_later(str(event.get("content") or ""), 0.7)
        elif kind == "session.input_audio.mute":
            self.muted = True
        elif kind == "session.input_audio.unmute":
            self.muted = False
        elif kind == "session.input_audio.append":
            self._hear(event.get("audio") or "")
        elif kind == "session.close":
            self._close()

    async def recv(self) -> str:
        return json.dumps(await self.events.get())

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)

    async def close(self) -> None:
        self._close(quietly=True)

    def _push(self, event: dict | None) -> None:
        self.events.put_nowait(event)

    def _close(self, quietly: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        if self._task is not None:
            self._task.cancel()
        if not quietly:
            seconds = self.clock() - (self.started_at or self.clock())
            self._push({"type": "session.closed", "reason": "close_requested",
                        "usage": {"seconds": seconds}})
        self._push(None)

    # -- what it does ----------------------------------------------------------

    def _obey(self, text: str) -> None:
        if "Say now" in text:  # the opener, and its nudge, which repeats it
            self._say_later(GREETING, 0.8)
        elif "Answer it now" in text or "Answer their last message now" in text:
            self._say_later(self._next_answer(), self.first_s)
        elif "time limit" in text:
            self._say_later(GOODBYE, 0.5)

    def _next_answer(self) -> str:
        return self.says.pop(0) if self.says else FILLER

    def _say_later(self, text: str, delay: float) -> None:
        """Say `text` after `delay`. Its voice is made off the event loop
        (Kokoro takes a moment for a line it has not made before)."""
        async def later() -> None:
            started = time.monotonic()
            audio = await asyncio.to_thread(self.speech, text)
            await asyncio.sleep(max(0.0, delay - (time.monotonic() - started)))
            if not self.closed:
                self._say(text, audio)

        asyncio.ensure_future(later())

    def _say(self, text: str, audio: np.ndarray) -> None:
        audio = np.asarray(audio, np.float32)
        frames = [audio[i:i + FRAME] for i in range(0, len(audio), FRAME)]
        if frames and len(frames[-1]) < FRAME:
            frames[-1] = np.pad(frames[-1], (0, FRAME - len(frames[-1])))
        start = self._frame + len(self._talk)
        words = text.split()
        for i, word in enumerate(words):
            self._words.append((start + int(i * len(frames) / max(1, len(words))), word))
        self._talk.extend(frames)
        self.said.append((self.clock(), text))

    def _hear(self, payload: str) -> None:
        """Its input: the user's microphone, cleaned. Its own turn-taking reads it."""
        if not payload or self.closed:
            return
        pcm = np.frombuffer(base64.b64decode(payload), dtype="<i2").astype(np.float32) / 32768
        seconds = len(pcm) / LIVE_RATE
        self.input_s += seconds
        loud = (not self.muted and len(pcm)
                and float(np.sqrt(np.mean(pcm * pcm))) >= self.vad)
        if loud:
            self.loud_input_s += seconds
            self._user_frames += 1
            self._quiet_frames = 0
        else:
            self._user_frames = 0
            self._quiet_frames += 1
        if loud and self._user_frames * seconds >= 0.2 and not self._user_turn:
            self._user_turn = True
            self._owed_at = None
            if self._talk:
                # Talked over: it stops, as it is told to.
                self.interrupted += 1
                self._talk.clear()
                self._words.clear()
        if self._user_turn and not loud and self._quiet_frames * seconds >= 0.6:
            self._user_turn = False
            self._owed_at = self.clock() + self.first_s

    async def _run(self) -> None:
        """A frame every 100 ms, on the wall clock."""
        due = time.monotonic()
        while not self.closed:
            due += FRAME / LIVE_RATE
            await asyncio.sleep(max(0.0, due - time.monotonic()))
            self._tick()

    def _tick(self) -> None:
        if self._owed_at is not None and self.clock() >= self._owed_at and not self._talk:
            self._owed_at = None
            if self.delegate:
                self.delegations += 1
                self._say_later("Checking that now.", 0.0)
                self._push({"type": "session.delegation.created",
                            "delegation": {"id": f"d{self.delegations}", "type": "delegation",
                                           "target": "client"}})
            else:
                self._say_later(self._next_answer(), 0.0)
        if self._talk:
            frame = self._talk.pop(0)
        elif self.noise:
            frame = (self.noise * np.random.default_rng(self._frame).standard_normal(FRAME))
        else:
            frame = np.zeros(FRAME, np.float32)
        while self._words and self._words[0][0] <= self._frame:
            _, word = self._words.pop(0)
            self._push({"type": "session.output_transcript.delta", "delta": word + " "})
        pcm = (np.clip(frame, -1.0, 1.0) * 32767).astype("<i2")
        self._push({"type": "session.output_audio.delta",
                    "delta": base64.b64encode(pcm.tobytes()).decode()})
        self._frame += 1


class SocketTap:
    """The real GPT-Live's socket, with every event's kind, time and -- for its
    audio -- loudness kept, for measuring what the record could not say:
    whether its silent frames are zeros, and how it takes turns. Never its
    words (only their length) and never its audio itself."""

    def __init__(self, inner, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.inner = inner
        self.clock = clock
        self.got: list[dict] = []
        self.sent: list[dict] = []
        self._iter = None

    def _note(self, raw) -> None:
        with contextlib.suppress(Exception):
            event = json.loads(raw)
            kind = str(event.get("type"))
            note = {"at": self.clock(), "type": kind}
            if kind == "session.output_audio.delta":
                pcm = np.frombuffer(base64.b64decode(event.get("delta") or ""),
                                    dtype="<i2").astype(np.float32) / 32768
                note.update(n=len(pcm), rms=float(np.sqrt(np.mean(pcm * pcm))) if len(pcm)
                            else 0.0, peak=float(np.abs(pcm).max()) if len(pcm) else 0.0)
            elif kind.endswith("transcript.delta"):
                note.update(chars=len(str(event.get("delta") or "")),
                            start_ms=event.get("start_ms"), end_ms=event.get("end_ms"))
            elif kind == "session.closed":
                note.update(reason=event.get("reason"),
                            seconds=(event.get("usage") or {}).get("seconds"))
            elif kind == "error":
                note.update(error=str(event.get("error"))[:200])
            self.got.append(note)

    async def send(self, text: str) -> None:
        with contextlib.suppress(Exception):
            self.sent.append({"at": self.clock(), "type": str(json.loads(text).get("type"))})
        await self.inner.send(text)

    async def recv(self):
        raw = await self.inner.recv()
        self._note(raw)
        return raw

    def __aiter__(self):
        self._iter = self.inner.__aiter__()
        return self

    async def __anext__(self):
        raw = await self._iter.__anext__()
        self._note(raw)
        return raw

    async def close(self) -> None:
        await self.inner.close()
