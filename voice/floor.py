"""Who may make sound at the desk, and what was actually heard.

Two failures of 9 October 2026 came from the speakers having no owner and
no record of what they played:

- **Two voices on one turn.** The local voice spoke their assistant's reply;
  then GPT-Live, opened with the same words, said it again. Chimes, stall
  lines, a cut reply's next sentence and a live conversation's frames all
  went into one queue, each guarded by a flag of its own.
- **"Speaking" for minutes in silence.** GPT-Live streams audio frames all
  the time, silence included. The desk took the queue being full for its
  voice, so a live conversation that had gone quiet could not close, and
  their speech was "inside its own voice" the whole time.

So the speakers have one owner at a time -- a turn, a live conversation, a
chime -- and only the owner's sound is queued: anything else is dropped and
counted (`Floor.play`). Taking the floor stops what the last owner left.
And what was *audible* is measured, not inferred: the player keeps every
block it plays with its loudness (`voice/audio.Player.blocks`), read here
off the audio thread (`Floor.absorb`), and a block above AUDIBLE_RMS keeps
its owner audible for that block, the output latency and the room's echo
tail. Silent frames never are.

The floor also keeps, as text, what each owner was saying and when it was
due to play (`said_between`): the desk's own words, for telling them from
theirs when its voice comes back through the microphone (core/desk.py,
`own_words_removed`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable

import numpy as np

log = logging.getLogger("desk")

# Quieter than this is not sound anyone hears from a laptop's speakers:
# about -50 dBFS. Kokoro speaks at -25 to -20; a silent frame is zeros.
AUDIBLE_RMS = 0.003
# From the player's callback to the speaker: WASAPI's shared-mode buffer.
OUTPUT_LATENCY_S = 0.06
# How long the room holds the desk's voice after it stops, as far as the
# microphone is concerned (core/desk.OWN_VOICE_TAIL_S says the same for the
# approval rule, where it cannot be shortened by an upgrade).
ECHO_TAIL_S = 0.35
# A stream that has not called back for this long, with sound queued, is
# late: logged, and left to catch up with its queue intact. On the 9 October
# 2026 retest the watchdog took a stream this late for a stopped one, twice
# in 250 ms mid-reply, and dropped everything queued: the user heard "Contoso began
# in...", three seconds of glitches, then the reply's last sentence.
LATE_S = 1.0
# One that has not called back for this long, or that says it is not
# running, has stopped (a device gone, a callback that raised): it is
# reopened, and what it held is dropped -- nobody wants a reply's middle
# five seconds late.
DEAD_S = 5.0
# How often the stream's timekeeping is logged, when it was not clean.
REPORT_S = 60.0
# How fast speech is assumed to go, for text whose timing the floor was not
# told (a live conversation's transcript): about fifteen characters a second.
CHARS_PER_S = 15.0


class Floor:
    """The desk's speakers: one owner, and a measure of what was heard."""

    def __init__(self, player, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep) -> None:
        self.player = player
        self.clock = clock
        self.sleep = sleep
        self.owner = None
        self._lock = threading.Lock()
        self._until: dict = {}       # owner -> audible until (latency and tail included)
        self._onset: dict = {}       # owner -> when its present stretch of sound began
        self._tail_at = 0.0          # when what is queued will have played, by our count
        self._said: deque = deque(maxlen=400)  # (owner, text, start, end)
        self.dropped = 0
        self.audible_s: dict[str, float] = {}  # by owner's kind, for the log
        self.late = 0        # times the stream fell more than LATE_S behind
        self.reopened = 0    # times it had stopped and was opened again
        self._late_since: float | None = None
        self._reported: float | None = None
        self._reopener: threading.Thread | None = None
        # A player that pushes what it played (the simulator's, the gate's)
        # is told where; the real one leaves it to be read (`absorb`).
        if hasattr(player, "on_block"):
            player.on_block = self.played

    # -- who may speak ----------------------------------------------------

    def claim(self, owner) -> None:
        """Make `owner` the one that may sound, stopping whatever another
        owner left queued."""
        with self._lock:
            if self.owner is owner:
                return
            self.player.take()
            self._tail_at = self.clock()
            self.owner = owner

    def release(self, owner) -> None:
        """`owner` has nothing more to say; what it queued plays out."""
        with self._lock:
            if self.owner is owner:
                self.owner = None

    def play(self, owner, audio: np.ndarray, rate: int, *, text: str | None = None) -> bool:
        """Queue `owner`'s sound, if it holds the floor. Returns whether it
        was queued; a sound from anyone else is dropped and counted."""
        with self._lock:
            if self.owner is not owner:
                self.dropped += 1
                log.info("floor: a sound from %s dropped: %s holds the floor",
                         _kind(owner), _kind(self.owner))
                return False
            now = self.clock()
            seconds = len(audio) / float(rate or 1)
            start = max(now, self._tail_at)
            self._tail_at = start + seconds
            if text:
                self._said.append((owner, text, start, self._tail_at))
            self.player.enqueue(audio, rate, owner)
            return True

    def cut(self, owner) -> bool:
        """Stop `owner`'s sound now, for good. Returns whether anything was
        cut off. (Not kept to resume: a cut-in that comes to nothing --
        Whisper missing the phrase in the mix of its voice and theirs -- is
        most often "hey Steve, stop", and on 9 October 2026 exactly that
        capture was the stop that worked.)"""
        with self._lock:
            if self.owner is not owner:
                return False
            chunks = self.player.take()
            now = self.clock()
            self._tail_at = now
            # Its voice stops now; its echo is still in the room.
            if owner in self._until:
                self._until[owner] = min(self._until[owner], now + ECHO_TAIL_S)
            # What it never got to say was never in the room: not its words to
            # tell from theirs, and not said.
            self._said = deque(((o, t, s, min(e, now) if o is owner else e)
                                for o, t, s, e in self._said if not (o is owner and s >= now)),
                               maxlen=self._said.maxlen)
            self.owner = None
            return bool(chunks)

    def voice(self, owner) -> FloorVoice:
        """`owner`'s way to the speakers, in the shape `voice/pipeline.py`
        plays through."""
        return FloorVoice(self, owner)

    # -- what was heard ---------------------------------------------------

    def played(self, owner, rms: float, seconds: float, now: float | None = None) -> None:
        """A block went to the speakers at `now` (by default, just now)."""
        now = self.clock() if now is None else now
        if rms < AUDIBLE_RMS:
            return
        with self._lock:
            if now > self._until.get(owner, float("-inf")):
                self._onset[owner] = now
            self._until[owner] = max(self._until.get(owner, float("-inf")),
                                     now + seconds + OUTPUT_LATENCY_S + ECHO_TAIL_S)
            key = _kind(owner)
            self.audible_s[key] = self.audible_s.get(key, 0.0) + seconds

    def absorb(self) -> None:
        """What the player has played since the last look, each block at the
        time it went out (voice/audio.Player.blocks). Read here, off the
        audio thread, rather than told from it: its callback waited on this
        floor's lock, and on 9 October 2026 it went quiet mid-reply."""
        drain = getattr(self.player, "drain_blocks", None)
        if drain is None:
            return
        for at, owner, rms, seconds in drain():
            self.played(owner, rms, seconds, now=at)

    def audible(self, owner=None, now: float | None = None) -> bool:
        """Whether `owner` (any owner, by default) is audible now, its echo
        tail included."""
        self.absorb()
        now = self.clock() if now is None else now
        with self._lock:
            if owner is not None:
                return self._until.get(owner, float("-inf")) > now
            return any(until > now for until in self._until.values())

    def audible_until(self, owner=None) -> float:
        """Until when `owner` (by default, anything) is audible, tail included:
        the end of the desk's own voice, as the microphone hears it."""
        self.absorb()
        with self._lock:
            if owner is not None:
                return self._until.get(owner, float("-inf"))
            return max(self._until.values(), default=float("-inf"))

    def onset(self, owner) -> float:
        """When `owner`'s present or last stretch of sound began."""
        self.absorb()
        with self._lock:
            return self._onset.get(owner, float("-inf"))

    def note_said(self, owner, text: str, at: float | None = None) -> None:
        """Words `owner` said whose audio the floor did not time itself -- a
        live conversation's transcript."""
        at = self.clock() if at is None else at
        with self._lock:
            self._said.append((owner, text, at, at + len(text) / CHARS_PER_S))

    def said_between(self, start: float, end: float) -> str:
        """What the desk was saying, any owner, due to play between two times."""
        with self._lock:
            return " ".join(text for _, text, s, e in self._said if s <= end and e >= start)

    def forget(self) -> None:
        """Drop the record of what was said: the desk is switching off."""
        with self._lock:
            self._said.clear()

    def busy(self, owner) -> bool:
        """Whether `owner` still has sound queued or audible."""
        playing = bool(getattr(self.player, "is_playing", False))
        return (self.owner is owner and playing) or self.audible(owner)

    async def drained(self, owner, timeout: float = 120.0) -> bool:
        """Wait until `owner`'s sound has played and its echo died -- or it
        lost the floor. False if it was still going at `timeout`."""
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            if self.owner is not owner:
                return not self.audible(owner)
            if not self.busy(owner):
                return True
            await self.sleep(0.05)
        return False

    def check(self) -> bool:
        """The speaker stream's health, every engine tick. Late -- no
        callback for LATE_S with sound queued -- is logged and left alone:
        its queue plays when it catches up. Stopped -- no callback for
        DEAD_S, or the stream says it is not running -- is reopened in a
        thread, what it held dropped: on 9 October 2026 a stopped stream's
        queue read as "speaking" for minutes. Returns whether it reopened.

        Before the 9 October 2026 retest, late and stopped were one thing at
        one second, and a stream that was merely late had its queue dropped
        -- twice in 250 ms, and most of a reply with it."""
        self.absorb()
        player = self.player
        clock = getattr(player, "clock", time.monotonic)
        now = clock()
        self._report(now)
        last = float(getattr(player, "last_callback", 0.0) or 0.0)
        if not last:
            return False  # never opened
        playing = bool(getattr(player, "is_playing", False))
        gap = now - last
        if playing and (gap >= DEAD_S or not getattr(player, "running", True)):
            self._reopen(gap)
            return True
        if playing and gap >= LATE_S:
            if self._late_since is None:
                self._late_since = last
                self.late += 1
                log.warning("floor: the speaker stream is late: no callback for %.1f s with "
                            "%.1f s of sound queued; left to catch up",
                            gap, float(getattr(player, "queued_seconds", 0.0) or 0.0))
        elif self._late_since is not None and gap < LATE_S:
            log.info("floor: the speaker stream caught up after %.1f s without a callback",
                     last - self._late_since)
            self._late_since = None
        return False

    def _reopen(self, gap: float) -> None:
        if self._reopener is not None and self._reopener.is_alive():
            return
        self.reopened += 1
        self._late_since = None
        # What was queued will never play: from now, nothing more of it was
        # said, and the next sound starts now.
        with self._lock:
            now = self.clock()
            self._tail_at = now
            self._said = deque(((o, t, s, min(e, now)) for o, t, s, e in self._said if s < now),
                               maxlen=self._said.maxlen)
        player = self.player

        def reopen() -> None:
            try:
                if hasattr(player, "reopen"):
                    dropped = player.reopen()
                else:
                    dropped = sum(len(c) for c in player.take()) / float(
                        getattr(player, "device_rate", 0) or 24_000)
                log.warning("floor: the speaker stream had stopped (no callback for %.1f s); "
                            "reopened it, %.1f s of queued sound dropped", gap, dropped)
            except Exception:  # noqa: BLE001 - no speakers is a state, not a crash
                log.exception("floor: the speaker stream could not be reopened")

        # Closing and opening a device takes a while: never on the event loop.
        self._reopener = threading.Thread(target=reopen, name="floor-reopen", daemon=True)
        self._reopener.start()

    def _report(self, now: float) -> None:
        """Once a minute, how the speaker callbacks kept time -- when they
        did not keep it cleanly. Numbers, so the next sound test can say
        whether a glitch was a late callback, and how late."""
        if self._reported is None:
            self._reported = now
        if now - self._reported < REPORT_S:
            return
        self._reported = now
        take = getattr(self.player, "take_stats", None)
        if take is None:
            return
        stats = take()
        if stats["late"] or stats["underflows"]:
            log.info("floor: speaker callbacks=%d late=%d underflows=%d max_gap_ms=%.0f "
                     "max_run_ms=%.1f", stats["n"], stats["late"], stats["underflows"],
                     stats["max_gap_s"] * 1000, stats["max_run_s"] * 1000)


class FloorVoice:
    """One owner's way to the speakers, shaped like the player that
    `voice/pipeline.VoiceSession` used to write to directly."""

    def __init__(self, floor: Floor, owner) -> None:
        self.floor = floor
        self.owner = owner

    def enqueue(self, audio: np.ndarray, rate: int, text: str | None = None) -> bool:
        return self.floor.play(self.owner, audio, rate, text=text)

    def stop(self) -> bool:
        return self.floor.cut(self.owner)

    @property
    def queued_seconds(self) -> float:
        if self.floor.owner is not self.owner:
            return 0.0
        return float(getattr(self.floor.player, "queued_seconds", 0.0) or 0.0)

    @property
    def is_playing(self) -> bool:
        return self.floor.busy(self.owner)

    async def drained(self, timeout: float = 120.0) -> bool:
        return await self.floor.drained(self.owner, timeout)


def _kind(owner) -> str:
    """An owner's name for the log: a string as it is, else its class."""
    if owner is None:
        return "nobody"
    return owner if isinstance(owner, str) else type(owner).__name__.lower()
