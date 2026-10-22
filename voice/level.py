"""The voice's level: how loudly the microphone hears and the speakers play,
for the app's mark and voice button (docs/design/presence.md, Listening and
Speaking).

Nothing measured it live. The mark showed the same still diamond whether
the microphone heard the user or nothing at all, and nothing while Steve
spoke, so a dead microphone looked like a working one until the turn came
back empty.

The audio callbacks see every block already. Each keeps its last level
here in one assignment: PortAudio's thread must take no lock it could wait
on (voice/audio.py, `Player._callback`), and setting a dict's item takes
none. The worker reads them a few times a second and tells every open
window (agent/worker/background.py, `voice_level`).
"""

from __future__ import annotations

import math
import time

# The last block heard and played: when, on `time.monotonic`, and its RMS.
LAST: dict[str, tuple[float, float]] = {"heard": (0.0, 0.0), "played": (0.0, 0.0)}
# A level older than this is none: its stream has stopped calling back.
FRESH_S = 0.3
# Speech's range in decibels below full scale, drawn from 0 to 1: a quiet
# room at the bottom, a raised voice at the top.
QUIET_DB, LOUD_DB = -55.0, -15.0


def keep(which: str, rms: float) -> None:
    """From an audio callback: the RMS of the block just heard or played."""
    LAST[which] = (time.monotonic(), rms)


def scaled(rms: float) -> float:
    """An RMS as the mark draws it, in hundredths from 0 to 1."""
    if rms <= 0:
        return 0.0
    db = 20 * math.log10(rms)
    return round(min(1.0, max(0.0, (db - QUIET_DB) / (LOUD_DB - QUIET_DB))), 2)


def now(at: float | None = None) -> tuple[str, float] | None:
    """What the voice is doing and how loudly: listening while the
    microphone takes blocks, or else speaking while the speakers play; None
    once neither has for `FRESH_S`. Listening first: a press stops what was
    being said, and its click plays as the microphone opens."""
    at = time.monotonic() if at is None else at
    for of, which in (("listening", "heard"), ("speaking", "played")):
        when, rms = LAST[which]
        if at - when <= FRESH_S:
            return of, scaled(rms)
    return None
