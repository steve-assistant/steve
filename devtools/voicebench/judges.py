"""The end-of-turn judges the bench compares, each as a callable the ear takes.

`voice/turns.TurnJudge` gives Smart Turn the last eight seconds with the
silence after the audio. Smart Turn's own reference (`audio_utils.
truncate_audio_to_last_n_seconds` in pipecat-ai/smart-turn) puts it before,
so the window always ends at the moment being judged, and normalises over
the whole window. Fed the other way, the model sees a turn followed by
seconds of silence it never heard, which its training never showed it.
`FrontPadded` feeds it as the reference does, so the bench can show which
way cuts fewer turns before `voice/turns` changes.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from voice import turns


class FrontPadded(turns.TurnJudge):
    def features(self, audio: np.ndarray) -> np.ndarray:
        size = turns.WINDOW_S * turns.SR
        audio = np.asarray(audio, np.float32)[-size:]
        window = np.zeros(size, np.float32)
        window[size - len(audio):] = audio
        window = (window - window.mean()) / np.sqrt(window.var() + 1e-7)
        mel = self._features(window, padding=0)
        frames = size // 160
        mel = mel[:, :frames]
        if mel.shape[1] < frames:
            mel = np.pad(mel, ((0, 0), (0, frames - mel.shape[1])))
        return mel[np.newaxis].astype(np.float32)


def smart_turn(path: Path, *, front: bool) -> turns.TurnJudge:
    """Smart Turn v3.2's CPU model at `path`, padded as today or as its
    reference pads."""
    return (FrontPadded if front else turns.TurnJudge)(path)
