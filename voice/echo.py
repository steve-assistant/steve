"""The laptop's own voice taken out of the microphone, so the user can talk over it.

On the speakers the microphone hears whatever the desk says. So a live
conversation muted their microphone while it talked (voice/desk_call.py, half
duplex): whatever the user said over it was lost, and on 7 October 2026 they felt it
"missing half of what I say". Every voice system that lets you interrupt on
a speakerphone does it the same way -- acoustic echo cancellation: the
canceller is given what is about to be played (the far end) and subtracts
its echo from what the microphone hears (the near end). WebRTC's AEC3,
through LiveKit's SDK, does it in under a millisecond per 10 ms frame.

The far end is the desk's own player (voice/audio.Player.on_play), sample by
sample as it goes to the speakers -- ahead of its echo, which a canceller
needs; the speakers' loopback capture arrives in 200 ms reads, after it. So
what is cancelled is the desk's voice, Kokoro's and GPT-Live's; a Zoom call
cancels its own.

Off (`available` False) without the SDK or if it fails: the microphone passes
through untouched and the conversation stays half duplex, as before.
"""

from __future__ import annotations

import logging
import os
import threading

import numpy as np

log = logging.getLogger("voice.echo")

SR = 16_000
FRAME = SR // 100  # 10 ms, the only size the canceller takes
# Between a far-end frame reaching the canceller and its echo reaching it
# from the microphone: the output buffer, the room, the 100 ms microphone
# read. A hint; AEC3 finds the delay itself within a few hundred ms.
DELAY_MS = 120
SWITCH_ENV = "STEVE_DESK_ECHO"  # "0" turns it off


def _frame(samples: np.ndarray):
    from livekit import rtc

    data = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    return rtc.AudioFrame(data.tobytes(), SR, 1, FRAME)


class EchoCanceller:
    """`far(audio, rate)` with what is about to play; `near(block)` with the
    microphone, which comes back cleaned (16 kHz float, 10 ms multiples)."""

    def __init__(self) -> None:
        self.available = False
        self.cancelled = 0  # near frames processed, for status
        self._lock = threading.Lock()
        self._far = np.zeros(0, np.float32)
        self._near = np.zeros(0, np.float32)
        self._resampler = None
        self._rate = 0
        if os.environ.get(SWITCH_ENV, "").strip() == "0":
            return
        try:
            from livekit import rtc

            # Echo only: noise suppression would change what the wake
            # detector was trained on.
            self._apm = rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)
            self._apm.set_stream_delay_ms(DELAY_MS)
            self.available = True
        except Exception:  # noqa: BLE001 - no canceller is half duplex, as before
            log.exception("no echo canceller: the conversation stays half duplex")

    def far(self, audio: np.ndarray, rate: int) -> None:
        """What the speakers are about to play, at `rate`. From the
        player's thread; cheap enough to run there."""
        if not self.available:
            return
        try:
            import soxr

            with self._lock:
                if self._resampler is None or rate != self._rate:
                    self._resampler = soxr.ResampleStream(rate, SR, 1, dtype="float32")
                    self._rate = rate
                self._far = np.concatenate([self._far, self._resampler.resample_chunk(
                    np.asarray(audio, np.float32))])
                while len(self._far) >= FRAME:
                    frame, self._far = self._far[:FRAME], self._far[FRAME:]
                    self._apm.process_reverse_stream(_frame(frame))
        except Exception:  # noqa: BLE001
            log.exception("the echo canceller failed on the far end; turning it off")
            self.available = False

    def near(self, block: np.ndarray) -> np.ndarray:
        """The microphone, with the desk's own voice taken out."""
        if not self.available:
            return block
        try:
            with self._lock:
                self._near = np.concatenate([self._near, np.asarray(block, np.float32)])
                out = []
                while len(self._near) >= FRAME:
                    samples, self._near = self._near[:FRAME], self._near[FRAME:]
                    frame = _frame(samples)
                    self._apm.process_stream(frame)
                    out.append(np.frombuffer(bytes(frame.data), np.int16)
                               .astype(np.float32) / 32768)
                    self.cancelled += 1
            return np.concatenate(out) if out else np.zeros(0, np.float32)
        except Exception:  # noqa: BLE001
            log.exception("the echo canceller failed on the microphone; turning it off")
            self.available = False
            return block
