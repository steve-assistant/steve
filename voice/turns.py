"""Has the user finished? An end-of-turn judge for the desk's ear.

The ear ended a request after 0.8 s of silence (voice/listen.END_SILENCE_S),
whatever the user was saying. "Okay, perfect... can you Google..." -- a pause to
think -- was two requests, the first cut off. On 7 October 2026 the user felt it
"missing half of what I say". Voice systems stopped deciding the end of a
turn by a silence timer alone some time ago: at a pause, a small model
judges from the audio whether the sentence sounds finished, and only an
unfinished one is given longer.

This is that judge: Smart Turn v3 (pipecat-ai/smart-turn-v3, BSD-2), an
8 MB ONNX model that hears the last eight seconds of audio -- intonation,
not words -- and answers with the chance the turn is complete, in tens of
milliseconds on the CPU. Fetched once into data/models by `setup`
(`python -m voice.turns setup`), like the other speech models. Without it,
or if it fails, the ear keeps its timer: nothing gets worse.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from core import paths

log = logging.getLogger("voice.turns")


def model_dir() -> Path:
    return paths.models_dir() / "smart-turn"


REPO = "pipecat-ai/smart-turn-v3"
FILE = "smart-turn-v3.2-cpu.onnx"
SR = 16_000
WINDOW_S = 8


def setup() -> Path:
    """Fetch the model into data/models. Once; blocking."""
    from huggingface_hub import hf_hub_download

    model_dir().mkdir(parents=True, exist_ok=True)
    return Path(hf_hub_download(REPO, FILE, local_dir=str(model_dir())))


class TurnJudge:
    """`judge(audio)`: the chance a capture so far is a finished turn, 0 to 1
    -- or None when it cannot say (no model, or it failed), for the caller
    to fall back on its timer."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or model_dir() / FILE
        self._session = None
        self._features = None
        self._broken = False
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return not self._broken and self.path.exists()

    def _load(self) -> None:
        import onnxruntime as ort
        from faster_whisper.feature_extractor import FeatureExtractor

        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = ort.InferenceSession(str(self.path), sess_options=options,
                                             providers=["CPUExecutionProvider"])
        self._features = FeatureExtractor(feature_size=80, sampling_rate=SR,
                                          chunk_length=WINDOW_S)

    def __call__(self, audio: np.ndarray) -> float | None:
        if not self.available:
            return None
        try:
            with self._lock:
                if self._session is None:
                    self._load()
                features = self.features(audio)
                name = self._session.get_inputs()[0].name
                out = self._session.run(None, {name: features})
            return float(np.asarray(out[0]).reshape(-1)[0])
        except Exception:  # noqa: BLE001 - the timer is the fallback, never a crash
            log.exception("the end-of-turn judge failed; the ear keeps its timer")
            self._broken = True
            return None

    def features(self, audio: np.ndarray) -> np.ndarray:
        """The last eight seconds, normalised and padded at the end with
        silence, as 80 log-mel bands: Whisper's front end, as the model was
        trained on."""
        audio = np.asarray(audio, np.float32)[-WINDOW_S * SR:]
        audio = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)
        padded = np.zeros(WINDOW_S * SR, np.float32)
        padded[:len(audio)] = audio
        mel = self._features(padded, padding=0)
        frames = WINDOW_S * SR // 160
        mel = mel[:, :frames]
        if mel.shape[1] < frames:
            mel = np.pad(mel, ((0, 0), (0, frames - mel.shape[1])))
        return mel[np.newaxis].astype(np.float32)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["setup"]:
        print(setup())
    else:
        print("usage: python -m voice.turns setup")
