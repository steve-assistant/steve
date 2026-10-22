"""The desk's speakers, as a sound card's clock: the real `Player`, pulled in real time.

Two of the 9 October 2026 failures lived between the player and the sound
card: a stream whose callbacks came late -- over a second, mid-reply --
and a watchdog that took a late stream for a dead one and dropped what it
held ("Tea was first...", three seconds of glitches, the last sentence).
Neither shows in a test that empties the player's queue by hand. So the
player here is `voice/audio.Player` itself, with only its device swapped
(`VirtualPlayer._open`), and `SpeakerClock` asks its real `_callback` for a
block every period on the wall clock, as PortAudio's thread does. Its
callbacks then compete for the interpreter with Whisper, the wake detectors
and the event loop exactly as they do in the worker.

A callback that comes later than the device's buffer lasts means the
device ran dry: it played silence meanwhile, and what was queued plays
late, from where it was (`SpeakerClock.step`). That is what a late WASAPI
stream does, and the room hears the gap. A stall (`stalls`) is the same,
forced, for a stretch.

What was played, block by block with its owner and loudness, is kept for
the timeline (`VirtualPlayer.played`), alongside the copy the floor drains
(`voice/audio.Player.blocks`).
"""

from __future__ import annotations

import threading
import time
import types
from collections import deque
from collections.abc import Callable

import numpy as np
import soxr

from voice import audio as audio_io

DEVICE_RATE = 48_000
FRAMES = 1024  # voice/audio._BLOCKSIZE
# How far ahead of the device a callback's block is asked for: WASAPI's
# shared-mode buffer. A callback later than this left the device dry.
BUFFER_S = 0.02


class _Tee(deque):
    """`Player.blocks`, which the floor drains, with every block also kept."""

    def __init__(self, keep: list) -> None:
        super().__init__(maxlen=4096)
        self.keep = keep

    def append(self, item) -> None:
        self.keep.append(item)
        super().append(item)


class VirtualStream:
    """A stream that runs until it is stopped, aborted or closed."""

    def __init__(self) -> None:
        self.active = True

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def abort(self) -> None:
        self.active = False

    def close(self) -> None:
        self.active = False


class VirtualPlayer(audio_io.Player):
    """The desk's real player, on a device that is a clock in this process."""

    def __init__(self, rate: int = DEVICE_RATE) -> None:
        super().__init__(device=None)
        self._rate = rate
        self.device_rate = rate
        self.played: list[tuple[float, object, float, float]] = []  # (when, owner, rms, s)
        self.blocks = _Tee(self.played)
        self.opened = 0

    def _open(self) -> None:
        self.device_rate = self._rate
        self._stream = VirtualStream()
        self.opened += 1

    def kill(self) -> None:
        """The device goes away: the stream stops calling back, and says so."""
        if self._stream is not None:
            self._stream.active = False


class SpeakerClock:
    """A sound card's clock for a `VirtualPlayer`: a callback every FRAMES at
    DEVICE_RATE, on the wall clock, with what it played written to `sink` at
    16 kHz (the room). `stalls` are (at, seconds) after `start`'s t0 in which
    it does not call back."""

    def __init__(self, player: VirtualPlayer, sink: Callable[[np.ndarray], None], *,
                 stalls: tuple = (), clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, buffer_s: float = BUFFER_S) -> None:
        self.player = player
        self.sink = sink
        self.stalls = tuple(stalls)
        self.clock = clock
        self.sleep = sleep
        self.buffer_s = buffer_s
        self.rate = player.device_rate or DEVICE_RATE
        self.period = FRAMES / self.rate
        self.t0 = 0.0
        self.written = 0.0  # device seconds handed to the room since t0
        # Times the device ran dry with sound queued: (at, seconds, forced).
        self.glitches: list[tuple[float, float, bool]] = []
        self.dry_s = 0.0  # silence played for want of a callback, sound queued or not
        self.callbacks = 0
        self._down = soxr.ResampleStream(self.rate, 16_000, 1, dtype="float32")
        self._underflowed = False
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self, t0: float) -> None:
        self.t0 = t0
        self._running = True
        self._thread = threading.Thread(target=self._run, name="rehearsal-speaker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _stalled(self, at: float) -> float | None:
        """When the stall `at` is in ends, or None."""
        for start, seconds in self.stalls:
            if start <= at < start + seconds:
                return start + seconds
        return None

    def _write_silence(self, seconds: float) -> None:
        n = int(round(seconds * self.rate))
        if n > 0:
            self.sink(self._down.resample_chunk(np.zeros(n, np.float32)))
            self.written += n / self.rate

    def step(self) -> float:
        """One turn of the clock: due now, or catching up. Returns when the
        next is due (the clock's time)."""
        now = self.clock()
        at = now - self.t0
        ends = self._stalled(at)
        stream = self.player._stream
        dead = stream is None or not getattr(stream, "active", False)
        if ends is not None or dead:
            # No callback: the device plays silence up to now.
            behind = at - self.written
            if behind > 0:
                if self.player.is_playing:
                    self.glitches.append((self.written, behind, ends is not None))
                self.dry_s += behind
                self._write_silence(behind)
            self._underflowed = True
            return self.t0 + self.written + self.period
        behind = at - self.written
        if behind > 0:
            # Called back after its block was due to play: the device ran
            # dry meanwhile, and the block plays from now.
            if self.player.is_playing:
                self.glitches.append((self.written, behind, False))
            self.dry_s += behind
            self._write_silence(behind)
            self._underflowed = True
        out = np.zeros((FRAMES, 1), np.float32)
        status = types.SimpleNamespace(output_underflow=self._underflowed)
        self._underflowed = False
        self.player._callback(out, FRAMES, None, status)
        self.callbacks += 1
        self.sink(self._down.resample_chunk(out[:, 0]))
        self.written += self.period
        return self.t0 + self.written - self.buffer_s

    def _run(self) -> None:
        due = self.t0
        while self._running:
            wait = due - self.clock()
            if wait > 0:
                self.sleep(wait)
            due = self.step()


def audible_runs(played: list[tuple], *, floor: float, gap_s: float = 0.25) -> list[dict]:
    """Stretches in which one owner was audible: (owner, start, end), joining
    blocks less than `gap_s` apart, from `VirtualPlayer.played`."""
    runs: list[dict] = []
    for at, owner, level, seconds in played:
        if level < floor:
            continue
        last = runs[-1] if runs else None
        if last is not None and last["owner"] is owner and at - last["end"] <= gap_s:
            last["end"] = max(last["end"], at + seconds)
            last["peak"] = max(last["peak"], level)
        else:
            runs.append({"owner": owner, "start": at, "end": at + seconds, "peak": level})
    return runs


def kind(owner) -> str:
    """An owner's kind, as the floor names it: a turn, a live conversation, a chime."""
    if owner is None:
        return "nobody"
    return owner if isinstance(owner, str) else type(owner).__name__.lower()


def voices_at_once(runs: list[dict], *, gap_s: float = 0.3) -> list[tuple[dict, dict]]:
    """Two of the desk's voices run into each other with no pause between:
    a turn's and a live conversation's, two turns', or a sound from no owner
    at all (played around the floor). One player cannot sound two at the
    same instant, so this is what "two voices at once" looks like from the
    room: one cut straight into by another. A tick or a chime is not a
    voice."""
    voiced = [r for r in runs if kind(r["owner"]) in ("turn", "livecall", "nobody")]
    out = []
    for a, b in zip(voiced, voiced[1:], strict=False):
        if a["owner"] is not b["owner"] and b["start"] - a["end"] < gap_s:
            out.append((a, b))
    return out
