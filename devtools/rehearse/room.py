"""The room: what the desk's microphone hears, and what its loopback hears.

The microphone at a laptop does not hear the user alone. It hears the laptop's
own speakers a few centimetres away -- usually louder than they are, a
little late, smeared by the room and squashed by speakers too small for
the bass -- and the fan, and whatever else the laptop is playing. The echo
canceller (voice/echo.py) is given what the desk itself played and takes
its echo out; it is given nothing of a video, which another program plays.
Every one of those differences changes what survives of "hey Steve": on
the 10 October 2026 retest the raw microphone scored the user's phrase 0.91 and
the cancelled one 0.00, which only happens when the echo is well above
their voice. A rehearsal that mixed them in clean would never have shown it.

So the mixer here is sample-accurate and causal, built from four plain
parts -- a delay, a room's impulse response (a direct path and an
exponentially decaying tail), a gain, and a soft clip -- applied to what
the desk's speaker actually played (`write_desk`, from devtools/rehearse/
speaker.py) and to a video's sound, with the user's lines and room tone added.
The loopback is the laptop's output itself, digital and undelayed, as
WASAPI's loopback capture is.

Everything is 16 kHz, the ear's rate, on one sample clock that starts at
the room's t0.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

SR = 16_000


def gain(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def rms(audio: np.ndarray) -> float:
    audio = np.asarray(audio, np.float64)
    return float(np.sqrt(np.mean(audio * audio))) if len(audio) else 0.0


def dbfs(audio: np.ndarray) -> float:
    level = rms(audio)
    return 20.0 * np.log10(level) if level > 0 else float("-inf")


@dataclass
class Acoustics:
    """How the room carries sound to the microphone.

    echo      the speakers' sound at the microphone against what was played, dB
              (laptop speakers beside the microphone: near 0, often above the user)
    delay     from the speaker callback to the echo at the microphone, ms
              (the output buffer and the room)
    rt60      how long the room's echo takes to die by 60 dB, seconds
    reverb    the tail's energy against the direct path's
    drive     soft clipping of small speakers (0 is clean, 2-3 is a laptop's)
    highpass  the speakers' lowest frequency, Hz (0: none)
    noise     room tone, dBFS
    """

    # A desk by default: a webcam on the monitor, the laptop's speakers a
    # little way off. On the 10 October retest "hey Steve, stop" over a local
    # answer scored 0.95 raw and 0.99 after the canceller; a room at echo 0 dB
    # and drive 2 left 0.20 of it -- harsher than that one, so it is a variant a
    # scenario asks for (`room echo 0`, `room drive 2`), not the default.
    echo: float = -12.0
    delay: float = 60.0
    rt60: float = 0.3
    reverb: float = 0.25
    drive: float = 1.0
    highpass: float = 200.0
    noise: float = -62.0

    @classmethod
    def of(cls, knobs: dict) -> Acoustics:
        return cls(**{k: float(v) for k, v in knobs.items()})


def exponential_rir(rt60_s: float, reverb: float, *, rate: int = SR, seed: int = 0) -> np.ndarray:
    """A room's impulse response at its simplest: the direct path, then noise
    decaying exponentially to -60 dB at `rt60_s`, carrying `reverb` of the
    direct path's energy."""
    n = int(rt60_s * rate)
    if n < 2 or reverb <= 0:
        return np.ones(1, np.float64)
    t = np.arange(n) / rate
    tail = np.random.default_rng(seed).standard_normal(n) * np.exp(-6.91 * t / rt60_s)
    tail[0] = 0.0
    tail *= np.sqrt(reverb / np.sum(tail * tail))
    tail[0] = 1.0
    return tail


def highpass_fir(hz: float, *, rate: int = SR, taps: int = 129) -> np.ndarray:
    """A linear-phase high-pass: what speakers too small for the bass do."""
    if hz <= 0:
        return np.ones(1, np.float64)
    n = np.arange(taps) - (taps - 1) / 2
    low = 2 * hz / rate * np.sinc(2 * hz / rate * n) * np.hamming(taps)
    low /= low.sum()
    out = -low
    out[(taps - 1) // 2] += 1.0
    return out


def lowpass_fir(hz: float, *, rate: int, taps: int = 97) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    out = 2 * hz / rate * np.sinc(2 * hz / rate * n) * np.hamming(taps)
    return out / out.sum()


class Convolver:
    """Streaming convolution by FFT overlap-add: any block length in, the same
    length out, the response's tail carried into the next block."""

    def __init__(self, response: np.ndarray) -> None:
        self.response = np.asarray(response, np.float64)
        self._carry = np.zeros(len(self.response) - 1)
        self._spectra: dict[int, np.ndarray] = {}

    def push(self, block: np.ndarray) -> np.ndarray:
        x = np.asarray(block, np.float64)
        m, taps = len(x), len(self.response)
        if m == 0:
            return np.zeros(0, np.float32)
        size = 1 << (m + taps - 2).bit_length()
        spectrum = self._spectra.get(size)
        if spectrum is None:
            spectrum = self._spectra[size] = np.fft.rfft(self.response, size)
        y = np.fft.irfft(np.fft.rfft(x, size) * spectrum, size)[:m + taps - 1]
        y[:len(self._carry)] += self._carry
        self._carry = y[m:].copy()
        return y[:m].astype(np.float32)


class Delay:
    """A delay line of `n` samples."""

    def __init__(self, n: int) -> None:
        self._held = np.zeros(max(0, int(n)), np.float32)

    def push(self, block: np.ndarray) -> np.ndarray:
        if not len(self._held):
            return np.asarray(block, np.float32)
        joined = np.concatenate([self._held, np.asarray(block, np.float32)])
        out, self._held = joined[:len(block)], joined[len(block):]
        return out


def soft_clip(audio: np.ndarray, drive: float) -> np.ndarray:
    """A small speaker's compression: unity gain for quiet sound, squashed
    towards 1/drive for loud. Clean at drive 0."""
    if drive <= 0:
        return np.asarray(audio, np.float32)
    return (np.tanh(drive * np.asarray(audio, np.float64)) / drive).astype(np.float32)


class EchoPath:
    """What the microphone hears of what the laptop's speakers played:
    clipped by the speaker, carried by the room, `delay` late, at `echo` dB."""

    def __init__(self, acoustics: Acoustics, *, seed: int = 0) -> None:
        response = exponential_rir(acoustics.rt60, acoustics.reverb, seed=seed)
        response = np.convolve(response, highpass_fir(acoustics.highpass))
        self.drive = acoustics.drive
        self.level = gain(acoustics.echo)
        self.room = Convolver(response * self.level)
        self.delay = Delay(int(acoustics.delay / 1000.0 * SR))

    def push(self, played: np.ndarray) -> np.ndarray:
        return self.delay.push(self.room.push(soft_clip(played, self.drive)))


class Timeline:
    """Sounds placed at sample positions, rendered as their sum."""

    def __init__(self) -> None:
        self._items: list[tuple[int, np.ndarray]] = []
        self._lock = threading.Lock()

    def place(self, audio: np.ndarray, start: int) -> tuple[int, int]:
        audio = np.asarray(audio, np.float32)
        with self._lock:
            self._items.append((int(start), audio))
        return int(start), int(start) + len(audio)

    def render(self, start: int, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        with self._lock:
            items = list(self._items)
        for at, audio in items:
            lo, hi = max(start, at), min(start + n, at + len(audio))
            if lo < hi:
                out[lo - start:hi - start] += audio[lo - at:hi - at]
        return out


class Tape:
    """A growing recording on the room's sample clock: written in order by one
    thread, read by others once written."""

    def __init__(self, seconds: float) -> None:
        self.data = np.zeros(int(seconds * SR), np.float32)
        self.n = 0
        self._lock = threading.Lock()

    def write(self, audio: np.ndarray) -> None:
        audio = np.asarray(audio, np.float32)
        with self._lock:
            end = min(len(self.data), self.n + len(audio))
            self.data[self.n:end] = audio[:end - self.n]
            self.n += len(audio)

    def put(self, start: int, audio: np.ndarray) -> None:
        """Written at a position, for a recording not made in order."""
        audio = np.asarray(audio, np.float32)
        with self._lock:
            lo, hi = max(0, start), min(len(self.data), start + len(audio))
            if lo < hi:
                self.data[lo:hi] = audio[lo - start:hi - start]
            self.n = max(self.n, start + len(audio))

    def read(self, start: int, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        lo, hi = max(0, start), min(len(self.data), start + n, self.n)
        if lo < hi:
            out[lo - start:hi - start] = self.data[lo:hi]
        return out


class Room:
    """The room on one sample clock. The speaker writes what the desk played
    (`write_desk`); the microphone's thread reads blocks in order (`mic`),
    the loopback's at its own pace (`loopback`)."""

    def __init__(self, acoustics: Acoustics, *, seconds: float, seed: int = 0) -> None:
        self.acoustics = acoustics
        self.people = Timeline()   # the user, and anyone else in the room
        self.video = Timeline()    # what another program plays on the laptop
        self.desk = Tape(seconds)  # what the desk's speaker played
        self.heard = Tape(seconds)    # what the microphone heard, raw
        self.cleaned = Tape(seconds)  # the same after the echo canceller
        self.echo = EchoPath(acoustics, seed=seed)
        self._noise = np.random.default_rng(seed + 1)
        self._noise_level = gain(acoustics.noise)
        self._mic_at = 0

    def write_desk(self, audio: np.ndarray) -> None:
        self.desk.write(audio)

    def laptop(self, start: int, n: int) -> np.ndarray:
        """What the laptop played: the desk's sound and a video's."""
        return self.desk.read(start, n) + self.video.render(start, n)

    def mic(self, start: int, n: int) -> np.ndarray:
        """The microphone's block at `start`. In order: the room's echo is a
        stream, and a block skipped would leave a hole in its memory."""
        if start != self._mic_at:
            # The thread fell behind and skipped ahead: the echo path is
            # fed silence for what it missed, as a real room keeps time.
            gap = start - self._mic_at
            if gap > 0:
                self.echo.push(np.zeros(gap, np.float32))
        self._mic_at = start + n
        echo = self.echo.push(self.laptop(start, n))
        noise = self._noise_level * self._noise.standard_normal(n).astype(np.float32)
        block = (self.people.render(start, n) + echo + noise).astype(np.float32)
        self.heard.put(start, block)
        return block

    def loopback(self, start: int, n: int) -> np.ndarray:
        return self.laptop(start, n)
