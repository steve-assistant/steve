"""Hearing "hey Steve", on the laptop, before anything else hears anything.

The maintainer, 5 October 2026: an always-on assistant at the laptop, but "obviously
don't want everything I say going to the AI". So the microphone feeds only
this until the wake phrase: a small model on the CPU that turns sound into a
score, keeps nothing, and transcribes nothing. Whisper is not called, and no
model hears a word, until it fires (`voice/listen.py`; the rules are
`core/desk.py`).

The front end is openWakeWord's (Apache-2.0): a mel spectrogram and Google's
speech-embedding network, both ONNX, give one 96-number embedding every 80
ms. The head that reads sixteen of them (1.28 s) and says whether they end
in "hey Steve" is ours, trained by `voice/wake_train.py` from voices the
local TTS made -- their own name for it, with no stock model to fall back on.

A false wake costs little by design: what follows a wake is transcribed
locally and dropped unless it starts with the phrase (`core/desk.wake_phrase`),
so the head is tuned to miss them rarely rather than never to fire wrongly.

The same detector runs a second time on what the laptop itself is playing
(`listen.py`'s loopback). A wake heard there too is a video, a call or the
agent's own voice, not them.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core import paths


# As voice/tts_server.py: a worktree can point at the live checkout's models.
def model_dir() -> Path:
    return paths.models_dir() / "wake"


MEL_FILE = "melspectrogram.onnx"
EMBED_FILE = "embedding_model.onnx"
HEAD_FILE = "hey_steve.npz"

SAMPLE_RATE = 16_000
# One embedding step: the embedding model advances eight mel frames of 10 ms.
CHUNK = 1280
MEL_WINDOW = 76  # mel frames per embedding (760 ms)
MEL_STEP = 8
WINDOW = 16  # embeddings per decision (1.28 s)
EMBED_DIM = 96
# The mel model needs 480 samples of context before a chunk to give its eight
# frames (a 25 ms window, 10 ms hop).
_MEL_CONTEXT = 160 * 3

# A wake is one event, however many chunks score above the line while the
# phrase passes.
REFRACTORY_S = 1.5


def _session(path: Path, threads: int = 1):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=options,
                                providers=["CPUExecutionProvider"])


class Frontend:
    """Sound to embeddings. Loaded once; shared by every `Features`.

    **Chunk by chunk, always -- training included.** The mel model floors
    quiet frames relative to the loudest moment *in what it is given*, so a
    clip transformed whole and the same clip in 80 ms chunks differ wherever
    the room is quiet: by 8% to 29% of the numbers on a TTS clip, measured 5
    October 2026. Listening is chunked, so `stream` -- what training uses --
    computes exactly the chunks `Features` would, in one batch.
    """

    def __init__(self, models: Path | None = None, *, threads: int = 1) -> None:
        # One thread by default: two of these run all day beside Whisper and
        # the TTS, and a few milliseconds every 80 ms needs no more.
        # Training passes more.
        models = models or model_dir()
        self.mel = _session(models / MEL_FILE, threads)
        self.embed = _session(models / EMBED_FILE, threads)

    def mel_frames(self, inputs: np.ndarray) -> np.ndarray:
        """Chunks with their context [n, 1760] (float, in [-1, 1]) to each
        chunk's mel frames [n, 8, 32].

        The model was trained on int16-scaled samples, and its output is read
        as x / 10 + 2 -- openWakeWord's transform, without which the
        embedding network sees numbers it never saw.

        One call per chunk, never a batch: the floor is relative to the
        loudest value in the whole tensor it is given, batch included, so a
        loud chunk batched beside a quiet one changes the quiet one's frames
        (by up to 30 in an embedding, measured against digital silence).
        """
        x = np.asarray(inputs, dtype=np.float32) * 32767.0
        out = [self.mel.run(None, {"input": row[None]})[0] for row in x]
        return np.stack([o.reshape(-1, 32)[-MEL_STEP:] for o in out]) / 10.0 + 2.0

    def embed_windows(self, windows: np.ndarray, batch: int = 512) -> np.ndarray:
        """Mel windows [n, 76, 32] to embeddings [n, 96]."""
        if not len(windows):
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        out = [self.embed.run(None, {"input_1": windows[i:i + batch, ..., None]
                                     .astype(np.float32)})[0]
               for i in range(0, len(windows), batch)]
        return np.concatenate(out).reshape(-1, EMBED_DIM)

    def stream(self, audio: np.ndarray) -> np.ndarray:
        """The embeddings `Features` makes from this audio after a reset
        [n, 96], computed at once. Trailing audio short of a chunk is not
        heard, as it would not yet have been."""
        audio = np.asarray(audio, dtype=np.float32)
        chunks = len(audio) // CHUNK
        if chunks == 0:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        padded = np.concatenate([np.zeros(_MEL_CONTEXT, np.float32), audio[:chunks * CHUNK]])
        at = np.arange(chunks)[:, None] * CHUNK + np.arange(CHUNK + _MEL_CONTEXT)[None, :]
        mel = self.mel_frames(padded[at]).reshape(-1, 32)
        ends = [MEL_STEP * (c + 1) for c in range(chunks) if MEL_STEP * (c + 1) > MEL_WINDOW]
        if not ends:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        return self.embed_windows(np.stack([mel[e - MEL_WINDOW - 1:e - 1] for e in ends]))


class Features:
    """The streaming front end: audio in as it comes, embeddings out every
    80 ms. Holds a few seconds of numbers, never the sound past a second."""

    def __init__(self, frontend: Frontend) -> None:
        self.frontend = frontend
        self.reset()

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.float32)
        self._tail = np.zeros(_MEL_CONTEXT, dtype=np.float32)
        self._mel: deque[np.ndarray] = deque(maxlen=MEL_WINDOW + MEL_STEP * 4)
        self.embeddings: deque[np.ndarray] = deque(maxlen=WINDOW * 4)

    def push(self, audio: np.ndarray) -> int:
        """Feed 16 kHz audio; returns how many new embeddings it made."""
        self._pending = np.concatenate([self._pending, np.asarray(audio, dtype=np.float32)])
        made = 0
        while len(self._pending) >= CHUNK:
            chunk, self._pending = self._pending[:CHUNK], self._pending[CHUNK:]
            frames = self.frontend.mel_frames(np.concatenate([self._tail, chunk])[None])[0]
            self._tail = chunk[-_MEL_CONTEXT:]
            self._mel.extend(frames)
            if len(self._mel) > MEL_WINDOW:
                # One frame short of the newest. A chunk's eight frames start
                # three frames before it (the context), so the newest window
                # would begin one frame off the eight-frame grid that
                # openWakeWord's own features -- the negatives the head
                # learns from -- were computed on.
                mel = np.stack(list(self._mel)[-MEL_WINDOW - 1:-1])
                self.embeddings.append(self.frontend.embed_windows(mel[None])[0])
                made += 1
        return made

    def window(self) -> np.ndarray | None:
        """The last WINDOW embeddings [16, 96], or None until there are."""
        if len(self.embeddings) < WINDOW:
            return None
        return np.stack(list(self.embeddings)[-WINDOW:])


@dataclass
class Head:
    """The trained "hey Steve" reader: sixteen embeddings to a probability.

    A small network in plain numpy -- two hidden layers -- so running it
    needs nothing beyond what the voice extra already has, and the weights
    are a file `wake_train` writes, not code.
    """

    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: np.ndarray
    w3: np.ndarray
    b3: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    threshold: float = 0.5
    # The bar while the desk's own voice is audible, measured on it by
    # `wake_train.choose`; None for a head trained before that was measured.
    over_voice: float | None = None

    @classmethod
    def load(cls, path: Path) -> Head:
        with np.load(path) as saved:
            parts = {k: saved[k] for k in ("w1", "b1", "w2", "b2", "w3", "b3", "mean", "scale")}
            threshold = float(saved["threshold"]) if "threshold" in saved else 0.5
            over = float(saved["over_voice"]) if "over_voice" in saved else None
        return cls(**parts, threshold=threshold, over_voice=over)

    def save(self, path: Path) -> None:
        extra = {} if self.over_voice is None else {"over_voice": np.float32(self.over_voice)}
        np.savez(path, w1=self.w1, b1=self.b1, w2=self.w2, b2=self.b2, w3=self.w3,
                 b3=self.b3, mean=self.mean, scale=self.scale,
                 threshold=np.float32(self.threshold), **extra)

    def scores(self, windows: np.ndarray) -> np.ndarray:
        """[n, 16, 96] windows to [n] probabilities."""
        x = (windows.reshape(len(windows), -1).astype(np.float32) - self.mean) / self.scale
        h = np.maximum(x @ self.w1 + self.b1, 0.0)
        h = np.maximum(h @ self.w2 + self.b2, 0.0)
        z = (h @ self.w3 + self.b3).reshape(-1)
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class Detector:
    """One stream's wake detector: the mic's, or the loopback's.

    `push` returns the score when a wake fires on this audio, else None. A
    wake fires once; the next can come REFRACTORY_S later.
    """

    def __init__(self, frontend: Frontend, head: Head, *, threshold: float | None = None) -> None:
        self.features = Features(frontend)
        self.head = head
        self.threshold = head.threshold if threshold is None else threshold
        self._quiet_until = 0
        self._heard = 0  # samples pushed, the stream's own clock
        # The latest window's score, fired or not: the ear logs a near miss
        # (voice/listen.Ear), so a "hey Steve" it did not take is a number in
        # the log rather than nothing (9 October 2026: two of them left no
        # trace at all).
        self.last_score = 0.0

    @property
    def seconds(self) -> float:
        """How much audio this stream has pushed: its clock, in seconds."""
        return self._heard / SAMPLE_RATE

    def push(self, audio: np.ndarray) -> float | None:
        self._heard += len(audio)
        if not self.features.push(audio):
            return None
        window = self.features.window()
        if window is None:
            return None
        score = float(self.head.scores(window[None])[0])
        self.last_score = score
        if score < self.threshold or self._heard < self._quiet_until:
            return None
        self._quiet_until = self._heard + int(REFRACTORY_S * SAMPLE_RATE)
        return score

    def reset(self) -> None:
        self.features.reset()
