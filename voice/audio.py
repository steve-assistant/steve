"""Microphone capture and speaker playback.

Two facts about this machine shape the whole module, and both were measured
rather than assumed:

* **WASAPI shared mode only accepts a device's native rate.** The webcam mic
  runs at 32kHz and the headphones at 48kHz; asking for 16kHz returns
  PaErrorCode -9997. So capture happens at whatever the device offers and is
  resampled here -- Whisper wants 16kHz mono and Kokoro emits 24kHz, and
  neither matches any real device.
* Resampling is free at this scale: soxr converts three seconds of audio in
  about a millisecond, which is noise next to a 270ms transcription.

Playback is queue-driven rather than blocking, because replies are spoken
sentence by sentence: the first clause starts playing while the rest is still
being synthesised, and an interruption has to be able to drop what is queued
without waiting for it.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import sounddevice as sd
import soxr

from voice import level

log = logging.getLogger("voice")

# What Whisper was trained on. Not negotiable, and not what any device offers.
STT_SAMPLE_RATE = 16_000

# Capture is mono: Whisper mixes to mono anyway, and asking a 4-channel array
# mic for one channel lets WASAPI do the downmix in the driver.
CHANNELS = 1

_BLOCKSIZE = 1024

# A speaker callback that comes this much later than its block's length
# after the one before it is late: the device asked for sound and nobody
# answered in time, and it may have played a glitch meanwhile. (WASAPI asks
# in bursts of its own period, so a little lateness is ordinary.)
LATE_SLACK_S = 0.05


@dataclass
class Device:
    index: int
    name: str
    hostapi: str
    sample_rate: int
    channels: int
    is_default: bool = False

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "hostapi": self.hostapi,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "default": self.is_default,
        }


def _hostapi_index(preferred: str = "WASAPI") -> int:
    for i, api in enumerate(sd.query_hostapis()):
        if preferred.lower() in api["name"].lower():
            return i
    return sd.default.hostapi


def list_devices(kind: str = "input", hostapi: str = "WASAPI") -> list[Device]:
    """Devices on one host API.

    Restricted to a single API on purpose: Windows exposes the same physical
    microphone four times over (MME, DirectSound, WASAPI, WDM-KS), and showing
    all of them turns device selection into a guessing game. WASAPI is the
    modern low-latency one.
    """
    api_index = _hostapi_index(hostapi)
    api = sd.query_hostapis(api_index)
    default = api["default_input_device"] if kind == "input" else api["default_output_device"]
    key = "max_input_channels" if kind == "input" else "max_output_channels"

    out: list[Device] = []
    for index, info in enumerate(sd.query_devices()):
        if info["hostapi"] != api_index or info[key] <= 0:
            continue
        out.append(
            Device(
                index=index,
                name=info["name"],
                hostapi=api["name"],
                sample_rate=int(info["default_samplerate"]),
                channels=int(info[key]),
                is_default=(index == default),
            )
        )
    return out


def default_device(kind: str = "input", hostapi: str = "WASAPI") -> Device | None:
    for device in list_devices(kind, hostapi):
        if device.is_default:
            return device
    devices = list_devices(kind, hostapi)
    return devices[0] if devices else None


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return audio.astype(np.float32, copy=False)
    return soxr.resample(audio.astype(np.float32, copy=False), src_rate, dst_rate)


# ---------------------------------------------------------------------------
# The desk's own sounds
# ---------------------------------------------------------------------------


def tone(notes: tuple[float, ...], seconds: float, level: float) -> tuple[np.ndarray, int]:
    rate = 24_000
    parts = []
    for freq in notes:
        t = np.arange(int(seconds * rate)) / rate
        parts.append(level * np.sin(2 * np.pi * freq * t) * np.hanning(len(t)))
    return np.concatenate(parts).astype(np.float32), rate


# On every wake the user is heard: a soft tick, 60 ms (the maintainer, 9 October 2026 -- they
# could not tell whether two "hey Steve"s had been heard); and on every
# push-to-talk press, once the microphone is live. A pure tone, not
# speech: it trips neither the wake detector nor the voice detector on the
# loopback (tests/unit/test_desk_engine.py).
TICK = tone((1320.0,), 0.06, 0.05)
# How long the tick may be heard in the microphone after the press: its own
# 60 ms, and the output's latency on a laptop. Its silence is judged after it.
TICK_SPAN_S = 0.25


def tick_alone() -> None:
    """The tick, for a press with no session to play it: the first press
    after every start, while the models load (agent/worker/routes.py). It
    played none, so the first press of the day was the one a user could not
    tell had been heard. Never a reason to lose the press."""
    try:
        sd.play(*TICK)
    except Exception:  # noqa: BLE001 - the capture matters, the tick does not
        log.warning("could not play the tick", exc_info=True)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


@dataclass
class Recording:
    audio: np.ndarray  # mono float32 at STT_SAMPLE_RATE
    sample_rate: int = STT_SAMPLE_RATE
    device_rate: int = 0
    # From the press (`Recorder.start`) to the microphone's first sample: the
    # wait in which a word begun at the press is lost. None if none came.
    live_ms: int | None = None

    @property
    def duration_s(self) -> float:
        return len(self.audio) / self.sample_rate

    def silent_after(self, seconds: float) -> bool:
        """Whether the clip after its first `seconds` is room tone, below
        which Whisper hallucinates: left-on microphones produce confident
        transcriptions of nothing -- "Thank you." and "Bye." are the classic
        outputs -- so it is better to return nothing than to send silence to
        the model.

        The first `seconds` are skipped because the press's own tick plays as
        the microphone opens, and on a laptop's speakers it can reach it.
        Heard in, an empty press passed as speech, and was sent to be
        transcribed and billed (audit of 16 October 2026)."""
        rest = self.audio[int(seconds * self.sample_rate):]
        return not len(rest) or float(np.abs(rest).max()) < 0.01


class Recorder:
    """Push-to-talk capture.

    Deliberately not voice-activity gated: with a hotkey the user has already
    said exactly when the turn starts and ends, and a VAD can only disagree
    with them.
    """

    def __init__(self, device: int | None = None, max_seconds: float = 120.0) -> None:
        self.device = device
        self.max_seconds = max_seconds
        self._stream: sd.InputStream | None = None
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._device_rate = 0
        self.recording = False
        # When `start` was called, and when the first block's first sample
        # was heard, on `time.perf_counter`'s clock.
        self._asked = 0.0
        self._first: float | None = None

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001, ARG002
        # Runs on PortAudio's thread. Copy, because indata is reused.
        with self._lock:
            if self._first is None and self._device_rate:
                # The block arrives full: its first sample was heard its
                # length ago.
                self._first = time.perf_counter() - frames / self._device_rate
            block = indata[:, 0].copy()
            self._frames.append(block)
        level.keep("heard", float(np.sqrt(np.dot(block, block) / max(len(block), 1))))

    @property
    def live_ms(self) -> int | None:
        """From the press to the first sample heard; None until one is."""
        first = self._first
        return None if first is None else max(0, int((first - self._asked) * 1000))

    def start(self, pressed: float | None = None) -> None:
        """Open the microphone. `pressed`, the press's `time.perf_counter()`,
        starts the live time's clock: started here, whatever ran between the
        press and the open (a reply stopped, the desk's conversation ended)
        went unseen, and session E's bar was measured short of it."""
        if self.recording:
            return
        self._asked = time.perf_counter() if pressed is None else pressed
        self._first = None
        device = self.device if self.device is not None else default_device("input").index
        self._device_rate = int(sd.query_devices(device)["default_samplerate"])

        with self._lock:
            self._frames = []
        self._stream = sd.InputStream(
            device=device,
            channels=CHANNELS,
            samplerate=self._device_rate,  # native only; see module docstring
            dtype="float32",
            blocksize=_BLOCKSIZE,
            callback=self._callback,
        )
        self._stream.start()
        self.recording = True

    def stop(self) -> Recording:
        if not self.recording or self._stream is None:
            return Recording(audio=np.zeros(0, dtype=np.float32))

        # Let go first: a device unplugged while held fails `stop`, and a
        # recorder still marked recording returned early from every later
        # press until the worker restarted.
        stream, self._stream = self._stream, None
        self.recording = False
        try:
            stream.stop()
        finally:
            with contextlib.suppress(Exception):
                stream.close()

        with self._lock:
            frames, self._frames = self._frames, []

        if not frames:
            return Recording(audio=np.zeros(0, dtype=np.float32), device_rate=self._device_rate)

        raw = np.concatenate(frames)
        limit = int(self.max_seconds * self._device_rate)
        if len(raw) > limit:
            raw = raw[-limit:]  # keep the end: that is what was said most recently

        return Recording(
            audio=resample(raw, self._device_rate, STT_SAMPLE_RATE),
            device_rate=self._device_rate,
            live_ms=self.live_ms,
        )

    def __enter__(self) -> Recorder:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.recording:
            self.stop()


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


@dataclass
class _Playback:
    chunks: deque = field(default_factory=deque)
    position: int = 0
    # Who queued each chunk, in step with `chunks` (voice/floor.py); a chunk
    # queued with no owner has none.
    owners: deque = field(default_factory=deque)


class CallbackStats:
    """How the speaker stream's callbacks keep time: how many came, how many
    were late, the longest gap between two and the longest one took --
    since the floor last read them (`Player.take_stats`). Written by the
    audio thread alone, so it takes no lock; a read that races a write
    loses one sample, nothing more."""

    def __init__(self) -> None:
        self._last: float | None = None
        self.reset()

    def reset(self) -> None:
        self.n = 0
        self.late = 0
        self.underflows = 0
        self.max_gap_s = 0.0
        self.max_run_s = 0.0

    def add(self, at: float, run_s: float, period_s: float, status) -> None:
        if self._last is not None:
            gap = at - self._last
            self.max_gap_s = max(self.max_gap_s, gap)
            if gap > period_s + LATE_SLACK_S:
                self.late += 1
        self._last = at
        self.n += 1
        self.max_run_s = max(self.max_run_s, run_s)
        if getattr(status, "output_underflow", False):
            self.underflows += 1

    def take(self) -> dict:
        out = {"n": self.n, "late": self.late, "underflows": self.underflows,
               "max_gap_s": self.max_gap_s, "max_run_s": self.max_run_s}
        self.reset()
        return out


class Player:
    """Streaming playback with immediate interruption.

    A single output stream stays open and pulls from a queue. Queuing rather
    than calling sd.play() per sentence matters twice: consecutive sentences
    join without a gap, and stop() can discard everything pending in the time
    it takes to clear a deque, which is what barge-in needs.
    """

    def __init__(self, device: int | None = None) -> None:
        self.device = device
        self._stream: sd.OutputStream | None = None
        self._state = _Playback()
        self._lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._drained = threading.Event()
        self._drained.set()
        self.device_rate = 0
        self.clock = time.monotonic
        # What was played, block by block, for the desk's floor
        # (voice/floor.py) to read: (when, owner, loudness, seconds).
        # "Speaking" was inferred from the queue not being empty, and on 9
        # October 2026 a live conversation's silent frames kept it "speaking"
        # for minutes; what is measured here is what came out.
        self.blocks: deque = deque(maxlen=4096)
        # How the callbacks keep time, for the floor's report.
        self.stats = CallbackStats()
        # Each block as it goes to the speakers, silence included, for the
        # desk's echo canceller (voice/echo.py) -- handed over on a thread of
        # its own (`on_play`).
        self._on_play = None
        self._far: queue.SimpleQueue = queue.SimpleQueue()
        self._far_thread: threading.Thread | None = None
        # When the stream last asked for audio: a stream that has stopped
        # calling back is not playing what it holds.
        self.last_callback = 0.0

    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001, ARG002
        """PortAudio's thread, every 21 ms: copy what is queued and return.

        It also told the floor and fed the echo canceller here, each behind
        a lock of its own, and on the 9 October 2026 retest the stream went
        without a callback for over a second mid-reply. A callback that
        waits on a lock gives up the GIL and must win it back from a busy
        event loop, and the device plays a glitch meanwhile. So it takes no
        lock but the queue's: what it played is left in `blocks` for the
        floor to read, and the far end goes to the canceller's thread."""
        started = self.clock()
        outdata.fill(0)
        written = 0
        owner = None
        with self._lock:
            while written < frames and self._state.chunks:
                chunk = self._state.chunks[0]
                if owner is None and self._state.owners:
                    owner = self._state.owners[0]
                start = self._state.position
                take = min(frames - written, len(chunk) - start)
                outdata[written : written + take, 0] = chunk[start : start + take]
                written += take
                self._state.position += take
                if self._state.position >= len(chunk):
                    self._state.chunks.popleft()
                    if self._state.owners:
                        self._state.owners.popleft()
                    self._state.position = 0
            if not self._state.chunks and written < frames:
                self._drained.set()
        self.last_callback = started
        rate = max(self.device_rate, 1)
        if written:
            played = outdata[:written, 0]
            rms = float(np.sqrt(np.dot(played, played) / written))
            self.blocks.append((started, owner, rms, written / rate))
            level.keep("played", rms)
        if self._on_play is not None:
            self._far.put_nowait(outdata[:, 0].copy())
        self.stats.add(started, self.clock() - started, frames / rate, status)

    @property
    def on_play(self):
        """Told each block as it went to the speakers, silence included: the
        far end for the echo canceller. Called on a thread of its own, never
        the audio thread's, a moment after the block was played -- well
        ahead of its echo, which the microphone hands on 100 ms at a time."""
        return self._on_play

    @on_play.setter
    def on_play(self, fn) -> None:
        self._on_play = fn
        if fn is not None and (self._far_thread is None or not self._far_thread.is_alive()):
            self._far_thread = threading.Thread(target=self._far_end, name="player-far",
                                                daemon=True)
            self._far_thread.start()

    def _far_end(self) -> None:
        while True:
            self._tell_far(self._far.get())

    def _tell_far(self, block: np.ndarray) -> None:
        fn = self._on_play
        if fn is None:
            return
        try:
            fn(block, self.device_rate)
        except Exception:  # noqa: BLE001 - playback outranks the canceller
            log.exception("the echo canceller's far end failed; it is no longer told")
            self._on_play = None

    def drain_blocks(self) -> list[tuple]:
        """What was played since the last call: (when, owner, rms, seconds)."""
        out = []
        while True:
            try:
                out.append(self.blocks.popleft())
            except IndexError:
                return out

    def take_stats(self) -> dict:
        return self.stats.take()

    @property
    def running(self) -> bool:
        """Whether the stream says it is running -- which a stream that has
        merely fallen behind still does."""
        stream = self._stream
        if stream is None:
            return False
        try:
            return bool(stream.active)
        except Exception:  # noqa: BLE001 - a stream that cannot say is not running
            return False

    def reopen(self) -> float:
        """A stream that has stopped calling back: abandoned, what it held
        dropped, and a new one opened. Returns the seconds of sound dropped.

        Nothing queued is carried over. A chunk resumed where the old stream
        left it starts mid-word, and the old stream is aborted -- what it
        had buffered discarded -- not stopped, which would play it out."""
        with self._stream_lock:
            dropped = self.queued_seconds
            old, self._stream = self._stream, None
            with self._lock:
                self._state.chunks.clear()
                self._state.owners.clear()
                self._state.position = 0
                self._drained.set()
            if old is not None:
                with contextlib.suppress(Exception):
                    old.abort()
                with contextlib.suppress(Exception):
                    old.close()
            self._open()
        return dropped

    def _ensure_stream(self) -> None:
        """An output stream that is running. One that has stopped -- the
        device went away, or the callback raised -- is closed and opened
        again, and what it had queued is dropped: it never drained, and on
        the 9 October 2026 sound test a queue like that read as "still
        speaking" for minutes, holding a live conversation open, with every
        later reply silent until the worker restarted."""
        with self._stream_lock:
            if self._stream is not None:
                if self.running:
                    return
                log.warning("the speaker stream had stopped; opening it again")
                with contextlib.suppress(Exception):
                    self._stream.close()
                self._stream = None
                with self._lock:
                    self._state.chunks.clear()
                    self._state.owners.clear()
                    self._state.position = 0
                    self._drained.set()
            self._open()

    def _open(self) -> None:
        device = self.device if self.device is not None else default_device("output").index
        self.device_rate = int(sd.query_devices(device)["default_samplerate"])
        self._stream = sd.OutputStream(
            device=device,
            channels=CHANNELS,
            samplerate=self.device_rate,
            dtype="float32",
            blocksize=_BLOCKSIZE,
            callback=self._callback,
        )
        self._stream.start()

    def enqueue(self, audio: np.ndarray, sample_rate: int, owner=None) -> None:
        """Add audio to the queue, resampling to the device's rate."""
        self._ensure_stream()
        converted = resample(audio, sample_rate, self.device_rate)
        with self._lock:
            # Owners stay in step with chunks only if every chunk has one.
            if len(self._state.owners) != len(self._state.chunks):
                self._state.owners = deque([None] * len(self._state.chunks))
            self._state.chunks.append(converted)
            self._state.owners.append(owner)
            self._drained.clear()

    def play(self, audio: np.ndarray, sample_rate: int, *, wait: bool = True) -> None:
        self.enqueue(audio, sample_rate)
        if wait:
            self.wait()

    def stop(self) -> bool:
        """Drop everything queued. Returns whether anything was actually cut off."""
        return bool(self.take())

    def take(self) -> list[np.ndarray]:
        """Drop everything queued and return it, at the device's rate, the
        chunk being played from where it had reached -- so a reply stopped by
        a false alarm can be put back (voice/floor.Floor.resume)."""
        with self._lock:
            chunks = list(self._state.chunks)
            if chunks and self._state.position:
                chunks[0] = chunks[0][self._state.position:]
            self._state.chunks.clear()
            self._state.owners.clear()
            self._state.position = 0
            self._drained.set()
        return [c for c in chunks if len(c)]

    def wait(self, timeout: float | None = None) -> bool:
        return self._drained.wait(timeout)

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return bool(self._state.chunks)

    @property
    def queued_seconds(self) -> float:
        with self._lock:
            if not self.device_rate:
                return 0.0
            total = sum(len(c) for c in self._state.chunks) - self._state.position
        return max(total, 0) / self.device_rate

    def close(self) -> None:
        self.stop()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def __enter__(self) -> Player:
        self._ensure_stream()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def describe_devices() -> dict:
    """For the status endpoint and the device picker in the UI."""
    inputs = list_devices("input")
    outputs = list_devices("output")
    return {
        "input": [d.as_dict() for d in inputs],
        "output": [d.as_dict() for d in outputs],
        "default_input": next((d.as_dict() for d in inputs if d.is_default), None),
        "default_output": next((d.as_dict() for d in outputs if d.is_default), None),
        "stt_sample_rate": STT_SAMPLE_RATE,
    }


__all__ = [
    "STT_SAMPLE_RATE",
    "Device",
    "Player",
    "Recorder",
    "Recording",
    "default_device",
    "describe_devices",
    "list_devices",
    "resample",
]
