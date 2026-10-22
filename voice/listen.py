"""The desk's ear: an open microphone that hears only "hey Steve".

The maintainer, 5 October 2026: "always-on" at the laptop, switched on and off at
will, and "obviously don't want everything I say going to the AI". So the
microphone's sound goes to one place until the wake phrase -- the wake
detector (`voice/wake.py`), on the CPU, which keeps no sound and writes
nothing -- and to a ring buffer of PRE_ROLL_S in memory, so the phrase
itself is there when Whisper checks it. Only after a wake is anything
gathered, and only up to the end of what the user says. What happens to it then
is the desk's engine's (`voice/engine.py`).

**What else it may hand on is a mode the engine sets** (`EarMode`), swapped
whole: a follow-up window after a reply, a note the user started, a live
conversation they asked for. Before 9 October 2026 the ear's state was
changed from two threads -- the microphone's and the event loop's -- by six
methods, and nothing outside it knew what it was doing; now the engine
decides, the ear obeys, and it reports what it heard as events
(`EarEvent`). Without a mode it hands on nothing but a wake and what
followed it. That is the privacy boundary, and tests/gate/test_desk.py
holds it here.

**The laptop's own sound is heard too.** Its speakers' output (a WASAPI
loopback capture) runs through a second wake detector and a voice detector
when asked. A wake heard there as well is the laptop's -- a Zoom call, a
video, the agent reading an email aloud -- and is ignored
(`core/desk.wake_counts`); and every capture carries how much speech the
laptop played during it, for the rules on follow-ups and approvals.

**Audio is never written down.** Not to disk, not to the database, not to
a log: the ring buffer and a capture in flight are the only copies, and a
capture is handed on as an array and dropped.

**What it did is logged, in numbers** (9 October 2026: two "hey Steve"s
left no trace at all): a near miss of the wake detector, the microphone
opening and closing and why, blocks dropped and the sound card's
discontinuities.

`Ear` is the logic, fed blocks of 16 kHz audio with a clock -- the tests
drive it with arrays. `Ears` is the hardware: Windows' default microphone
(followed when the user plugs another in) and the speakers' loopback, both
through WASAPI (soundcard), each on its own thread, the microphone closed
while the screen is locked.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import queue
import threading
import time
import warnings
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from core import desk
from voice import wake

log = logging.getLogger("listen")

SR = wake.SAMPLE_RATE
# When a capture is over: this much quiet after the user stops -- with no end-of-
# turn judge (voice/turns.py), or when it cannot say.
END_SILENCE_S = 0.8
# With one: at this much quiet it is asked whether the user sounds finished; a
# turn that does ends there, one that does not is given until the longest
# pause, asked once more on the way. 0.8 s cut "okay, perfect... can you
# Google..." in two (7 October 2026); people pause longer than that to think.
TURN_ASK_S = (0.4, 1.2)
TURN_FINISHED = 0.5
END_SILENCE_MAX_S = 2.0
# After "hey Steve" the user often waits for the tick before asking. On the 9
# October 2026 retest the user said "hey Steve", paused, then "what's the
# weather?": the judge ended the capture 0.48 s after the wake -- "hey
# Steve" alone sounds finished -- and the question, asked while Whisper was
# still on the phrase, was heard by nothing. So a wake's capture waits this
# long after the wake for their request to begin before quiet may end it...
WAKE_WAIT_S = 3.0
# ...and the user has begun it once the voice detector has heard this much speech
# after the wake: less is the tail of the phrase, a breath or a click.
REQUEST_SPEECH_S = 0.2
# A follow-up's pre-roll: the start of the first word, caught before the
# voice detector is sure.
FOLLOW_UP_PRE_ROLL_S = 0.3
VAD_THRESHOLD = 0.5
# A note's stretch of speech, or one of theirs in a live conversation: ended by
# a shorter pause than a request, and cut at SEGMENT_MAX_S so a lecturer who
# never pauses is still written down.
SEGMENT_SILENCE_S = 0.6
SEGMENT_MAX_S = 15.0
# A microphone's first moments are not heard: this laptop's built-in array
# -- which the user says does not work -- returned a second of loud nonsense on
# opening (RMS up to 0.99), measured 5 October 2026, and a device just
# plugged in may do the same.
SETTLE_S = 1.5
# The loopback is read 200 ms at a time: soundcard's read loop took 9.6% of
# a core at 50 ms reads and 4.9% at 200, measured. It is only asked about
# afterwards, so the delay costs nothing but `Loopback.wait_s`.
LOOPBACK_READ_S = 0.2
# A wake detector window that scored this much but not enough is logged as
# a near miss -- once, if no wake follows within NEAR_WAKE_S, and not in the
# NEAR_WAKE_S after one fired: the windows after a wake score high as the
# phrase slides out of them, and every "near wake" of the 9 October 2026
# retest (0.71, 0.87, 0.79) was that -- logged two seconds after a wake that
# counted -- not a "hey Steve" it missed. 0.4, not 0.5, so the next sound
# test can see how far below the bar a missed one falls.
NEAR_WAKE = 0.4
NEAR_WAKE_S = 2.0
# The bar while the desk's own voice is audible. The echo canceller ducks
# their voice under its own (measured on WebRTC's AEC3 with speech-like
# signals, 9 October 2026: 4-5 dB with its echo no louder than them, 9-12 dB
# with it 6-12 dB louder, more with the distortion of laptop speakers), and
# "hey Steve, stop" over an answer went unheard three times that evening.
# The desk never says the phrase (core/desk.never_spoken), the laptop's own
# copy is checked at the same bar, and Whisper must still hear it: so a
# lower bar costs at most an answer cut short by a false wake. From the
# 5 October 2026 head's calibration: 0.7 holds 86.7% of
# held-out voices against 82.9% at the usual 0.9, at 4.8 false wakes an
# hour of ordinary audio against 1.7; 0.6 adds one point of recall for a
# quarter more false wakes.
#
# Since 11 October 2026 a head is trained on the desk's own voice as heard
# through the canceller, and carries the bar it was measured to hold over
# it (`wake.Head.over_voice`, `over_voice_bar`); this is the bar for a head
# trained before then.
WAKE_OVER_VOICE = 0.7
# Over the desk's voice, the raw microphone's "hey Steve" counts too. On 10
# October 2026 at 00:20 the canceller left 0.00 of their "hey Steve, stop"
# over GPT-Live's voice while the raw microphone scored it 0.91, and the
# call talked on over them; the rehearsal rig's louder room shows the other
# case, the raw microphone drowned in echo and the cleaned one hearing them.
# Neither path alone is enough. This was first for live conversations only,
# on one local answer where the cleaned path had heard them (0.99); that
# night's rehearsal had a long local answer at normal volume score their stop
# 0.62 cleaned and 1.00 raw, and talk on over them for 31 seconds. A wrong
# wake cuts an answer short; a missed one talks on over "stop". The laptop's
# own copy is still checked, at the same bar, so speech it played -- a
# GPT-Live that says the phrase, a video -- wakes nothing.
RAW_WAKES = True
# Speech over the desk's own voice, without the wake phrase, that lasts this
# long starts a capture (decision D2, off: core/desk.BARGE_IN_WITHOUT_WAKE).
BARGE_SPEECH_S = 0.6


def over_voice_bar(detector) -> float:
    """The wake's bar while the desk's own voice is audible: what the head
    was measured to hold over that voice (`wake.Head.over_voice`, set by
    `wake_train.choose`), or WAKE_OVER_VOICE for a head trained before
    that was measured."""
    measured = getattr(getattr(detector, "head", None), "over_voice", None)
    return float(measured) if measured else WAKE_OVER_VOICE


class Vad:
    """Silero's voice detector (the model faster-whisper ships), streamed in
    32 ms steps with its state carried. Speech or not, per step."""

    STEP = 512
    _CONTEXT = 64

    def __init__(self) -> None:
        import os

        import faster_whisper
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        path = Path(os.path.dirname(faster_whisper.__file__)) / "assets" / "silero_vad_v6.onnx"
        self._session = ort.InferenceSession(str(path), sess_options=options,
                                             providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), np.float32)
        self._c = np.zeros((1, 1, 128), np.float32)
        self._context = np.zeros(self._CONTEXT, np.float32)
        self._pending = np.zeros(0, np.float32)

    def push(self, audio: np.ndarray) -> list[bool]:
        """Feed audio; returns speech-or-not for each complete 32 ms step."""
        self._pending = np.concatenate([self._pending, np.asarray(audio, np.float32)])
        out = []
        while len(self._pending) >= self.STEP:
            step, self._pending = self._pending[:self.STEP], self._pending[self.STEP:]
            x = np.concatenate([self._context, step])[None]
            prob, self._h, self._c = self._session.run(
                None, {"input": x, "h": self._h, "c": self._c})
            self._context = step[-self._CONTEXT:]
            out.append(float(np.squeeze(prob)) >= VAD_THRESHOLD)
        return out


@dataclass
class Heard:
    """One capture, handed on: their request (after a wake), a follow-up, a
    note's stretch, or one of their stretches in a live conversation.

    `audio` is the only copy and lives in memory. `laptop_speech_s` is how
    much speech the laptop itself played while it was being said."""

    audio: np.ndarray
    woke: bool
    laptop_speech_s: float
    started: float
    ended: float
    # A stretch of a dictation or a lecture the user started: transcribed into
    # their note, never a request (agent/worker/desk.py).
    segment: bool = False
    # Where it was heard: the microphone, or the laptop's own output (`Tap`).
    source: str = "mic"
    # How the capture ended, for the turn's log line: "judge" (the end-of-
    # turn judge said the user had finished), "cap" (END_SILENCE_MAX_S of quiet),
    # "timer" (END_SILENCE_S, with no judge or none that could say) or "max"
    # (MAX_CAPTURE_S); and what the judge was asked -- (seconds of quiet, its
    # chance the user had finished). Numbers, never words.
    ended_by: str = ""
    asks: tuple = ()
    # The wake came while the desk was busy answering -- over its voice, or
    # over its thinking: the user cut in. Its pre-roll holds the desk's voice and
    # its echo. On 9 October 2026 "hey Steve, stop" over an 82-second answer
    # reached the model as "I'll talk." and opened a billed live conversation.
    barge_in: bool = False
    # Seconds of speech the voice detector heard *after* the wake. A capture
    # whose words were all in the pre-roll -- with the desk's voice mixed in --
    # has none; a request the user said after the phrase has some.
    speech_s: float = 0.0
    # What it is: "wake", "follow", "barge" (speech over the desk's voice,
    # D2), "segment" (a note's) or "utterance" (theirs, in a live conversation).
    kind: str = "wake"

    @property
    def bare(self) -> bool:
        """A wake with nothing said after it, even after WAKE_WAIT_S: the
        phrase alone, or a request the user has not begun yet."""
        return self.kind == "wake" and self.speech_s < REQUEST_SPEECH_S


@dataclass
class EarEvent:
    """Something the ear heard, for the engine (voice/engine.py). From the
    ear's thread; the engine takes it on the event loop."""

    kind: str  # wake, laptop_wake, near_wake, captured, segment, utterance, barge_speech
    at: float
    heard: Heard | None = None
    score: float | None = None
    barge: bool = False


@dataclass(frozen=True)
class EarMode:
    """What the ear may hand on besides a wake and what followed it. Set
    whole by the engine; the ear's thread reads it, never changes it.

    busy          -- the desk is answering: a wake now is a cut-in
    follow_until  -- until then, speech starts a capture without the wake
                     phrase (a follow-up, core/desk.FOLLOW_UP_S)
    segments      -- a note the user started: every stretch of speech, for the note
    stream        -- a live conversation they asked for: every block, to it
    utterances    -- with `stream`: their stretches too, for local Whisper
    barge_speech  -- sustained speech over the desk's voice starts a capture
                     (D2, off unless core/desk.BARGE_IN_WITHOUT_WAKE)
    audible       -- whether the desk's own voice is audible now
                     (voice/floor.Floor.audible): the wake's bar is then
                     `over_voice_bar`
    """

    busy: bool = False
    follow_until: float = 0.0
    segments: bool = False
    stream: Callable[[np.ndarray], None] | None = None
    utterances: bool = False
    barge_speech: bool = False
    audible: Callable[[], bool] | None = None


QUIET = EarMode()


class Loopback:
    """What the laptop is playing: kept for KEEP_S, and listened to only
    when asked -- for the wake phrase when the microphone heard one, for
    speech when a capture ends.

    Listened to all the time, it cost half the ear's CPU (a second wake
    detector and a voice detector, every 80 ms, measured 5 October 2026)
    for answers needed a few times an hour. Fed from its own thread; asked
    from the microphone's."""

    KEEP_S = 30.0
    # The detector needs this much before a moment to judge it: 1.28 s of
    # window and the front end's warm-up.
    LEAD_S = 2.4
    # How long a question waits for audio still in the loopback's current
    # read. Its reads are 200 ms (half the CPU of 50 ms ones), so the
    # laptop's own copy of the phrase can land just after the mic's wake.
    # `Ears` sets it; zero means do not wait.
    wait_s = 0.0

    def __init__(self, detector: Callable[[], object], vad,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.detector = detector  # makes a fresh one per question
        self.vad = vad
        self.clock = clock
        self._lock = threading.Lock()
        self._blocks: deque[tuple[float, np.ndarray]] = deque()  # (end time, audio)
        self._n = 0
        self._vad_lock = threading.Lock()  # asked from the ear's thread and the loop's

    def push(self, audio: np.ndarray, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            self._blocks.append((now, np.asarray(audio, np.float32)))
            self._n += len(audio)
            while self._blocks and self._n - len(self._blocks[0][1]) >= self.KEEP_S * SR:
                self._n -= len(self._blocks.popleft()[1])

    def _latest(self) -> float:
        with self._lock:
            return self._blocks[-1][0] if self._blocks else float("-inf")

    def _between(self, start: float, end: float) -> list[tuple[float, np.ndarray]]:
        with self._lock:
            return [(t, b) for t, b in self._blocks if start <= t <= end]

    def wakes(self, near: float | None = None, threshold: float | None = None) -> list[float]:
        """When the laptop itself played the wake phrase, in the ECHO_S
        before `near` (by default, now): the moments its detector fired --
        at `threshold`, the bar the microphone's wake was heard at, so a
        lower bar over the desk's own voice is a lower bar here too."""
        near = self.clock() if near is None else near
        deadline = time.monotonic() + self.wait_s
        while self.wait_s and self._latest() < near and time.monotonic() < deadline:
            time.sleep(0.02)
        detector, fired = self.detector(), []
        if threshold is not None and hasattr(detector, "threshold"):
            detector.threshold = threshold
        for t, block in self._between(near - desk.ECHO_S - self.LEAD_S,
                                      near + self.wait_s + 0.1):
            if detector.push(block) is not None:
                fired.append(t)
        return fired

    def speech_between(self, start: float, end: float) -> float:
        """Seconds of speech the laptop played between two clock times."""
        blocks = self._between(start, end + 0.1)
        with self._vad_lock:
            self.vad.reset()
            steps = sum(sum(self.vad.push(b)) for _, b in blocks)
        return steps * Vad.STEP / SR


@dataclass
class Counts:
    """What the ear has done, for /voice/status -- numbers, never words."""

    wakes: int = 0
    laptop_wakes_ignored: int = 0
    near_wakes: int = 0
    captures: int = 0
    follow_ups: int = 0
    barge_ins: int = 0
    turns_judged: int = 0
    last: dict = field(default_factory=dict)


class Ear:
    """The microphone's logic, driven by blocks of 16 kHz audio: the wake
    detector and the ring buffer always, and whatever the engine's mode
    allows (`EarMode`). It decides nothing about requests; it reports.

    Its one state of its own is what it is gathering -- a capture, a
    segment, an utterance -- and whether it has handed a capture on that
    the engine has not yet answered with a new mode: until then it is busy,
    so a wake is a cut-in and no follow-up starts."""

    def __init__(self, *, detector, loopback: Loopback, vad,
                 on_event: Callable[[EarEvent], None],
                 clock: Callable[[], float] = time.monotonic,
                 judge: Callable[[np.ndarray], float | None] | None = None,
                 raw_detector=None) -> None:
        self.detector = detector
        self._bar = getattr(detector, "threshold", None)  # its own, in quiet
        self._over_bar = over_voice_bar(detector)  # while the desk's voice is audible
        # A second detector on the microphone before the echo canceller,
        # run only while the desk's voice is audible, that wakes nothing: it
        # logs what "hey Steve" scored there against what it scored cleaned
        # (`_raw_score`), so the next sound test shows whether the canceller
        # is what hides them.
        self.raw_detector = raw_detector
        if raw_detector is not None and hasattr(raw_detector, "threshold"):
            raw_detector.threshold = 2.0  # it never fires
        self._raw_on = False
        self._raw_best: tuple[float, float, float] | None = None  # (raw, cleaned, when)
        self._clean_best = 0.0
        self._near_quiet_until = float("-inf")
        # Has the user finished? (voice/turns.TurnJudge) None: the timer alone.
        self.judge = judge
        self.loopback = loopback
        self.vad = vad
        self.on_event = on_event
        self.clock = clock
        self.counts = Counts()
        self.mode = QUIET
        self._lock = threading.RLock()
        self._ring: deque[np.ndarray] = deque()
        self._ring_n = 0
        self._gathering: str | None = None  # "capture", "segment", "utterance"
        self._handed = False
        self._capture: list[np.ndarray] = []
        self._capture_n = 0
        self._woke = False
        self._barge = False
        self._kind = "wake"
        self._speech_s = 0.0
        self._started = 0.0
        self._wake_at = 0.0  # when the capture's wake fired
        self._quiet_s = 0.0
        self._asked = 0  # of TURN_ASK_S, in this pause
        self._asks: list[tuple[float, float]] = []
        self._ended_by = ""
        self._barge_run = 0.0
        self._near: tuple[float, float, float, bool] | None = None  # (best, when, bar, over)
        # When the microphone last held speech, by the voice detector, in
        # any mode that runs it: what tells a live conversation the user is still
        # in it, and an approval it hands over that they said it.
        self.speech_at = 0.0

    # -- the engine's side -----------------------------------------------

    def set_mode(self, mode: EarMode) -> None:
        """What the ear may hand on from now. A capture under way finishes."""
        with self._lock:
            self.mode = mode
            self._handed = False
            if self._gathering in ("segment", "utterance") and not (
                    mode.segments or mode.utterances):
                self._capture, self._capture_n, self._gathering = [], 0, None
            if self._gathering is None:
                self.vad.reset()
                self._barge_run = 0.0

    @property
    def gathering(self) -> str | None:
        return self._gathering

    def forget(self) -> None:
        """Drop everything held: the ring, a capture in flight."""
        with self._lock:
            self._ring.clear()
            self._ring_n = 0
            self._capture, self._capture_n, self._gathering = [], 0, None
            self._handed = False
            self.mode = QUIET
            self.detector.reset()
            self.vad.reset()

    # -- the microphone's side -------------------------------------------

    def push(self, audio: np.ndarray, now: float | None = None,
             raw: np.ndarray | None = None) -> None:
        """A block of the microphone, cleaned of the desk's own voice; `raw`
        is the same before the canceller, if there is one."""
        now = self.clock() if now is None else now
        audio = np.asarray(audio, np.float32)
        events: list[EarEvent] = []
        with self._lock:
            self._remember(audio)
            over = self._over_voice()
            bar = self._set_bar(over)
            fired = self.detector.push(audio)
            self._near_miss(fired, now, events, over)
            mode = self.mode
            raw_wake = self._raw_score(raw, over, now, bar=bar)
            if fired is None and raw_wake is not None:
                log.info("ear: a wake heard on the raw microphone over its voice "
                         "(raw %.2f, cleaned %.2f)", raw_wake,
                         float(getattr(self.detector, "last_score", 0.0) or 0.0))
                fired = raw_wake
            if mode.stream is not None:
                # A live conversation the user asked for hears everything; the
                # phrase still reaches the engine, which ends it.
                mode.stream(audio)
            steps = self.vad.push(audio) if self._needs_vad(mode, now) else []
            if any(steps):
                self.speech_at = now
            if fired is not None and self._gathering != "capture":
                if desk.wake_counts(now, self.loopback.wakes(near=now, threshold=bar)):
                    if self._gathering is not None:
                        self._hand(now, events)  # the note's stretch so far
                    self._wake(now, mode, events)
                else:
                    self.counts.laptop_wakes_ignored += 1
                    log.info("a wake the laptop itself played: ignored")
                    events.append(EarEvent("laptop_wake", now))
            elif self._gathering is not None:
                self._gather(audio, steps, now, events)
            elif not self._handed and any(steps):
                self._speech(audio, mode, now, steps, events)
            elif not any(steps):
                self._barge_run = 0.0
        for event in events:
            self.on_event(event)

    def _needs_vad(self, mode: EarMode, now: float) -> bool:
        return bool(self._gathering or mode.stream is not None or mode.segments
                    or mode.barge_speech or (mode.follow_until > now and not self._handed))

    def _over_voice(self) -> bool:
        """Whether the desk's own voice is audible now, by the engine's word."""
        audible = self.mode.audible
        if audible is None:
            return False
        try:
            return bool(audible())
        except Exception:  # noqa: BLE001 - not knowing is the usual bar
            return False

    def _set_bar(self, over: bool) -> float | None:
        """The wake detector's bar for this block: its own, or the bar over
        the desk's voice while that is audible (`over_voice_bar`). None if
        it has no bar to set."""
        if self._bar is None:
            return None
        bar = min(self._bar, self._over_bar) if over else self._bar
        self.detector.threshold = bar
        return bar

    def _near_miss(self, fired, now: float, events: list[EarEvent], over: bool = False) -> None:
        if fired is not None:
            self._near = None
            self._near_quiet_until = now + NEAR_WAKE_S  # its own tail is not a miss
            return
        score = getattr(self.detector, "last_score", None)
        threshold = getattr(self.detector, "threshold", 1.0)
        if (score is not None and now >= self._near_quiet_until
                and NEAR_WAKE <= score < threshold
                and (self._near is None or score > self._near[0])):
            self._near = (float(score), now, float(threshold), over)
        if self._near is not None and now - self._near[1] >= NEAR_WAKE_S:
            best, _, bar, was_over = self._near
            self._near = None
            self.counts.near_wakes += 1
            log.info("ear: near wake score=%.2f (under %.2f) over=%s", best, bar,
                     "voice" if was_over else "quiet")
            events.append(EarEvent("near_wake", now, score=best))

    def _raw_score(self, raw: np.ndarray | None, over: bool, now: float, *,
                   bar: float | None = None) -> float | None:
        """While the desk's voice is audible, what the wake detector makes of
        the microphone before the canceller, beside what it made of it
        after: logged when either came near. A score at the bar is
        returned, as a wake (RAW_WAKES); None otherwise."""
        if self.raw_detector is None or raw is None:
            return None
        if not over:
            if self._raw_on:
                self._raw_on = False
                self.raw_detector.reset()
                self._report_raw(force=True)
            return None
        self._raw_on = True
        self.raw_detector.push(np.asarray(raw, np.float32))
        score = float(getattr(self.raw_detector, "last_score", 0.0) or 0.0)
        clean = float(getattr(self.detector, "last_score", 0.0) or 0.0)
        self._clean_best = max(self._clean_best, clean)
        if score >= NEAR_WAKE and (self._raw_best is None or score > self._raw_best[0]):
            self._raw_best = (score, self._clean_best, now)
        if self._raw_best is not None and now - self._raw_best[2] >= NEAR_WAKE_S:
            self._report_raw()
        # One wake per phrase without a refractory time of its own: the wake
        # starts a capture, and a capture takes no second wake.
        if RAW_WAKES and bar is not None and score >= bar:
            return score
        return None

    def _report_raw(self, force: bool = False) -> None:
        best = self._raw_best
        if best is not None:
            log.info("ear: over its voice the raw microphone scored %.2f, the cleaned %.2f "
                     "(bar %.2f)", best[0], max(best[1], self._clean_best),
                     min(self._bar or 1.0, self._over_bar))
        if best is not None or force:
            self._raw_best, self._clean_best = None, 0.0

    def _speech(self, audio, mode: EarMode, now: float, steps, events) -> None:
        """Speech, with nothing being gathered: what the mode makes of it."""
        keep = int(FOLLOW_UP_PRE_ROLL_S * SR)
        if mode.stream is not None and mode.utterances:
            self._begin(self._recent(keep), now=now - keep / SR, kind="utterance")
        elif mode.segments:
            self._begin(self._recent(keep), now=now - keep / SR, kind="segment")
        elif mode.follow_until > now:
            self.counts.follow_ups += 1
            self._begin(self._recent(keep), now=now - keep / SR, kind="follow")
        elif mode.barge_speech:
            self._barge_run += sum(steps) * Vad.STEP / SR
            if self._barge_run >= BARGE_SPEECH_S:
                run = int((self._barge_run + FOLLOW_UP_PRE_ROLL_S) * SR)
                self.counts.barge_ins += 1
                self._begin(self._recent(run), now=now - run / SR, kind="barge", barge=True)
                events.append(EarEvent("barge_speech", now, barge=True))
                self._barge_run = 0.0

    def _remember(self, audio: np.ndarray) -> None:
        self._ring.append(audio)
        self._ring_n += len(audio)
        limit = int(desk.PRE_ROLL_S * SR)
        while self._ring and self._ring_n - len(self._ring[0]) >= limit:
            self._ring_n -= len(self._ring.popleft())

    def _recent(self, n: int) -> np.ndarray:
        held = np.concatenate(list(self._ring)) if self._ring else np.zeros(0, np.float32)
        return held[-n:]

    def _wake(self, now: float, mode: EarMode, events: list[EarEvent]) -> None:
        self.counts.wakes += 1
        barge = mode.busy or self._handed
        if barge:
            self.counts.barge_ins += 1
        events.append(EarEvent("wake", now, barge=barge))
        self._begin(self._recent(int(desk.PRE_ROLL_S * SR)), now=now - desk.PRE_ROLL_S,
                    kind="wake", woke=True, barge=barge)
        self._wake_at = now

    def _begin(self, pre_roll: np.ndarray, *, now: float, kind: str, woke: bool = False,
               barge: bool = False) -> None:
        self._gathering = "capture" if kind in ("wake", "follow", "barge") else kind
        self._kind = kind
        self._capture = [pre_roll]
        self._capture_n = len(pre_roll)
        self._woke = woke
        self._barge = barge
        self._speech_s = 0.0
        self._started = now
        self._wake_at = now
        self._quiet_s = 0.0
        self._asked = 0
        self._asks, self._ended_by = [], ""
        self.vad.reset()

    def _gather(self, audio: np.ndarray, steps: list[bool], now: float,
                events: list[EarEvent]) -> None:
        self._capture.append(audio)
        self._capture_n += len(audio)
        for speech in steps:
            if speech:
                self._quiet_s, self._asked = 0.0, 0
                self._speech_s += Vad.STEP / SR
            else:
                self._quiet_s += Vad.STEP / SR
        if self._gathering != "capture":
            if self._quiet_s >= SEGMENT_SILENCE_S or self._capture_n >= SEGMENT_MAX_S * SR:
                self._hand(now, events)
        elif self._capture_n >= desk.MAX_CAPTURE_S * SR:
            self._ended_by = "max"
            self._hand(now, events)
        elif self._finished(now):
            self._hand(now, events)

    def _finished(self, now: float) -> bool:
        """Whether the pause the user is in ends the request: by the judge when
        there is one and it can say, by the timer otherwise. How it ended is
        left in `_ended_by`, and what the judge was asked in `_asks`.

        A wake the user has said nothing after is not judged at all: they may be
        waiting for the tick. It ends WAKE_WAIT_S after the wake, unasked."""
        if self._kind == "wake" and self._speech_s < REQUEST_SPEECH_S:
            if now - self._wake_at < WAKE_WAIT_S:
                return False
            self._ended_by = "no request"
            return True
        if self.judge is None:
            return self._ends_by_timer()
        if self._quiet_s >= END_SILENCE_MAX_S:
            self._ended_by = "cap"
            return True
        if self._asked < len(TURN_ASK_S) and self._quiet_s >= TURN_ASK_S[self._asked]:
            self._asked += 1
            chance = self.judge(np.concatenate(self._capture))
            if chance is None:  # it cannot say: the timer decides from here
                self.judge = None
                return self._ends_by_timer()
            self.counts.turns_judged += 1
            self._asks.append((round(self._quiet_s, 2), round(float(chance), 2)))
            if chance >= TURN_FINISHED:
                self._ended_by = "judge"
                return True
        return False

    def _ends_by_timer(self) -> bool:
        if self._quiet_s >= END_SILENCE_S:
            self._ended_by = "timer"
            return True
        return False

    def _hand(self, now: float, events: list[EarEvent]) -> None:
        kind, gathering = self._kind, self._gathering
        request = gathering == "capture"
        heard = Heard(audio=np.concatenate(self._capture) if self._capture
                      else np.zeros(0, np.float32),
                      woke=self._woke and request,
                      laptop_speech_s=self.loopback.speech_between(self._started, now),
                      started=self._started, ended=now, segment=kind == "segment",
                      ended_by=self._ended_by if request else "",
                      asks=tuple(self._asks) if request else (),
                      barge_in=self._barge and request, speech_s=self._speech_s, kind=kind)
        self._capture, self._capture_n, self._gathering = [], 0, None
        if request:
            self.counts.captures += 1
            self._handed = True  # busy until the engine sets a new mode
            events.append(EarEvent("captured", now, heard=heard, barge=heard.barge_in))
        else:
            events.append(EarEvent(kind, now, heard=heard))


# ---------------------------------------------------------------------------
# The hardware
# ---------------------------------------------------------------------------


def screen_locked() -> bool:
    """Whether Windows is showing the lock screen (or no one is signed in):
    the input desktop cannot be opened from their session then."""
    try:
        user32 = ctypes.windll.user32
    except AttributeError:  # not Windows: nothing to lock
        return False
    desktop = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desktop:
        return True
    try:
        return not user32.SwitchDesktop(desktop)
    finally:
        user32.CloseDesktop(desktop)


# The sound card's "data discontinuity" warnings, counted rather than printed:
# each is audio the microphone or the loopback lost, and a "hey Steve" lost
# that way left no trace (9 October 2026: 25 of them, printed, unread).
_discontinuities = {"n": 0}
_hooked = {"done": False}


def _count_discontinuities() -> None:
    if _hooked["done"]:
        return
    _hooked["done"] = True
    shown = warnings.showwarning

    def showwarning(message, category, filename, lineno, file=None, line=None):
        if category.__name__ == "SoundcardRuntimeWarning":
            _discontinuities["n"] += 1
            return
        shown(message, category, filename, lineno, file, line)

    warnings.showwarning = showwarning


class Ears:
    """The microphone and the loopback, opened for an `Ear` and closed when
    it should not hear: switched off, or the screen locked."""

    # The microphone is read 100 ms at a time: the wake detector judges 80 ms
    # steps, and shorter reads cost more CPU for nothing.
    MIC_READ_S = 0.1
    # How often it looks whether Windows' default microphone has changed.
    FOLLOW_S = 2.0
    # How often the ear's losses are logged, when there were any.
    REPORT_S = 60.0

    def __init__(self, listener: Ear, *, signed_in: Callable[[], bool],
                 locked: Callable[[], bool] = screen_locked, canceller=None,
                 on_state: Callable[[bool, str], None] | None = None) -> None:
        self.listener = listener
        # Whether the person who set Steve up is signed in to the app
        # (agent/worker/signin.py): asked at every check, as the lock is.
        self.signed_in = signed_in
        self.locked = locked
        # The desk's own voice taken out of the microphone (voice/echo.py).
        self.canceller = canceller
        # Told when the microphone opens or closes, and why (the engine).
        self.on_state = on_state
        self._queue: queue.Queue = queue.Queue(maxsize=400)
        self._running = False
        self._threads: list[threading.Thread] = []
        self._want_mic = False
        self.open = False
        self.device: str | None = None  # the microphone in use, by name
        self.paused_for: str | None = None
        self.error: str | None = None
        self.loopback_ok = False
        self.dropped = 0
        self._reported = (0, 0)

    def start(self) -> None:
        if self._running:
            return
        _count_discontinuities()
        self._running = True
        for target in (self._process, self._watch_lock, self._loopback, self._mic):
            thread = threading.Thread(target=target, name=f"ears-{target.__name__}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def halt(self) -> None:
        """Stop hearing at once: no block is handed on after this returns.
        The threads wind down on their own (`stop` waits for them)."""
        self._running = False
        self._close_mic()

    def stop(self) -> None:
        """Stop, and wait for the threads. Blocks for up to seconds: never on
        the event loop (`docs/protocols.md` section 2) -- on 9 October 2026
        switching off from the app joined four threads there."""
        self.halt()
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads.clear()
        self.listener.forget()

    # -- the microphone --------------------------------------------------

    def _open_mic(self) -> None:
        self._want_mic = True
        self.open = True

    def _close_mic(self) -> None:
        self._want_mic = False
        self.open = False
        while not self._queue.empty():
            self._queue.get_nowait()

    def _mic(self) -> None:
        """Windows' default microphone, read through WASAPI, followed when it
        changes. The maintainer, 5 October 2026: the laptop's own microphone does not
        work, and the user plugs one in to talk to it. PortAudio (sounddevice, as
        push-to-talk uses) sees only the devices there when it started, so a
        microphone plugged in later would never be heard; WASAPI sees them as
        they come and go. It costs more CPU (6.8% of a core against 2-3%,
        measured) and is the difference between hearing them and not."""
        try:
            import soundcard
        except ImportError as exc:
            self.error = f"no microphone: {exc}"
            return
        _com_init()
        frames = int(SR * self.MIC_READ_S)
        while self._running:
            if not self._want_mic:
                self.device = None
                time.sleep(0.1)
                continue
            try:
                mic = soundcard.default_microphone()
                self.device = mic.name
                settled = time.monotonic() + SETTLE_S
                checked = time.monotonic()
                with mic.recorder(samplerate=SR, channels=1, blocksize=frames) as recorder:
                    while self._running and self._want_mic:
                        data = recorder.record(numframes=frames)
                        now = time.monotonic()
                        if now >= settled:
                            try:
                                self._queue.put_nowait(
                                    np.ascontiguousarray(data[:, 0], dtype=np.float32))
                            except queue.Full:
                                self.dropped += 1
                        if now - checked >= self.FOLLOW_S:  # plugged in, unplugged, chosen
                            checked = now
                            if soundcard.default_microphone().name != mic.name:
                                log.info("ear: the default microphone changed: following it")
                                self.listener.forget()
                                break
            except Exception as exc:  # noqa: BLE001 - a microphone gone: find the next
                error = f"microphone: {type(exc).__name__}: {exc}"[:200]
                if error != self.error:
                    log.warning("ear: %s", error)
                self.error = error
                self.device = None
                time.sleep(1.0)

    def check(self) -> None:
        """Open while the user is at the laptop; closed, and the ring forgotten,
        while the screen is locked or no one is signed in. Run every two
        seconds."""
        locked = self.locked()
        signed_in = self.signed_in()
        # Not without the loopback: it is how the laptop's own sound is told
        # from theirs (core/desk.wake_counts). Deaf is better than fooled.
        allowed = desk.may_listen(switched_on=self._running, locked=locked,
                                  signed_in=signed_in) and self.loopback_ok
        try:
            if allowed and not self.open:
                self._open_mic()
                self.paused_for = None
                log.info("ear: mic open")
                if self.on_state is not None:
                    self.on_state(True, "")
            elif not allowed:
                reason = ("locked" if locked else "signed out" if not signed_in
                          else "no loopback")
                if self.open:
                    self._close_mic()
                    self.listener.forget()
                    log.info("ear: mic closed reason=%s", reason)
                    if self.on_state is not None:
                        self.on_state(False, reason)
                self.paused_for = reason
        except Exception as exc:  # noqa: BLE001 - no mic is a state, not a crash
            self.error = f"{type(exc).__name__}: {exc}"[:200]
            self._close_mic()

    def report(self) -> None:
        """What the ear lost since it last said, if anything: numbers."""
        now = (self.dropped, _discontinuities["n"])
        if now != self._reported:
            log.info("ear: dropped blocks=%d discontinuities=%d",
                     now[0] - self._reported[0], now[1] - self._reported[1])
            self._reported = now

    def _watch_lock(self) -> None:
        reported = time.monotonic()
        while self._running:
            self.check()
            if time.monotonic() - reported >= self.REPORT_S:
                reported = time.monotonic()
                self.report()
            for _ in range(20):
                if not self._running:
                    return
                time.sleep(0.1)

    def _process(self) -> None:
        while self._running:
            try:
                block = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._running:
                return
            try:
                raw = None
                if self.canceller is not None:
                    raw, block = block, self.canceller.near(block)
                    if not len(block):
                        continue
                self.listener.push(block, raw=raw)
            except Exception:  # noqa: BLE001 - one bad block must not deafen it
                log.exception("the listener failed on a block")

    # -- the loopback ----------------------------------------------------

    def _loopback(self) -> None:
        """The speakers' output, for the loopback detector. While it is not
        running the microphone stays closed (`check`)."""
        try:
            import soundcard
            import soxr
        except ImportError as exc:
            self.error = f"no loopback: {exc}"
            return
        _com_init()
        rate = 48_000
        frames = int(rate * LOOPBACK_READ_S)
        self.listener.loopback.wait_s = LOOPBACK_READ_S + 0.15
        while self._running:
            try:
                speaker = soundcard.default_speaker()
                mic = soundcard.get_microphone(id=str(speaker.name), include_loopback=True)
                resampler = soxr.ResampleStream(rate, SR, 1, dtype="float32")
                with mic.recorder(samplerate=rate, channels=1, blocksize=frames) as recorder:
                    self.loopback_ok = True
                    while self._running:
                        data = recorder.record(numframes=frames)
                        if not self.open:
                            continue
                        self.listener.loopback.push(resampler.resample_chunk(
                            np.ascontiguousarray(data[:, 0], dtype=np.float32)))
            except Exception as exc:  # noqa: BLE001 - a device change: open it again
                self.loopback_ok = False
                error = f"loopback: {type(exc).__name__}: {exc}"[:200]
                if error != self.error:
                    log.warning("ear: %s", error)
                self.error = error
                time.sleep(2)
        self.loopback_ok = False


# ---------------------------------------------------------------------------
# Notes of what the laptop plays
# ---------------------------------------------------------------------------


class Segmenter:
    """A stream cut into stretches of speech, for a transcript: from the
    first speech after quiet (with a little before it) to
    SEGMENT_SILENCE_S of quiet, or SEGMENT_MAX_S, whichever comes first."""

    PRE_ROLL_S = 0.3

    def __init__(self, vad, *, source: str) -> None:
        self.vad = vad
        self.source = source
        self._before: deque[np.ndarray] = deque()
        self._before_n = 0
        self._gathered: list[np.ndarray] = []
        self._n = 0
        self._quiet_s = 0.0
        self._started = 0.0

    def push(self, audio: np.ndarray, now: float) -> list[Heard]:
        audio = np.asarray(audio, np.float32)
        steps = self.vad.push(audio)
        out: list[Heard] = []
        if not self._gathered:
            if any(steps):
                self._gathered = [*self._before, audio]
                self._n = self._before_n + len(audio)
                self._started = now - self._n / SR
                self._quiet_s = 0.0
                self._before.clear()
                self._before_n = 0
            else:
                self._before.append(audio)
                self._before_n += len(audio)
                while self._before and self._before_n - len(self._before[0]) >= \
                        self.PRE_ROLL_S * SR:
                    self._before_n -= len(self._before.popleft())
            return out
        self._gathered.append(audio)
        self._n += len(audio)
        for speech in steps:
            self._quiet_s = 0.0 if speech else self._quiet_s + Vad.STEP / SR
        if self._quiet_s >= SEGMENT_SILENCE_S or self._n >= SEGMENT_MAX_S * SR:
            out.append(Heard(audio=np.concatenate(self._gathered), woke=False,
                             laptop_speech_s=0.0, started=self._started, ended=now,
                             segment=True, source=self.source, kind="segment"))
            self._gathered, self._n = [], 0
        return out


class Tap:
    """What the laptop plays -- a call, a video -- cut into stretches of
    speech for a note the user started, and nothing else.

    The maintainer, 5 October 2026: "can we make it be able to note-take my zoom
    calls/videos I'm watching ... we'd want to capture it on the way out
    rather than through the speakers". So it reads the output itself
    (WASAPI loopback): the call as the laptop received it, clean, at any
    volume, muted or not -- and with no microphone, which the user may not have.

    It runs only while such a note is open (agent/worker/desk.py), hears no
    wake phrase -- nothing played on the laptop is ever an instruction --
    and keeps no sound: a stretch is handed on, transcribed, and dropped."""

    def __init__(self, on_segment: Callable[[Heard], None], vad=None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.on_segment = on_segment
        self.clock = clock
        self.segmenter = Segmenter(vad if vad is not None else Vad(), source="laptop")
        self.last_speech = clock()
        self.ok = False
        self.error: str | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def push(self, audio: np.ndarray, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        for heard in self.segmenter.push(audio, now):
            self.last_speech = now
            self.on_segment(heard)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.last_speech = self.clock()
        self._thread = threading.Thread(target=self._run, name="tap", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self.ok = False

    def _run(self) -> None:
        try:
            import soundcard
            import soxr
        except ImportError as exc:
            self.error = f"no loopback: {exc}"
            return
        _com_init()
        rate = 48_000
        frames = int(rate * LOOPBACK_READ_S)
        while self._running:
            try:
                speaker = soundcard.default_speaker()
                out = soundcard.get_microphone(id=str(speaker.name), include_loopback=True)
                resampler = soxr.ResampleStream(rate, SR, 1, dtype="float32")
                checked = time.monotonic()
                with out.recorder(samplerate=rate, channels=1, blocksize=frames) as recorder:
                    self.ok, self.error = True, None
                    while self._running:
                        data = recorder.record(numframes=frames)
                        self.push(resampler.resample_chunk(
                            np.ascontiguousarray(data[:, 0], dtype=np.float32)))
                        # Headphones on mid-call: the call goes to them now.
                        if time.monotonic() - checked >= Ears.FOLLOW_S:
                            checked = time.monotonic()
                            if soundcard.default_speaker().name != speaker.name:
                                break
            except Exception as exc:  # noqa: BLE001 - the output changed: open it again
                self.ok = False
                self.error = f"loopback: {type(exc).__name__}: {exc}"[:200]
                time.sleep(2)
        self.ok = False


def _com_init() -> None:
    """soundcard talks to WASAPI through COM, which each thread must join."""
    with contextlib.suppress(AttributeError):
        ctypes.windll.ole32.CoInitializeEx(None, 0)
