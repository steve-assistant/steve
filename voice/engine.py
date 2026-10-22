"""The desk's state machine: the one place its state changes, one owner per turn.

Before 9 October 2026 the desk's state was spread over six places -- a
fourteen-key dict, two module globals, two context variables, the ear's
states and the live call's clocks -- changed from six threads, each piece
added by an audit to close one hole. Two sound tests that day found what
falls between them: the local voice and GPT-Live answering the same turn,
a live conversation that could not close, a request dropped when a call
hung up, and no way out of a call but switching hands-free off.

So the desk is one value here (`Engine.mode`, `Engine.turn`, `Engine.call`)
changed in one method (`Engine._to`), on the event loop. Threads report
what they heard (`voice/listen.EarEvent`); they decide nothing. Every turn
is an object (`Turn`) that owns the speakers while it runs
(`voice/floor.py`): a turn that has been cut cannot make a sound, and what
their assistant asked of a turn -- notes of the room, a live conversation --
lives on the turn and dies with it.

The modes:

    off        hands-free switched off (push-to-talk still runs here)
    paused     switched on, the microphone closed: locked, signed out, no loopback
    idle       waiting for "hey Steve" (and a note's stretches, if one is open)
    capture    gathering what follows a wake
    transcribe Whisper, then what code decides (core/desk.py rules first)
    local      their assistant answering, spoken as it is written
    follow     FOLLOW_UP_S in which the user can carry on without the phrase
    live       a live conversation they asked for by name (voice/desk_call.py)

"Hey Steve" reaches this in every mode, and wins: it stops what is talking,
ends a live conversation -- handing over what the user said in it -- and starts a
capture. Every mode has a longest stay (`DWELL_S`); a mode that outlives it
is logged and left, so "not responding for minutes" is a bug the log names.

Who may open a live conversation is `LIVE_OPENED_BY`: the maintainer's choice on
9 October 2026 was the user alone, by name ("let's talk live"); the back-and-forth
is the follow-up window, in the local voice, with Steve behind it.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from core import desk as rules
from voice.audio import TICK
from voice.audio import tone as _tone
from voice.listen import QUIET, EarEvent, EarMode

log = logging.getLogger("desk")

# Who may open a live conversation at the desk. "user": only the user, asking for
# one by name -- the maintainer's choice, 9 October 2026 (decision C). "user and
# assistant": Steve's `talk_it_through` too, as it was before
# (decision A); the conversation then opens on the reply it has just said,
# given to GPT-Live as history, never as a line to say again.
LIVE_OPENED_BY = "user"

# How often the engine looks at its clocks.
TICK_S = 0.25
# The longest a mode may last before it is taken to be stuck.
DWELL_S = {"capture": rules.MAX_CAPTURE_S + 10.0, "transcribe": 30.0, "local": 300.0,
           "live": 15 * 60 + 90.0}
# A reply still being worked on, and nothing said for this long: a line, so
# a quiet laptop is not taken for one that did not hear. The first stall
# line (voice/pipeline.STALL_S) comes at 1.5 s; these are for long lookups.
STILL_ON_IT_S = (10.0, 45.0)
STILL_ON_IT = ("Still on it.", "This is taking a while; I'll say when it's done.")
# A request that has to wait for an earlier run of their assistant to finish.
ONE_MOMENT_S = 1.5
# A live conversation: closed after IDLE_S with nothing from either side --
# its voice (audible *and* its transcript arriving), them (the microphone's
# voice detector, outside its voice and over no other laptop speech), or
# work in hand -- or after SILENT_S with no *word* from either and no work.
IDLE_S = 20.0
SILENT_S = 45.0
LIVE_MAX_S = 15 * 60
WRAP_UP_BEFORE_S = 20.0
# Opened at their request: if it has said nothing OPENER_NUDGE_S after the user
# asked, and they have not spoken since, it is nudged; by OPENER_GIVE_UP_S
# after it opened it is closed and their request answered by the local voice
# (or, with nothing to answer, the user is told). Never while the user is talking: on 9
# October 2026 the phone's watchdog hung up nine seconds in, as they finished.
# The nudge was at 6 s; at the retest that evening the user had given up waiting
# by 8.
OPENER_NUDGE_S = 4.0
OPENER_GIVE_UP_S = 10.0
# It has answered what the user said once it has been audible this long after it.
ANSWERED_AFTER_S = 3.0
# Speech on the microphone with this much other laptop speech in the second
# before it is the laptop's (a video, a call), not theirs.
COINCIDE_S = 1.0
COINCIDE_SPEECH_S = 0.3
# What a live conversation's end waits for: stretches still being
# transcribed, and hand-overs their assistant is still answering.
HANDOVER_WAIT_S = 1.5
LATE_ANSWER_S = 90.0
# Long enough for the live voice to say it is taking notes before it ends.
NOTES_AFTER_LIVE_S = 6.0


# "Go on": the phrase alone, waiting to be asked.
GO_ON = _tone((660.0, 880.0), 0.09, 0.12)
# Switched off: two falling notes, so the user knows the switch took.
OFF = _tone((880.0, 587.0), 0.09, 0.10)
# A live conversation the user asked for, connecting: three rising notes, at once.
CONNECTING = _tone((523.0, 659.0, 784.0), 0.07, 0.10)


@dataclass(eq=False)
class Turn:
    """One exchange: what started it, what it asked, how it ended."""

    id: int
    source: str  # wake, follow, barge, hotkey, handover, live
    started: float
    cut_in: bool = False
    request: str = ""
    asks: dict = field(default_factory=dict)
    outcome: str = ""
    cut: bool = False
    voice: object = None
    heard: dict = field(default_factory=dict)
    transcript: object = None
    released: float = 0.0
    facts: dict = field(default_factory=dict)
    began: float = field(default_factory=time.perf_counter)
    result: object = None
    logged: bool = False


@dataclass
class Decision:
    """What code made of a transcript (agent/worker/desk.py): an outcome for
    the log, and what comes next -- idle, follow, agent, live or off."""

    outcome: str
    then: str = "idle"
    request: str = ""


def _ended(call) -> str:
    """Why the desk ended this live conversation, if it has: the call's own
    reason (voice/desk_call.LiveCall.end_reason), never one shared by all."""
    return getattr(call, "end_reason", "") or ""


def heard_facts(heard) -> dict:
    """A capture's numbers, without its audio, for the turn's log line."""
    return {"woke": heard.woke, "barge_in": heard.barge_in, "kind": heard.kind,
            "captured_s": heard.ended - heard.started, "speech_s": heard.speech_s,
            "ended_by": heard.ended_by, "asks": heard.asks,
            "laptop_s": heard.laptop_speech_s}


class Engine:
    """The desk. `shell` is the worker's side (agent/worker/desk.DeskShell):
    Whisper, the rules, their assistant, notes, the live conversation's
    parts, the record. `floor` is the speakers (voice/floor.py)."""

    def __init__(self, shell, floor, *, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self.shell = shell
        shell.engine = self
        self.floor = floor
        self.clock = clock
        self.sleep = sleep
        self.ear = None
        self.mode = "off"
        self.why = ""
        self.since = clock()
        self.turn: Turn | None = None
        self.call = None
        self.notes_on = False
        self.follow_until = 0.0
        self.history: deque = deque(maxlen=20)
        self.counts = {"wakes": 0, "laptop_wakes": 0, "near_wakes": 0, "cut_ins": 0,
                       "stuck": 0, "live_handovers": 0}
        self._ids = itertools.count(1)
        self._agent = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._clocks: dict = {}
        # A bare "hey Steve" being checked by Whisper (`_capture`): what the user
        # starts saying meanwhile is gathered and held for its verdict, and
        # the stretches of time whose captures a false wake withdrew.
        self._bare: Turn | None = None
        self._held: list = []
        self._withdrawn: deque = deque(maxlen=8)
        self.dropped_unread = 0

    # -- the one place the mode changes --------------------------------------

    def _to(self, mode: str, why: str = "") -> None:
        now = self.clock()
        if mode != self.mode or why != self.why:
            log.info("desk: %s -> %s why=%s turn=%s after_s=%.1f", self.mode, mode, why or "-",
                     self.turn.id if self.turn else "-", now - self.since)
            self.history.append({"at": round(now, 1), "from": self.mode, "to": mode,
                                 "why": why})
            self.mode, self.why, self.since = mode, why, now
        self._apply_ear()

    def _apply_ear(self) -> None:
        if self.ear is not None:
            self.ear.set_mode(self.ear_mode())

    def ear_mode(self) -> EarMode:
        """What the ear may hand on in this mode. Nothing but a wake in idle,
        unless a note the user started is open."""
        mode = self.mode
        if mode in ("off", "paused"):
            return QUIET
        if mode == "idle":
            return EarMode(segments=self.notes_on)
        if mode == "follow":
            return EarMode(follow_until=self.follow_until)
        if mode == "transcribe" and self._bare is not None:
            # "Hey Steve" and nothing after it, being checked: the user may be
            # asking now, and on the 9 October 2026 retest they were -- while a
            # cold Whisper took 2.4 s -- and nobody heard it. What the user says
            # is gathered, and held until Whisper has heard the phrase.
            return EarMode(busy=True, follow_until=self.since + DWELL_S["transcribe"])
        call = self.call
        # While it talks, "hey Steve" is heard at a lower bar
        # (voice/listen.over_voice_bar): the echo canceller ducks them.
        audible = self.floor.audible
        if (mode == "live" and call is not None and call.live is not None
                and not call.closing and not _ended(call)):
            return EarMode(busy=True, stream=call.feed, utterances=True, audible=audible)
        if mode == "local":
            return EarMode(busy=True, barge_speech=rules.BARGE_IN_WITHOUT_WAKE, audible=audible)
        return EarMode(busy=True)

    def spawn(self, work) -> asyncio.Task:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    async def settle(self) -> None:
        """Wait for everything the engine has started to finish: a turn, a
        note's stretch. For tests and a clean shutdown."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled() or task.exception() is None:
            return
        # A failed step must not leave the desk deaf, or holding the floor.
        log.error("desk: a step failed", exc_info=task.exception())
        if self.mode not in ("off", "paused"):
            self._halt("error")
            self._to("idle", "error")

    # -- the microphone ----------------------------------------------------

    def attach(self, ear) -> None:
        """The ear exists; until its microphone opens, the desk is paused."""
        self.ear = ear
        self._to("paused", "opening")

    def detach(self) -> None:
        self.ear = None

    def opened(self) -> None:
        """The microphone is open: listening."""
        if self.mode in ("off", "paused"):
            self.turn = None
            self._to("idle", "mic open")

    def closed(self, reason: str) -> None:
        """The microphone closed -- locked, signed out, no loopback: nothing more
        is heard."""
        if self.mode != "off":
            self._halt(reason)
            self._to("paused", reason)

    def switch_off(self, reason: str = "off") -> None:
        self._halt(reason)
        self.notes_on = False
        self._to("off", reason)

    def _halt(self, reason: str) -> None:
        """Stop everything under way: a live conversation ends, handing over
        what the user said; a turn is cut; the speakers fall silent."""
        if self.call is not None:
            self._end_live(reason)
        if self.turn is not None:
            self._cut(self.turn)
        self.turn = None
        if self.floor.owner is not None:
            self.floor.cut(self.floor.owner)

    def ear_event(self, event: EarEvent) -> None:
        """What the ear heard, on the event loop."""
        if self.mode in ("off", "paused"):
            if event.heard is not None:
                event.heard.audio = None
            return
        kind = event.kind
        if kind == "wake":
            self._wake(event)
        elif kind == "laptop_wake":
            self.counts["laptop_wakes"] += 1
        elif kind == "near_wake":
            self.counts["near_wakes"] += 1
        elif kind == "captured":
            self.spawn(self._capture(event.heard))
        elif kind == "segment":
            self.spawn(self.shell.segment(event.heard))
        elif kind == "utterance":
            self.spawn(self._utterance(event.heard))
        elif kind == "barge_speech":
            self._barge_speech()

    def _wake(self, event: EarEvent) -> None:
        """"Hey Steve", theirs: whatever is talking stops, a live conversation
        ends, and what follows is gathered."""
        self.counts["wakes"] += 1
        if self.call is not None:
            self._end_live("wake")
        elif self.turn is not None:
            self._cut(self.turn)
        if event.barge:
            self.counts["cut_ins"] += 1
        self.play("tick", *TICK)
        self._to("capture", "cut-in" if event.barge else "wake")

    def _barge_speech(self) -> None:
        """Sustained speech over the desk's voice, no wake phrase (D2, off by
        default): its voice stops; what the user said is judged by
        `core/desk.barge_in_counts` when the capture is in."""
        if self.mode == "local" and self.turn is not None:
            self._cut(self.turn)
            self._to("capture", "speech over its voice")

    def _cut(self, turn: Turn) -> None:
        if turn.cut:
            return
        turn.cut = True
        if self.floor.owner is turn:
            self.shell.interrupt()
        self.floor.cut(turn)

    def play(self, owner, sound: np.ndarray, rate: int) -> None:
        """A sound of the desk's own -- a tick, a chime -- that takes the
        floor for as long as it plays."""
        self.floor.claim(owner)
        self.floor.play(owner, sound, rate)
        self.floor.release(owner)

    async def say(self, owner, text: str) -> None:
        """A line of the desk's, said through the floor as `owner` and waited
        for: what comes next must not hear it."""
        if self.floor.owner is not owner:
            self.floor.claim(owner)
        await self.shell.speak(text, self.floor.voice(owner))
        await self.floor.drained(owner, 60.0)

    # -- a capture, and its turn -----------------------------------------

    def _new_turn(self, source: str, **kwargs) -> Turn:
        return Turn(id=next(self._ids), source=source, started=self.clock(), **kwargs)

    async def _capture(self, heard) -> None:
        if heard.kind == "follow" and self._bare is not None:
            # Begun while a bare "hey Steve" was being checked: it waits for
            # Whisper's verdict on the phrase (`_verdict`).
            self._held.append(heard)
            return
        if heard.kind == "follow" and any(a <= heard.started <= b for a, b in self._withdrawn):
            self._drop_unread(heard, "a follow-up begun after a false wake")
            return
        turn = self._new_turn(heard.kind, cut_in=heard.barge_in, heard=heard_facts(heard))
        self.turn = turn
        if getattr(heard, "bare", False):
            self._bare = turn
        self._to("transcribe", heard.kind)
        try:
            transcript = await self.shell.transcribe(heard)
            if self.turn is not turn:
                turn.outcome = turn.outcome or "superseded"
                return
            turn.transcript = transcript
            decision = await self.shell.decide(turn, transcript, heard)
            turn.outcome = decision.outcome
            if self._bare is turn:
                self._verdict(turn, heard, decision)
            if self.turn is not turn:
                return
            if decision.then == "agent":
                await self._answer(turn, decision.request)
            elif decision.then == "live":
                await self._live(turn, by="user", request=decision.request)
            elif decision.then == "follow":
                await self._follow(turn)
            elif decision.then != "off":
                self._rest(turn)
        except Exception:
            # Said in its line, so a capture that failed is not mistaken for
            # one that was heard and handled.
            turn.outcome = "failed"
            raise
        finally:
            if self._bare is turn:
                # Superseded or failed before its verdict: the phrase was
                # never confirmed, so nothing begun after it is their request.
                self._verdict(turn, heard, None)
            self._finish(turn)

    def _verdict(self, turn: Turn, heard, decision: Decision | None) -> None:
        """Whisper's word on a bare "hey Steve": what the user began saying while
        it was checked is their next request if the phrase was there, and is
        dropped unread if it was not -- a capture is handed on in idle only
        after a wake Whisper confirmed (tests/gate/test_desk.py)."""
        self._bare = None
        held, self._held = self._held, []
        confirmed = (decision is not None and decision.outcome not in ("false_wake", "unreadable")
                     and self.mode not in ("off", "paused"))
        if not confirmed:
            # Withdrawn too: a stretch still being gathered, handed on later.
            self._withdrawn.append((heard.ended - 1.0, self.clock() + 0.5))
            for each in held:
                self._drop_unread(each, "said after a wake Whisper did not confirm")
            return
        for each in held:
            self.spawn(self._capture(each))

    def asking(self) -> bool:
        """Whether the user is already saying something after a bare "hey Steve":
        then the "go on" chime would only talk over them."""
        return bool(self._held) or (self.ear is not None
                                    and getattr(self.ear, "gathering", None) == "capture")

    def _drop_unread(self, heard, why: str) -> None:
        heard.audio = None
        self.dropped_unread += 1
        log.info("desk: a capture dropped unread: %s", why)
        # The ear waits for an answer to every capture it hands on (it is
        # busy until then, and a wake would count as a cut-in): this is one.
        self._apply_ear()

    def _finish(self, turn: Turn) -> None:
        if not turn.logged:
            turn.logged = True
            self.shell.log_turn(turn)

    def _rest(self, turn: Turn) -> None:
        """Back to waiting for the phrase."""
        if self.turn is turn:
            self.floor.release(turn)
            self.turn = None
            self._to("idle", turn.outcome)

    async def _answer(self, turn: Turn, request: str) -> None:
        """Their assistant answers, spoken through this turn's hold on the
        speakers; then the follow-up window."""
        turn.request = request
        turn.voice = self.floor.voice(turn)
        self.floor.claim(turn)
        self._to("local", turn.source)
        if self._agent.locked():
            self.spawn(self._one_moment(turn))
        async with self._agent:
            if self.turn is not turn:
                turn.outcome = "superseded"
                return
            turn.result = await self.shell.answer(turn)
        if turn.cut or self.turn is not turn:
            turn.outcome = "cut"
            return
        if (title := turn.asks.get("notes")) is not None and self._may_open_room():
            await self.floor.drained(turn, 60.0)
            self.shell.start_room(title)
        # Not for a cut-in: what the desk heard of one is the least reliable
        # thing it hears (its own voice is in it). The tool refuses one too;
        # this is the second lock, as it was before the rebuild.
        if (turn.asks.get("live") is not None and LIVE_OPENED_BY != "user"
                and not turn.cut_in):
            await self.floor.drained(turn, 60.0)
            if self.turn is turn:
                await self._live(turn, by="assistant")
            return
        await self._follow(turn)

    async def _one_moment(self, turn: Turn) -> None:
        await self.sleep(ONE_MOMENT_S)
        if self.turn is turn and self._agent.locked() and not self.floor.audible():
            await self.say(turn, "One moment.")

    async def _follow(self, turn: Turn) -> None:
        """When its voice and echo have died: FOLLOW_UP_S for them to carry on
        without the phrase -- unless a note of the room is open. Then speech
        is the note's: a follow-up window would hand the lecturer's next
        sentence to their assistant as a request, and a lecture is never to
        reach a model (the desk's microphone in docs/security-model.md). With a note open the user
        says "hey Steve" with every request."""
        await self.floor.drained(turn, 60.0)
        if self.turn is not turn or self.mode in ("off", "paused"):
            return
        self.floor.release(turn)
        self.turn = None
        if self.notes_on:
            self._to("idle", "a note is open")
            return
        self.follow_until = self.clock() + rules.FOLLOW_UP_S
        self._to("follow", turn.outcome)

    def _may_open_room(self) -> bool:
        """Whether notes of the room may start: only with the microphone
        open -- hands-free on, the screen unlocked. `take_notes` refuses
        them otherwise; this is the second lock."""
        return self.ear is not None and self.mode not in ("off", "paused")

    # -- push-to-talk ---------------------------------------------------------

    def hotkey_down(self) -> None:
        """The user pressed to talk: whatever is talking stops, and a live
        conversation ends, what they said in it kept."""
        if self.call is not None:
            self._end_live("hotkey")
        if self.turn is not None:
            self._cut(self.turn)
        self.turn = None
        if self.floor.owner is not None:
            self.floor.cut(self.floor.owner)

    async def hotkey_turn(self, run):
        """A push-to-talk turn, run by `run(out)` (the websocket's
        `finish_turn`), through the floor like any other."""
        turn = self._new_turn("hotkey")
        self.turn = turn
        turn.voice = self.floor.voice(turn)
        self.floor.claim(turn)
        if self.mode not in ("off", "paused"):
            self._to("local", "hotkey")
        try:
            async with self._agent:
                with self.shell.turn_context(turn):
                    result = await run(turn.voice)
            if ((title := turn.asks.get("notes")) is not None and not turn.cut
                    and self._may_open_room()):
                await self.floor.drained(turn, 60.0)
                self.shell.start_room(title)
            if self.turn is turn:
                if self.mode in ("off", "paused"):
                    self.floor.release(turn)
                    self.turn = None
                else:
                    await self._follow(turn)
            return result
        finally:
            if self.turn is turn and self.mode in ("off", "paused"):
                self.turn = None

    # -- notes ------------------------------------------------------------------

    def transcribe(self, on: bool) -> None:
        """A note the user started is open, or has ended: in idle, every stretch
        of speech goes to it."""
        self.notes_on = on
        self._apply_ear()

    def end_live_soon(self, reason: str, title: str | None = None) -> None:
        """Notes asked for in a live conversation: it ends once it has said
        so, or its own voice would be written into them; room notes then
        start (`title`)."""
        call = self.call
        if title is not None and call is not None:
            call.notes_after = title

        async def later() -> None:
            await self.sleep(NOTES_AFTER_LIVE_S)
            if self.call is call:
                self._end_live(reason)

        if call is not None:
            self.spawn(later())

    # -- a live conversation -------------------------------------------------

    async def _live(self, turn: Turn, *, by: str, request: str = "") -> None:
        from voice.live import LiveUnavailable

        if self.shell.live_minutes_left() < 0.5:
            await self.say(turn, "That's the live conversation time used up for now, so "
                                 "I'll answer one thing at a time.")
            await self._follow(turn)
            return
        from voice.desk_call import ANSWER_NOW, GREET

        call = self.shell.live_call()
        self.call = call
        now = self.clock()
        self._clocks = {"by": by, "asked": now, "opened": now, "active": now, "nudged": False,
                        "warned": False, "user": 0.0,
                        "limit": min(LIVE_MAX_S, self.shell.live_minutes_left() * 60.0)}
        if request:
            # Their words before it was listening: what it must answer first,
            # and what the local voice answers if it never does.
            call.words.add(request, now, now)
        self.floor.claim(call)
        if by == "user":
            # Heard, at once: the connection takes a second or two, and on
            # 9 October 2026 the user heard nothing for eight and asked again.
            self.floor.play(call, *CONNECTING)
        self._to("live", "connecting")
        opener = (ANSWER_NOW if request else GREET) if by == "user" else None
        try:
            await call.start(self.shell.history(turn), opener=opener, place=self.shell.place())
        except LiveUnavailable as exc:
            log.warning("no live conversation: %s", exc)
            if self.call is call:
                self.call = None
            if self.turn is turn:
                await self.say(turn, "I can't start a live conversation right now. Ask me "
                                     "one thing at a time instead.")
                await self._follow(turn)
            return
        if self.turn is not turn or _ended(call):
            # A wake, the hotkey or the switch while it was connecting: it
            # was ended before it had a socket to close, so it is closed here
            # rather than carried -- `run` would wait on it for ever.
            reason = _ended(call) or "superseded"
            await call.abandon(reason)
        else:
            now = self.clock()
            self._clocks.update(opened=now, active=now)
            self.shell.live_started(call)
            self._to("live", by)
            reason = await call.run()
        await self._after_live(turn, call, reason)

    def _end_live(self, reason: str) -> None:
        call = self.call
        if call is None or call.closing or _ended(call):
            return
        call.end_reason = reason
        call.ended_at = self.clock()
        log.info("desk: ending the live conversation: %s", reason)
        self.floor.cut(call)
        self.spawn(call.close(reason))
        self._apply_ear()

    async def _utterance(self, heard) -> None:
        """A stretch of their speech in a live conversation: the desk's own
        Whisper writes it down, and that is what is handed over."""
        call = self.call
        if call is None or call.closing:
            heard.audio = None
            return
        call.words.expect()
        text = ""
        try:
            transcript = await self.shell.transcribe(heard)
            text = transcript.text if transcript.ok else ""
            if (phrase := rules._PHRASE.search(text)) is not None:
                # The stretch a "hey Steve" cut short: the phrase, and what
                # follows it, are the wake's capture's -- not words of their
                # left unanswered here. On 9 October 2026 "Hey Steve" was
                # written into the desk's thread as one.
                text = text[:phrase.start()].strip()
            overlapped = (self.floor.onset(call) <= heard.ended
                          and self.floor.audible_until(call) >= heard.started)
            if overlapped:
                # Said over its voice: its own words, come back through the
                # microphone, are not theirs.
                said = self.floor.said_between(heard.ended - rules.OWN_WORDS_S, heard.ended)
                text = rules.own_words_removed(text, said)
            elif heard.laptop_speech_s > 0.5 * max(heard.ended - heard.started, 0.1):
                text = ""  # a video or a call, through the speakers
        finally:
            call.words.add(text, heard.started, heard.ended)
        if text and self.shell.approval_shaped(text):
            call.hand_over_now()

    def _user_spoke(self, now: float, call) -> bool:
        """Whether the user has spoken since the last look: the microphone's voice
        detector, outside its voice and its echo, over no other laptop speech."""
        at = self.ear.speech_at if self.ear is not None else 0.0
        if at <= self._clocks["user"]:
            return False
        self._clocks["user"] = at
        if at <= self.floor.audible_until(call):
            return False
        return self.shell.laptop_speech(at - COINCIDE_S, at) < COINCIDE_SPEECH_S

    def _live_tick(self, now: float) -> None:
        call, st = self.call, self._clocks
        if call is None or call.live is None or call.closing or _ended(call):
            return
        talking = call.talking(now)
        if self.floor.audible(call, now) and now - self.floor.onset(call) >= ANSWERED_AFTER_S:
            call.words.answered_before(self.floor.onset(call))
        work = call.working
        if talking or work or self._user_spoke(now, call):
            st["active"] = now
        last_sign = max(st["opened"], call.words.last_word_at, call.said_word_at,
                        now if work else float("-inf"))
        if st["by"] == "user" and not call.ever_said and not work and \
                call.words.last_word_at <= st["opened"]:
            waited = now - st["opened"]
            if waited >= OPENER_GIVE_UP_S:
                log.warning("desk: the live voice said nothing in %.0f s; the local voice "
                            "answers instead", waited)
                self._end_live("never spoke")
                return
            if now - st["asked"] >= OPENER_NUDGE_S and not st["nudged"]:
                st["nudged"] = True
                from voice.desk_call import NUDGE, nudge

                said = nudge(call.opener) if getattr(call, "opener", None) else NUDGE
                self.spawn(call.append("session.instructions.append", None, said))
        if now - st["active"] >= IDLE_S:
            self._end_live("quiet")
        elif now - last_sign >= SILENT_S:
            self._end_live("silent")
        elif call.elapsed() >= st["limit"]:
            self._end_live("time limit")
        elif not st["warned"] and call.elapsed() >= st["limit"] - WRAP_UP_BEFORE_S:
            st["warned"] = True
            from voice.desk_call import WRAP_UP

            self.spawn(call.append("session.instructions.append", None, WRAP_UP))

    async def _after_live(self, turn: Turn, call, reason: str) -> None:
        """However it ended: what the user said that was not answered is answered
        by the local voice, or -- the desk going off, or their new request
        taking over -- written into the desk's thread. Nothing is dropped."""
        end = _ended(call) or reason
        call.end_reason = end
        if getattr(call, "ended_at", None) is None:
            call.ended_at = self.clock()
        self.floor.cut(call)
        if call.work:
            await asyncio.wait(list(call.work), timeout=LATE_ANSWER_S)
        await call.words.settle(HANDOVER_WAIT_S, self.sleep)
        unanswered = call.words.unanswered()
        late = list(call.late)
        if self.call is call:
            self.call = None
        self.shell.live_ended(call, end, unanswered=unanswered)
        notes, call.notes_after = getattr(call, "notes_after", None), None
        ours = (self.turn is turn and self.mode not in ("off", "paused")
                and end not in ("wake", "hotkey"))
        if not ours:
            self.shell.to_thread(unanswered)
            if self.turn is turn and self.mode == "live":
                # Never left in a live conversation that has none: on 9
                # October 2026 the desk sat 23 s in "live" with no call, deaf
                # to all but "hey Steve".
                self._rest(turn)
            return
        if notes is not None:
            self.shell.to_thread(unanswered)
            self.shell.start_room(notes)
            self._rest(turn)
            return
        if late:
            await self.say(turn, " ".join(late))
        if end == "never spoke" and not unanswered and not late and self.turn is turn:
            # It was asked for by name and never spoke: said, so the silence
            # is not taken for one that did not hear.
            await self.say(turn, "I couldn't get the live voice to answer. Ask me here "
                                 "instead.")
        if unanswered and self.turn is turn:
            self.counts["live_handovers"] += 1
            log.info("desk: a live conversation ended (%s) with the user's words unanswered: "
                     "the local voice answers them", end)
            handover = self._new_turn("handover")
            self.turn = handover
            try:
                await self._answer(handover, unanswered)
            finally:
                self._finish(handover)
            return
        await self._follow(turn)

    # -- time ----------------------------------------------------------------------

    def tick(self) -> None:
        """The engine's clocks, every TICK_S."""
        now = self.clock()
        mode = self.mode
        if mode == "follow" and now >= self.follow_until:
            self._to("idle", "follow-up over")
        elif mode == "live":
            self._live_tick(now)
        elif mode == "local":
            self._local_tick(now)
        limit = DWELL_S.get(self.mode)
        if limit is not None and now - self.since > limit:
            self.counts["stuck"] += 1
            log.warning("desk: %s outlived %.0f s; back to waiting for the phrase", self.mode,
                        limit)
            self._halt("stuck")
            self._to("idle", "stuck")

    def _local_tick(self, now: float) -> None:
        turn = self.turn
        if turn is None or turn.cut or self.floor.audible(now=now):
            return
        said = turn.facts.get("still", 0)
        quiet_since = max(turn.started, self.floor.audible_until(turn))
        if said < len(STILL_ON_IT_S) and now - quiet_since >= STILL_ON_IT_S[said]:
            turn.facts["still"] = said + 1
            self.spawn(self._still(turn, STILL_ON_IT[said]))

    async def _still(self, turn: Turn, line: str) -> None:
        if self.turn is turn and self.floor.owner is turn:
            await self.shell.speak(line, self.floor.voice(turn))

    async def run(self) -> None:
        """The clocks, in production (the simulator ticks by hand)."""
        while True:
            await self.sleep(TICK_S)
            try:
                self.tick()
                self.floor.check()
            except Exception:  # noqa: BLE001 - the clock must keep going
                log.exception("desk: a tick failed")

    def status(self) -> dict:
        """Numbers and states, never words."""
        call = self.call
        owner = self.floor.owner
        return {
            "mode": self.mode, "why": self.why,
            "for_s": round(self.clock() - self.since, 1),
            "turn": self.turn.id if self.turn else None,
            "owner": None if owner is None else (owner if isinstance(owner, str)
                                                 else type(owner).__name__.lower()),
            "audible": self.floor.audible(),
            "dropped_sounds": self.floor.dropped,
            "speaker": {"late": getattr(self.floor, "late", 0),
                        "reopened": getattr(self.floor, "reopened", 0)},
            "live": None if call is None else {
                "elapsed_s": round(call.elapsed(), 1), "talking": call.talking(),
                "closing": call.closing},
            "counts": dict(self.counts),
            "history": list(self.history),
        }
