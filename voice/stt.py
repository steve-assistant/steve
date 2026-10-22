"""Speech-to-text with faster-whisper.

Stays in the worker process, unlike TTS. CTranslate2 reaches CUDA through the
*system* CUDA toolkit rather than the pip wheels, so it does not need the DLL
juggling that forced Kokoro out of process -- and keeping it here avoids a
round trip on the leg the user is most directly waiting on.

Measured on the RTX 3080: `large-v3-turbo` at int8_float16 transcribes a
3.3-second clip in ~270ms warm, using about 2GB of VRAM. The `small` model was
barely faster and noticeably worse, so accuracy is free here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core import paths


def model_cache() -> Path:
    return paths.models_dir() / "whisper"


DEFAULT_MODEL = "large-v3-turbo"

# int8_float16 rather than float16: same accuracy in practice, roughly half
# the VRAM, and it leaves room for a local LLM later.
DEFAULT_COMPUTE_TYPE = "int8_float16"

# Whisper emits these for silence and near-silence. They are not transcription
# errors so much as the model's prior asserting itself over an empty input,
# and letting one through means the agent answers something never said.
HALLUCINATION_PHRASES = {
    "thank you.",
    "thanks for watching!",
    "thank you for watching.",
    "you",
    "bye.",
    ".",
    "...",
    "silence.",
    "[blank_audio]",
    "subs by www.zeoranger.co.uk",
}


@dataclass
class Transcript:
    text: str
    language: str
    duration_s: float
    transcribe_ms: int
    device: str
    rejected: str | None = None  # why an otherwise-valid result was discarded

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.rejected is None

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "language": self.language,
            "duration_s": round(self.duration_s, 2),
            "transcribe_ms": self.transcribe_ms,
            "device": self.device,
            "rejected": self.rejected,
        }


class Transcriber:
    """A loaded Whisper model. Load once at startup; loading takes ~20s."""

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL,
        *,
        device: str = "auto",
        compute_type: str | None = None,
    ) -> None:
        self.model_size = model_size
        self.requested_device = device
        self.compute_type = compute_type
        self.device: str | None = None
        self._model = None
        self._lock = threading.Lock()
        self._load_s: float | None = None

    def _resolve_device(self) -> tuple[str, str]:
        if self.requested_device != "auto":
            return self.requested_device, (
                self.compute_type
                or (DEFAULT_COMPUTE_TYPE if self.requested_device == "cuda" else "int8")
            )
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", self.compute_type or DEFAULT_COMPUTE_TYPE
        except Exception:  # noqa: BLE001 - absence of CUDA is not an error
            pass
        return "cpu", self.compute_type or "int8"

    def load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        device, compute_type = self._resolve_device()
        started = time.perf_counter()
        try:
            self._model = WhisperModel(
                self.model_size,
                device=device,
                compute_type=compute_type,
                download_root=str(model_cache()),
            )
        except Exception:
            if device == "cpu":
                raise
            # A CUDA failure here is usually a driver or cuDNN problem. CPU is
            # several times slower but keeps voice usable rather than dead.
            self._model = WhisperModel(
                self.model_size,
                device="cpu",
                compute_type="int8",
                download_root=str(model_cache()),
            )
            device = "cpu"

        self.device = device
        self.compute_type = compute_type
        self._load_s = time.perf_counter() - started

        # The first transcription pays CUDA kernel compilation. Spending it
        # here means the user's first spoken turn is as fast as the tenth.
        self.warm()

    def warm(self) -> int:
        """Run the model once, for real, and return how long it took (ms).

        This was a transcription of a second of zeros, which the VAD filter
        removed whole ("VAD filter removed 00:01.000 of audio"), so Whisper
        itself never ran: on the 9 October 2026 retest the first "hey
        Steve" paid the kernels instead -- 2,433 ms against ~450 warm --
        and what the user said meanwhile was lost. Faint noise, with the filter
        off, runs the encoder and the decoder."""
        if self._model is None:
            self.load()
            return 0
        noise = (0.003 * np.random.default_rng(0).standard_normal(16_000)).astype(np.float32)
        started = time.perf_counter()
        with self._lock:
            segments, _ = self._model.transcribe(noise, language="en", beam_size=1,
                                                 vad_filter=False,
                                                 condition_on_previous_text=False)
            for _ in segments:
                pass
        return int((time.perf_counter() - started) * 1000)

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def status(self) -> dict:
        return {
            "available": self.loaded,
            "model": self.model_size,
            "device": self.device,
            "compute_type": self.compute_type,
            "load_s": round(self._load_s, 1) if self._load_s else None,
        }

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = "en",
        warmup: bool = False,
    ) -> Transcript:
        """Transcribe 16kHz mono float32 audio.

        Takes an array rather than a path: the audio came from a callback in
        memory, and writing it to disk to read it straight back would add
        milliseconds and a cleanup problem for no benefit.
        """
        if self._model is None:
            self.load()

        duration = len(audio) / 16_000
        started = time.perf_counter()
        with self._lock:  # CTranslate2 models are not safe to share concurrently
            segments, info = self._model.transcribe(
                audio.astype(np.float32, copy=False),
                language=language,
                beam_size=1,  # greedy; beam search costs latency for little gain here
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,  # stops one bad turn poisoning the next
            )
            text = " ".join(segment.text for segment in segments).strip()
        elapsed = int((time.perf_counter() - started) * 1000)

        result = Transcript(
            text=text,
            language=getattr(info, "language", language or "en"),
            duration_s=duration,
            transcribe_ms=elapsed,
            device=self.device or "?",
        )
        if warmup:
            return result

        if text.lower().strip() in HALLUCINATION_PHRASES:
            result.rejected = "looks like a silence hallucination"
            result.text = ""
        return result
