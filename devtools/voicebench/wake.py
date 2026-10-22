"""The wake rows: misses by section, false wakes an hour, and the time to the tick.

The desk's wake was tuned on how a handful of tries felt. Scored on the
maintainer's 44 takes of 14 October, its head proposed only 27 of them, even
at a low bar (07-voice.md, section 1). Here every detector is scored on the
same takes, in the same 80 ms blocks the microphone delivers. A take counts as heard if the detector
fires anywhere in it. A false wake is a fire in ordinary sound, counted
once per `REFRACTORY_S` as the desk counts them.

The check (stage 2) is scored apart, on the clip a proposal would hand it:
whether `core/desk.wake_phrase` finds the phrase in what it heard, and how
long that took on this machine's CPU.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import numpy as np

from core import desk
from devtools.voicebench.sets import Clip
from devtools.voicebench.turns import percentile

SR = 16_000
CHUNK = 1280  # voice/wake.CHUNK
REFRACTORY_S = 1.5  # voice/wake.REFRACTORY_S
# Before and after each take: faint noise, so the detector's 1.28 s window
# is full when the phrase begins and the phrase's end is inside the take.
PAD_S = 1.0
FLOOR = 1e-4


def _padded(audio: np.ndarray, seed: int = 0) -> np.ndarray:
    noise = np.random.default_rng(seed).normal(0, FLOOR, int(PAD_S * SR)).astype(np.float32)
    return np.concatenate([noise, audio, noise])


def fires(detector, audio: np.ndarray) -> list[tuple[float, float]]:
    """(when, score) of each fire, on the audio's clock, one per refractory."""
    detector.reset()
    out: list[tuple[float, float]] = []
    last = float("-inf")
    for at in range(0, len(audio) - CHUNK + 1, CHUNK):
        score = detector.push(audio[at:at + CHUNK])
        now = (at + CHUNK) / SR
        if score is not None and now - last >= REFRACTORY_S:
            out.append((now, float(score)))
            last = now
    return out


@dataclass(frozen=True)
class Take:
    name: str
    section: str
    heard: bool
    late_s: float | None  # the first fire after the phrase's end; None if missed


def takes(detector, clips: Iterable[Clip],
          phrase_end: Callable[[Clip], float | None]) -> list[Take]:
    """Each wake take through the detector. `phrase_end` says where the
    phrase ends in a take (the voice detector's last speech)."""
    out = []
    for clip in clips:
        fired = fires(detector, _padded(clip.audio))
        end = phrase_end(clip)
        late = None
        if fired and end is not None:
            late = fired[0][0] - PAD_S - end
        out.append(Take(clip.name, clip.section, bool(fired), late))
    return out


def false_wakes(detector, clips: Iterable[Clip]) -> tuple[int, float]:
    """(fires, hours) over ordinary sound."""
    count, seconds = 0, 0.0
    for clip in clips:
        count += len(fires(detector, clip.audio))
        seconds += clip.seconds
    return count, seconds / 3600


def miss_rows(results: list[Take]) -> dict:
    sections = sorted({t.section for t in results})
    lates = [t.late_s for t in results if t.late_s is not None]
    return {
        "by_section": {s: {"takes": sum(t.section == s for t in results),
                           "missed": sum(t.section == s and not t.heard for t in results)}
                       for s in sections},
        "late_p50_s": percentile(lates, 50),
        "late_p90_s": percentile(lates, 90),
    }


@dataclass(frozen=True)
class Checked:
    name: str
    accepted: bool
    seconds: float


def check(transcribe: Callable[[np.ndarray], str], clips: Iterable[Clip], *,
          clock: Callable[[], float] = time.perf_counter) -> list[Checked]:
    """Each clip through a check that writes down what it heard: accepted
    if `core/desk.wake_phrase` finds the phrase at its start."""
    out = []
    for clip in clips:
        started = clock()
        text = transcribe(clip.audio)
        took = clock() - started
        out.append(Checked(clip.name, desk.wake_phrase(text) is not None, took))
    return out


def check_rows(positives: list[Checked], negatives: list[Checked]) -> dict:
    times = [c.seconds for c in positives + negatives]
    return {
        "positives": len(positives),
        "rejected": sum(not c.accepted for c in positives),
        "negatives": len(negatives),
        "accepted": sum(c.accepted for c in negatives),
        "p50_s": percentile(times, 50),
        "p90_s": percentile(times, 90),
    }


def whisper(name: str, *, threads: int = 4, download_root: str | None = None):
    """faster-whisper on the CPU as a check: int8, English, no timestamps,
    one beam. It pads every clip to 30 seconds, which is most of its time."""
    from faster_whisper import WhisperModel

    model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads,
                         download_root=download_root)

    def transcribe(audio: np.ndarray) -> str:
        segments, _ = model.transcribe(audio, language="en", beam_size=1,
                                       without_timestamps=True, vad_filter=False,
                                       condition_on_previous_text=False)
        return " ".join(s.text for s in segments)

    return transcribe
