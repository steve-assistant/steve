"""The bench's recordings, read from the folders the recorder writes.

Each session's manifest says what its takes are (`record.py`). This turns a
folder into clips with their labels, so no scorer parses a manifest itself
and no two scorers can read one differently. The labels are kept as the
speaker gave them: where a phrase or a request ended is found later, from
the audio, by the scorer that needs it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from devtools.voicebench.record import SR, read_wav


@dataclass(frozen=True, eq=False)
class Clip:
    name: str  # the file, relative to its session's folder
    audio: np.ndarray  # 16 kHz mono float32
    section: str = ""  # session a: plain, request, far, media, near
    enter_s: float | None = None  # session b: when the speaker pressed Enter
    two_part: bool = False  # session b: the prompt invited a request in two parts
    loopback: np.ndarray | None = None  # session c: what the speakers played, at 16 kHz
    play_s: float | None = None  # session c: when the answer began, on the mic's clock
    prompt_s: float | None = None  # session c: when the screen asked for "stop"

    @property
    def seconds(self) -> float:
        return len(self.audio) / SR


@dataclass(frozen=True, eq=False)
class Session:
    kind: str  # a, b, c or d (record.SESSIONS)
    folder: Path
    clips: tuple[Clip, ...]

    @property
    def hours(self) -> float:
        return sum(c.seconds for c in self.clips) / 3600


def to_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    """A recording at a whole multiple of 16 kHz (the speakers' loopback is
    48 kHz), brought down to it: averaged over each run of samples, which
    is filter enough for a level and a detector, not for listening."""
    if rate == SR:
        return audio
    if rate % SR:
        raise ValueError(f"cannot bring {rate} Hz down to {SR}")
    step = rate // SR
    usable = len(audio) // step * step
    return audio[:usable].reshape(-1, step).mean(axis=1).astype(np.float32)


def _audio(path: Path) -> np.ndarray:
    audio, rate = read_wav(path)
    return to_16k(audio, rate)


def load(folder: Path) -> Session:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    kind = manifest["session"]
    clips = []
    for take in manifest["takes"]:
        if kind == "c":
            clips.append(Clip(name=take["mic"], audio=_audio(folder / take["mic"]),
                              loopback=_audio(folder / take["loopback"]),
                              play_s=take["play_s"], prompt_s=take["prompt_s"]))
        else:
            clips.append(Clip(name=take["file"], audio=_audio(folder / take["file"]),
                              section=take.get("section", ""), enter_s=take.get("enter_s"),
                              two_part=bool(take.get("two_part", False))))
    return Session(kind=kind, folder=folder, clips=tuple(clips))


def find(root: Path, kinds: str = "") -> dict[str, list[Session]]:
    """Every session under `root` (the recorder's folders, each with its
    manifest), by kind, oldest first; only those of `kinds` if it is given.
    A folder's kind is the last letter of its name, so a session still being
    recorded is not opened at all when it is not asked for: on Windows a
    file held open for reading can make the recorder's save fail."""
    found: dict[str, list[Session]] = {}
    for manifest in sorted(root.glob("*/manifest.json")):
        if kinds and manifest.parent.name[-1:] not in kinds:
            continue
        session = load(manifest.parent)
        found.setdefault(session.kind, []).append(session)
    return found
