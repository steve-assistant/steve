"""The turn rows: where the desk would have ended each of session B's requests.

The desk has cut people off mid-request since 7 October, and every fix was
judged by saying a few sentences at it. A copy of its policy here would
drift from the real one the first time either changed. So the runner
drives the real `voice/listen.Ear`. Each recording goes through it in the
microphone's 80 ms blocks, on a clock that is the recording's own time,
starting as a wake's capture starts. Only the judge, the voice detector and
the policy's numbers come from the candidate.

The label is the speaker's: they pressed Enter when they had finished.
Their last word ends at the voice detector's last speech before that
moment. A capture that ended before that word is an early cut, and how long
after it the capture ended is the hold.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np

from devtools.voicebench.sets import Clip
from voice import listen

SR = listen.SR
CHUNK = 1280  # voice/wake.CHUNK: the blocks the microphone delivers
STEP_S = listen.Vad.STEP / SR
# A take begins this long before its first speech: the tick has sounded and
# the speaker is starting. The recorder's takes begin when the prompt
# appears, and a speaker who reads it for longer than WAKE_WAIT_S would
# otherwise be "no request", which session B is not measuring.
LEAD_S = 0.3
# Silence run on after the recording, so the longest pause can elapse even
# when the speaker pressed Enter the moment they stopped.
AFTER_S = 3.0


@dataclass(frozen=True)
class Policy:
    """A candidate's numbers, named as `voice/listen` names them."""

    name: str
    ask_s: tuple[float, ...] = listen.TURN_ASK_S
    finished: float = listen.TURN_FINISHED
    max_s: float = listen.END_SILENCE_MAX_S
    timer_s: float = listen.END_SILENCE_S


TODAY = Policy("today")


@contextlib.contextmanager
def applied(policy: Policy) -> Iterator[None]:
    """`voice/listen`'s numbers set to a candidate's for one run, and put
    back. The ear reads them as module globals at each decision, which is
    what lets the real ear run a policy it does not ship."""
    names = {"TURN_ASK_S": policy.ask_s, "TURN_FINISHED": policy.finished,
             "END_SILENCE_MAX_S": policy.max_s, "END_SILENCE_S": policy.timer_s}
    before = {name: getattr(listen, name) for name in names}
    try:
        for name, value in names.items():
            setattr(listen, name, value)
        yield
    finally:
        for name, value in before.items():
            setattr(listen, name, value)


@dataclass(frozen=True)
class Speech:
    """What the voice detector heard in a take, before Enter."""

    first_s: float | None  # the first speech step's start
    last_s: float | None  # the last speech step's end: the end of the last word
    longest_pause_s: float  # the longest quiet between the first and the last


def speech(audio: np.ndarray, vad, until_s: float | None = None) -> Speech:
    vad.reset()
    steps = vad.push(audio)
    if until_s is not None:
        steps = steps[:int(until_s / STEP_S)]
    spoken = [i for i, s in enumerate(steps) if s]
    if not spoken:
        return Speech(None, None, 0.0)
    gaps = [b - a - 1 for a, b in zip(spoken, spoken[1:], strict=False)]
    return Speech(spoken[0] * STEP_S, (spoken[-1] + 1) * STEP_S,
                  max(gaps, default=0) * STEP_S)


@dataclass(frozen=True)
class Turn:
    name: str
    two_part: bool
    last_s: float | None  # the end of the speaker's last word, on the take's clock
    longest_pause_s: float
    ended_s: float | None  # when the ear handed the capture on; None if it never did
    ended_by: str
    asks: tuple[tuple[float, float], ...]  # (seconds of quiet, the judge's chance)

    @property
    def early(self) -> bool:
        return self.ended_s is not None and self.last_s is not None and self.ended_s < self.last_s

    @property
    def hold_s(self) -> float | None:
        if self.ended_s is None or self.last_s is None or self.early:
            return None
        return self.ended_s - self.last_s


class _FiresFirst:
    """A wake detector that fires on the first block it is given: the
    capture starts with the take, as one starts at the tick."""

    threshold = 0.5
    last_score = None

    def __init__(self) -> None:
        self._fired = False

    def push(self, audio: np.ndarray) -> float | None:
        if self._fired:
            return None
        self._fired = True
        return 1.0

    def reset(self) -> None:
        pass


class _Never:
    threshold = 0.5
    last_score = None

    def push(self, audio: np.ndarray) -> None:
        return None

    def reset(self) -> None:
        pass


class _Silent:
    """The loopback's voice detector: the laptop played nothing in session B."""

    def reset(self) -> None:
        pass

    def push(self, audio: np.ndarray) -> list[bool]:
        return [False] * (len(audio) // listen.Vad.STEP)


def _no_wall_clock() -> float:
    raise AssertionError("the bench runs on the recording's clock, never the wall's")


def run(clip: Clip, *, vad, judge: Callable[[np.ndarray], float | None] | None,
        policy: Policy = TODAY) -> Turn:
    """One take through the real ear: where it ended the capture, and why."""
    heard = speech(clip.audio, vad, clip.enter_s)
    start = 0 if heard.first_s is None else max(0, int((heard.first_s - LEAD_S) * SR))
    audio = np.concatenate([clip.audio[start:], np.zeros(int(AFTER_S * SR), np.float32)])
    events: list[listen.EarEvent] = []
    vad.reset()
    ear = listen.Ear(detector=_FiresFirst(), loopback=listen.Loopback(_Never, _Silent(),
                                                                     clock=_no_wall_clock),
                     vad=vad, on_event=events.append, clock=_no_wall_clock, judge=judge)
    offset = start / SR
    with applied(policy):
        for at in range(0, len(audio), CHUNK):
            block = audio[at:at + CHUNK]
            ear.push(block, now=(at + len(block)) / SR)
            captured = [e for e in events if e.kind == "captured"]
            if captured:
                done = captured[0].heard
                return Turn(clip.name, clip.two_part, heard.last_s, heard.longest_pause_s,
                            done.ended + offset, done.ended_by, tuple(done.asks))
    return Turn(clip.name, clip.two_part, heard.last_s, heard.longest_pause_s, None, "", ())


def percentile(values: list[float], q: float) -> float | None:
    return float(np.percentile(values, q)) if values else None


def rows(turns: list[Turn]) -> dict:
    """The bar's turn rows, and what they rest on."""
    labelled = [t for t in turns if t.last_s is not None]
    holds = [t.hold_s for t in labelled if t.hold_s is not None]
    return {
        "turns": len(labelled),
        "early": sum(t.early for t in labelled),
        "early_two_part": sum(t.early for t in labelled if t.two_part),
        "two_part": sum(t.two_part for t in labelled),
        "never_ended": sum(t.ended_s is None for t in labelled),
        "hold_p50_s": percentile(holds, 50),
        "hold_p90_s": percentile(holds, 90),
        "ended_by": {k: sum(t.ended_by == k for t in labelled)
                     for k in sorted({t.ended_by for t in labelled})},
        "unheard": len(turns) - len(labelled),
    }


def pauses(turns: list[Turn], timers=(0.6, 0.8, 1.0, 1.2, 1.6, 2.0)) -> dict[float, int]:
    """How many requests a fixed pause would have cut early: those with a
    pause inside them at least that long. What section 7's commit and
    longest pause are chosen from."""
    return {t: sum(turn.longest_pause_s >= t for turn in turns) for t in timers}
