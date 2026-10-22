"""A live conversation at the desk on GPT-Live: opened when the user asks for one.

The maintainer, 5 October 2026, wanted GPT-Live to take over "automatically if I do
anything requiring a back-and-forth". On 9 October 2026 the user chose otherwise,
after two sound tests in which the hand-over between the local voice and
GPT-Live failed every way it could (docs/desk-voice-design.md):

- the local voice said its reply, and GPT-Live, told to open with the same
  words, said it again;
- the phone's opening watchdog hung up nine seconds in, as the user finished their
  first sentence, and their words were lost;
- its stream of silent audio frames was taken for speech, so the call
  could not close;
- "hey Steve" inside the call only paused it.

So the back-and-forth is the local follow-up window, and this opens only
when the user asks for it by name ("let's talk live": `voice/engine.py`). It is
the desk's own client, not the phone's call with parts swapped. It shares
what was the same (`voice/live.py`) -- the connection, the voice, their own
words as the only thing handed over, one hand-over at a time -- and nothing
else:

- **History, not a script.** What the user and their assistant just said goes in
  as the session's opening history (`session.input`, the Live API's
  "ordered text-only history supplied before startup"), never as words for
  it to say: nothing the local voice said is said twice.
- **Their words, by local Whisper.** A delegation carries no text; what is
  handed to their assistant is what the desk's own Whisper heard of their
  stretches of speech since the last hand-over (`Words`) -- never GPT-Live's
  transcript of the room, which made "Casey stop" of "hey Steve, stop".
- **Its voice is measured.** Its frames go to the speakers through the
  desk's floor (voice/floor.py), which counts only audible ones; and its own
  transcript, which arrives paced with its speech, is the second witness.
- **The engine owns the end.** Quiet, limits, a wake and the switch are
  `voice/engine.py`'s; this only connects, carries, hands over and closes.

A minute costs $0.05 (`agent.models.LIVE_VOICE`), and only while it is open.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import logging
import re
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable

import numpy as np

from agent.cut import cut
from voice import live
from voice.live import Session, Turn, joined, speakable

log = logging.getLogger("desk")

AUDIO_FORMAT = {"type": "audio/pcm", "rate": 24_000}
RATE = 24_000
IN_RATE = 16_000  # the ear's
# How long a hand-over waits for a stretch of their speech that is still being
# transcribed: GPT-Live delegates about 2 s after the user stops (measured on the
# phone), Whisper takes about 0.4 s.
HANDOFF_WAIT_S = 1.5
# How long after it promises a lookup it has to hand off before the desk does.
WATCHDOG_S = 2.0
# Its own transcript arrives paced with its speech: this long after the last
# of it, it is taken to have stopped talking (the SDK's transcript grouper
# ends an assistant's turn after 2,000 ms without transcript).
TRANSCRIPT_HOLD_S = 2.0
# The most of the desk thread's history it is given, and each message's length.
HISTORY_MESSAGES = 12
HISTORY_CHARS = 600
CLOSE_WAIT_S = 5.0
_HEADPHONES = ("headphone", "headset", "airpods", "buds", "earphone", "hands-free", "earbud")
# Not words: what a transcript of a room makes of noise.
_FILLERS = frozenset({"uh", "um", "er", "erm", "ah", "oh", "mm", "mhm", "hm", "hmm", "huh"})
_GOODBYES = frozenset({"bye", "goodbye", "thanks", "thank", "you", "ok", "okay", "cool",
                       "great", "that", "s", "all", "thats", "it", "for", "now", "cheers"})

INSTRUCTIONS = """You are the voice of the user's personal assistant, talking with them
at their laptop, hands-free. They are at their desk and asked for a live
conversation. You are an AI and they know it.

It is {when} where the user is{place}.

Talk like a capable assistant across a desk: brief, natural and warm, one or
two sentences at a time, no filler. Let them interrupt, and stop when they do.

Your delivery: upbeat and friendly, with lively intonation -- relaxed, never
flat or monotone.

The conversation so far is above: what the user said and what their assistant
said back, aloud. Never repeat what was already said; carry on from it.

You know nothing yourself about their calendar, email, contacts, reminders,
tasks, files, notes, commitments or anything they have told their assistant,
and you cannot do anything yourself. Whenever they ask about any of those, or
ask for anything to be done -- a reminder, an event, a draft, an email, a note,
notes or a transcript of a meeting or call, a lookup -- delegate it straight
away and say only a few words while you wait, about what you are checking
("Checking your calendar.", "Looking that up."), worded differently each
time. Never guess, and never make up a time, a name or a fact.

Answer only the user. If what you hear is background noise, a video or music,
other people talking to each other, or speech not meant for you, say nothing
at all -- not "I'm here", not "take your time". If you could not make out what
they said, ask them to say it again in a few words; never guess at it, and
never hand a request you only half heard to their assistant.

When the answer comes back, tell them in a sentence or two in your own words,
keeping names, times and numbers exactly as given. Never say something was
done unless the answer says it was done. If they answer an approval ("yes" or
"no" and a word), delegate it; you cannot approve anything yourself.

When they are done -- "that's all", "thanks", goodbye -- say goodbye in a few
words."""

ANSWER_NOW = ("The user has just asked you something: it is their last message above. "
              "Answer it now -- delegating, if it needs anything of theirs.")
# Asked for with nothing else to answer ("let's talk live"). Told only to
# answer their last message, on the 9 October 2026 retest (17:29:22) it said
# nothing for eight seconds -- nudged too -- and the user took it for not having
# heard: there was nothing to answer but "let's talk live", and its standing
# instructions say never to fill a silence with "I'm here".
GREET = ("The user has just asked to talk with you live, and is waiting to hear you. Say "
         "now, in a few words, that you're listening and ask what's on their mind. This once "
         "that is not filler: it is how they know you are there.")
NUDGE = "The user is waiting for you. Answer their last message now, in a sentence or two."


def nudge(opener: str) -> str:
    """What it is told when it has said nothing a few seconds in: its opener
    again, more firmly."""
    if opener == GREET:
        return "The user is waiting and has heard nothing from you yet. " + opener
    return NUDGE
WRAP_UP = ("The conversation is about to reach its time limit. Say in one sentence that "
           "you have to stop here, and that they can say hey Steve to start again.")
NOTHING_HEARD = ("Nothing the user said was caught clearly enough to pass on. Ask them to say "
                 "it again, briefly.")
ALREADY_ASKED = "That was already passed to the assistant; its answer is above or on its way."
UNREACHABLE = ("The assistant is not answering right now. Tell the user so in a few words, and "
               "do not guess at what it would have said.")


def duplex_default() -> bool:
    """Full duplex only on headphones, unless the echo canceller works: on the
    speakers it would hear itself."""
    try:
        from voice import audio as audio_io

        out = audio_io.default_device("output")
    except Exception:  # noqa: BLE001 - no device: the safe default
        return False
    return bool(out) and any(word in out.name.lower() for word in _HEADPHONES)


def session_start(*, history: list[tuple[str, str]], now: dt.datetime | None = None,
                  place: str = "") -> dict:
    """The `session.start`: the desk's voice, with the conversation so far as
    the session's own history -- the user's words as theirs, their
    assistant's as its -- and no line to say. `place` is the town the user
    said they live in, or "" when they have not."""
    from agent import cardwords
    from agent.models import LIVE_VOICE

    # No waiting card's word, as for every model (agent/provider.Unworded):
    # one that knew it could say a card of its own around it, aloud.
    waiting = cardwords.words_now()
    items = []
    for who, words in history[-HISTORY_MESSAGES:]:
        words = cut(cardwords.mask(str(words or "").strip(), waiting), HISTORY_CHARS,
                    sentence=True)
        if not words:
            continue
        mine = who != "user"
        items.append({"type": "message", "role": "assistant" if mine else "user",
                      "content": [{"type": "output_text" if mine else "input_text",
                                   "text": words}]})
    return {
        "type": "session.start",
        "event_id": "start",
        "session": {
            "model": LIVE_VOICE.model_id,
            "instructions": INSTRUCTIONS.format(when=live.when(now),
                                                place=f" ({place})" if place else ""),
            "audio": {"format": AUDIO_FORMAT, "output": {"voice": live.VOICE}},
            "delegation": {"type": "client"},
            "store": False,
            "input": items,
        },
    }


def real_words(text: str) -> list[str]:
    """The words in a transcript that are not a "hmm" -- in any script."""
    return [w for w in re.findall(r"[^\W\d_]+", (text or "").lower()) if w not in _FILLERS]


class Words:
    """Their words in a live conversation, as the desk's own Whisper heard
    them, stretch by stretch: what is handed to their assistant, and what is
    left unanswered when the conversation ends."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.items: list[dict] = []
        self.pending = 0  # stretches still being transcribed
        self.last_word_at = float("-inf")

    def expect(self) -> None:
        self.pending += 1

    def add(self, text: str, start: float, end: float) -> None:
        self.pending = max(0, self.pending - 1)
        text = (text or "").strip()
        if not text:
            return
        self.items.append({"text": text, "start": start, "end": end,
                           "taken": False, "answered": False})
        if real_words(text):
            self.last_word_at = self.clock()

    async def settle(self, timeout: float, sleep) -> None:
        """Wait for stretches still being transcribed, up to `timeout`."""
        deadline = self.clock() + timeout
        while self.pending and self.clock() < deadline:
            await sleep(0.05)

    def untaken(self) -> str:
        return joined([i["text"] for i in self.items if not i["taken"]])

    def take(self) -> tuple[str, float]:
        """What the user said since the last hand-over, and when they finished it."""
        fresh = [i for i in self.items if not i["taken"]]
        for item in fresh:
            item["taken"] = item["answered"] = True
        return joined([i["text"] for i in fresh]), max((i["end"] for i in fresh), default=0.0)

    def answered_before(self, at: float) -> None:
        """It spoke at `at`: what the user said before then has had its answer."""
        for item in self.items:
            if item["end"] <= at:
                item["answered"] = True

    def unanswered(self) -> str:
        """What the user said that nobody answered: real words, not a goodbye."""
        left = [i["text"] for i in self.items if not i["answered"]]
        words = real_words(" ".join(left))
        if not words or all(w in _GOODBYES for w in words):
            return ""
        return joined(left)


class LiveCall:
    """One live conversation: connected, carried, handed over, closed.

    `ask` answers their words through their assistant (agent/worker/desk.py:
    the approval rules, then `answering.voice_reply` on channel `voice`)."""

    def __init__(self, *, floor, words: Words, ask: Callable[..., Awaitable[str]],
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep,
                 duplex: Callable[[], bool] = lambda: False, connect=None) -> None:
        import soxr

        self.floor = floor
        self.words = words
        self.ask = ask
        self.clock = clock
        self.sleep = sleep
        self.duplex = duplex
        self.connect = connect or live.connect
        self.session = Session(ask=ask)
        self._recent: deque[str] = deque(maxlen=6)  # its last transcript fragments
        self._watchdog_task: asyncio.Task | None = None
        self.live = None
        self.reason = ""
        self.closing = False
        # Why the desk ended this conversation, once it has (voice/engine.py):
        # this call's own. It was the engine's, one for all calls, and on the
        # 9 October 2026 retest a call ended by a wake set it as it finished
        # closing -- after the next call had cleared it -- and the next call
        # took itself to have been ended while it connected.
        self.end_reason = ""
        self.opener: str | None = None
        # Notes of the room asked for in it, started once it has ended.
        self.notes_after: str | None = None
        self.opened_at = clock()
        self.seconds = 0.0  # its own count, from `session.usage.updated`
        self.said_at = float("-inf")  # when its own transcript last arrived
        self.said_word_at = float("-inf")
        self.ever_said = False
        self.events: Counter = Counter()
        self.audio_s = 0.0  # its frames' seconds, and the audible ones'
        self.loud_s = 0.0
        self.muted = False
        self._duplex_failed = False
        self.work: set[asyncio.Task] = set()
        self.late: list[str] = []
        self.ended_at: float | None = None  # when the desk decided it was over
        self._mic: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = asyncio.Event()
        self._handoff_lock = asyncio.Lock()
        self._last_handoff = float("-inf")
        self._promised: float | None = None
        self._in = soxr.ResampleStream(IN_RATE, RATE, 1, dtype="float32")
        rate = int(getattr(floor.player, "device_rate", 0) or RATE)
        self.out_rate = rate
        self._out = soxr.ResampleStream(RATE, rate, 1, dtype="float32")

    def elapsed(self) -> float:
        return self.clock() - self.opened_at

    # -- starting -------------------------------------------------------------

    async def start(self, history: list[tuple[str, str]], *, opener: str | None = None,
                    place: str = "") -> None:
        """Connect and start the session, and tell it `opener` (ANSWER_NOW,
        GREET) if it is to speak first. Raises `live.LiveUnavailable`
        before a word was said if it cannot."""
        self.opener = opener
        self._loop = asyncio.get_running_loop()
        self._mic = asyncio.Queue(maxsize=400)
        try:
            sock = await asyncio.wait_for(self.connect(), live.START_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - anything here means "not now"
            raise live.LiveUnavailable(f"connect: {type(exc).__name__}: {exc}") from exc
        try:
            await sock.send(json.dumps(session_start(history=history, place=place)))
            first = json.loads(await asyncio.wait_for(sock.recv(), live.START_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await sock.close()
            raise live.LiveUnavailable(f"start: {type(exc).__name__}: {exc}") from exc
        if first.get("type") != "session.started":
            with contextlib.suppress(Exception):
                await sock.close()
            raise live.LiveUnavailable(f"start refused: {json.dumps(first)[:300]}")
        self.live = sock
        self.opened_at = self.clock()
        if opener:
            await self.append("session.instructions.append", None, opener)

    # -- the microphone in -----------------------------------------------------

    def feed(self, block: np.ndarray) -> None:
        """A block of their microphone, from the ear's thread."""
        loop, mic = self._loop, self._mic
        if loop is None or mic is None or self.closing:
            return

        def put() -> None:
            with contextlib.suppress(asyncio.QueueFull):
                mic.put_nowait(block)

        with contextlib.suppress(RuntimeError):  # the loop has gone: nothing to feed
            loop.call_soon_threadsafe(put)

    async def _send_mic(self) -> None:
        """Their microphone to it, as it comes. On the speakers without a working
        echo canceller, its input is muted while its own voice is audible
        (`session.input_audio.mute`) and silence is sent, so it cannot hear
        itself; on headphones, or with the canceller working, it hears them
        over itself -- full duplex, their choice."""
        while not self.closing:
            block = await self._mic.get()
            if block is None:
                break
            hold = not self._duplex_now() and self.floor.audible(self)
            if hold != self.muted:
                self.muted = hold
                await self._send({"type": "session.input_audio.mute" if hold
                                  else "session.input_audio.unmute", "event_id":
                                  f"m{time.monotonic_ns()}"})
            if hold:
                block = np.zeros_like(block)
            pcm = np.clip(self._in.resample_chunk(block), -1.0, 1.0)
            await self._send({"type": "session.input_audio.append",
                              "audio": base64.b64encode((pcm * 32767).astype("<i2")
                                                        .tobytes()).decode()})

    def _duplex_now(self) -> bool:
        """Whether its own voice is being kept out of the microphone now. A
        question that cannot be answered means no -- muted while it talks is
        the safe side -- and is said once in the log. Before, a raise here
        ended the microphone's sender in silence, and the conversation went
        on deaf to them (found in the port of its tests, 9 October 2026)."""
        try:
            return bool(self.duplex())
        except Exception:  # noqa: BLE001 - see the docstring: muted, and said
            if not self._duplex_failed:
                self._duplex_failed = True
                log.warning("live: could not tell whether its voice is kept out of the "
                            "microphone; muting it while it talks", exc_info=True)
            return False

    # -- what it says -----------------------------------------------------------

    async def run(self) -> str:
        """Carry the conversation until it closes; returns why it ended."""
        sender = asyncio.ensure_future(self._send_mic())
        try:
            async for raw in self.live:
                if self._event(json.loads(raw)):
                    break
        except Exception as exc:  # noqa: BLE001 - a dropped socket is an end, not a crash
            if not self.closing:
                log.warning("gpt-live connection lost: %s", type(exc).__name__)
        finally:
            self.closing = True
            self._closed.set()
            sender.cancel()
            if self._watchdog_task is not None:
                self._watchdog_task.cancel()
            # A hand-over under way is not cancelled: its answer is theirs, and
            # is said by the local voice (`late`, voice/engine.py). Before 9
            # October 2026 the end of a call cancelled them with the rest.
            from agent.models import LIVE_VOICE

            self.session.voice_cost_usd = LIVE_VOICE.cost_usd(self.seconds or self.elapsed())
            with contextlib.suppress(Exception):
                await self.live.close()
        self.reason = self.reason or ("voice closed" if self.closing else "voice lost")
        return self.reason

    def _event(self, event: dict) -> bool:
        """One event from GPT-Live. True when the session has closed."""
        kind = str(event.get("type"))
        self.events[kind] += 1
        if kind == "session.output_audio.delta":
            self._play(event.get("delta") or "")
        elif kind == "session.output_transcript.delta":
            text = str(event.get("delta") or "")
            now = self.clock()
            self.said_at = now
            self.ever_said = True
            self.session.note("us", text)
            self.floor.note_said(self, text, at=now)
            if real_words(text):
                self.said_word_at = now
            self._recent.append(text)
            if live.PROMISE.search(joined(list(self._recent))):
                self._watch(now)
        elif kind == "session.input_transcript.delta":
            # Its transcript of the room: kept for the record, handed to
            # nobody (their words are the desk's own Whisper's: `Words`).
            self.session.note("them", str(event.get("delta") or ""))
        elif kind == "session.delegation.created":
            delegation = (event.get("delegation") or {}).get("id")
            self.spawn(self.handoff(delegation))
        elif kind in ("session.usage.updated", "session.closed"):
            self.seconds = float((event.get("usage") or {}).get("seconds") or self.seconds)
            if kind == "session.closed":
                self.reason = self.reason or str(event.get("reason") or "closed")
                return True
        elif kind == "error":
            log.warning("gpt-live error: %s", json.dumps(event.get("error"))[:300])
        return False

    def _play(self, payload: str) -> None:
        if not payload or self.closing:
            return
        pcm = np.frombuffer(base64.b64decode(payload), dtype="<i2").astype(np.float32) / 32768
        seconds = len(pcm) / RATE
        self.audio_s += seconds
        from voice.floor import AUDIBLE_RMS

        if len(pcm) and float(np.sqrt(np.mean(np.square(pcm)))) >= AUDIBLE_RMS:
            self.loud_s += seconds
        # Every frame is played, silence included, so its speech keeps its
        # timing; only audible ones count as its voice (voice/floor.py).
        self.floor.play(self, self._out.resample_chunk(pcm), self.out_rate)

    def talking(self, now: float | None = None) -> bool:
        """Whether it is talking: audible on the speakers *and* its own
        transcript still arriving -- both witnesses, so neither a stream of
        quiet noise nor a stuck transcript alone can keep a call open."""
        now = self.clock() if now is None else now
        return self.floor.audible(self, now) and now - self.said_at <= TRANSCRIPT_HOLD_S

    # -- handing their words over --------------------------------------------------

    def spawn(self, work) -> None:
        task = asyncio.ensure_future(work)
        self.work.add(task)
        task.add_done_callback(self.work.discard)

    def hand_over_now(self) -> None:
        """Their answer to a card goes to their assistant by code, the moment it
        is heard, whatever GPT-Live makes of it."""
        self.spawn(self.handoff(None))

    def _watch(self, now: float) -> None:
        if self._promised is None and not self.closing:
            self._promised = now
            self._watchdog_task = asyncio.ensure_future(self._watchdog(now))

    async def _watchdog(self, promised_at: float) -> None:
        """It said it would check, and has not handed anything over: the desk
        hands over their words itself."""
        try:
            await self.sleep(WATCHDOG_S)
            if self._last_handoff < promised_at and self.words.untaken():
                log.info("gpt-live promised a lookup and did not hand off; handing off")
                await self.handoff(None)
        finally:
            self._promised = None

    async def handoff(self, delegation_id: str | None) -> None:
        """Their words since the last hand-over, to their assistant; its answer,
        back. One at a time, in order."""
        from core import call_policy

        async with self._handoff_lock:
            await self.words.settle(HANDOFF_WAIT_S, self.sleep)
            started = self.clock()
            text, heard_at = self.words.take()
            recently = started - self._last_handoff < live.SAME_REQUEST_S
            self._last_handoff = started
            if not text:
                await self.append(
                    "session.thinking.append" if recently else "session.instructions.append",
                    delegation_id, ALREADY_ASKED if recently else NOTHING_HEARD)
                return
            if len(self.session.turns) >= call_policy.MAX_TURNS_PER_CALL:
                await self.append("session.instructions.append", delegation_id, WRAP_UP)
                self.reason = "turn limit"
                self.spawn(self.close("turn limit"))
                return
            try:
                answer = await self.ask(text, heard_at=heard_at)
            except Exception:  # noqa: BLE001 - one more thing to lose mid-call
                log.exception("a live hand-over failed")
                answer = ""
            spoken = cut(speakable(answer), live.MAX_RESULT_CHARS, sentence=True, mark="")
            if self.closing:
                # It ended while their assistant was working: the answer is
                # said by the local voice instead (`voice/engine.py`).
                if spoken:
                    self.late.append(spoken)
            elif spoken:
                await self.append("session.commentary.append", delegation_id, spoken)
            else:
                await self.append("session.instructions.append", delegation_id, UNREACHABLE)
            self.session.turns.append(Turn(said=spoken or "(nothing answered)", heard=text,
                                           seconds=self.clock() - started))

    @property
    def working(self) -> bool:
        return any(not task.done() for task in self.work) or self._handoff_lock.locked()

    # -- talking to it -----------------------------------------------------------

    async def _send(self, event: dict) -> None:
        if self.live is None:
            return
        with contextlib.suppress(Exception):
            await self.live.send(json.dumps(event))

    async def append(self, kind: str, delegation_id: str | None, content: str) -> None:
        """Text for GPT-Live to read: never a waiting card's word, even in
        their assistant's answer it is to say (`session_start`)."""
        from agent import cardwords

        content = cardwords.mask(content, cardwords.words_now())
        await self._send({"type": kind, "event_id": f"e{time.monotonic_ns()}",
                          "delegation_id": delegation_id, "content": content})

    async def close(self, reason: str) -> None:
        """Ask GPT-Live to finish; `run` ends on its `session.closed`, or when
        that has not come in CLOSE_WAIT_S."""
        if self.closing:
            return
        self.closing = True
        self.reason = self.reason or reason
        if self.live is None:
            return  # still connecting: `abandon` closes what `start` opens
        await self._send({"type": "session.close", "event_id": "close"})
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._closed.wait(), CLOSE_WAIT_S)
        if self.live is not None:
            with contextlib.suppress(Exception):
                await self.live.close()

    async def abandon(self, reason: str) -> None:
        """Close a session that was ended while it connected, without running
        it: nothing was said in it, and nothing will be."""
        self.closing = True
        self.reason = self.reason or reason
        self._closed.set()
        if self.live is not None:
            await self._send({"type": "session.close", "event_id": "close"})
            with contextlib.suppress(Exception):
                await self.live.close()

    def summary(self) -> str:
        """Numbers, never words: what it sent, and how much of it was voice."""
        kinds = " ".join(f"{k.split('.')[-1] if k.startswith('session.') else k}={n}"
                         for k, n in sorted(self.events.items()))
        return (f"frames_s={self.audio_s:.0f} audible_s={self.loud_s:.0f} "
                f"words_after_s={max(0.0, self.said_word_at - self.opened_at):.0f} "
                f"events: {kinds or '-'}")
