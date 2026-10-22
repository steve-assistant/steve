"""Push-to-talk's ear and voice, on OpenAI: core's, with no local model.

The desk heard with Whisper large-v3-turbo and spoke with Kokoro, both run
on the laptop: two gigabytes of video memory, a graphics chip kept awake,
and 20 seconds to load. A product laptop has no graphics chip, and on its
CPU Whisper took 8.9 s to hear a short request (docs/plan/07-context.md,
section 10). So core hears through `core/hearing.py` and speaks through
`core/voicing.py`, on the key every model already uses; the local models
are the voice add-on's.

Each class keeps the interface of the local one it stands in for --
`voice/stt.Transcriber`, `voice/tts.Speaker` -- so `voice/pipeline.py`
does not know which it holds.

- **Billed as chat.** Each call runs in the caller's unit of work, or in
  chat's where there is none: a push-to-talk turn is a conversation.
- **A failure is said, not guessed.** Speech-to-text that cannot answer
  gives a transcript rejected with OpenAI's reason, which the app shows;
  a voice that cannot answer raises `TTSUnavailable`, and the reply is
  shown, not spoken.
- **Fixed lines are fetched once.** The stalls and the preambles are said
  at every warm-up's end; fetched each time, every start of the worker
  would pay for them again and wait on a dozen calls. They are kept in the
  data folder, by model, voice, speed and text.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import time
import wave
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from voice.speakable import to_speech
from voice.stt import HALLUCINATION_PHRASES, Transcript
from voice.tts import Speech, TTSUnavailable

log = logging.getLogger("voice")

DEVICE = "openai"
STT_SAMPLE_RATE = 16_000


@contextlib.contextmanager
def _billed():
    """The caller's unit of work, or chat's if it is in none."""
    from core import spend

    if spend.current() is not None:
        yield
        return
    with spend.spending("chat"):
        yield


def wav(audio: np.ndarray, rate: int = STT_SAMPLE_RATE) -> bytes:
    """Mono float samples as a 16-bit WAV file, in memory."""
    samples = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(samples.tobytes())
    return out.getvalue()


class CloudTranscriber:
    """`voice/stt.Transcriber`'s interface, heard by OpenAI."""

    device = DEVICE
    loaded = True

    def load(self) -> None:
        """Nothing to load."""

    def warm(self) -> int:
        return 0

    def status(self) -> dict:
        from core import hearing

        return {"available": True, "model": hearing.MODEL, "device": DEVICE}

    def transcribe(self, audio: np.ndarray, *, language: str | None = "en",
                   warmup: bool = False) -> Transcript:
        """What 16 kHz mono float32 `audio` says. `language` is OpenAI's to
        tell; `warmup` has nothing to warm."""
        from core import hearing

        seconds = len(audio) / STT_SAMPLE_RATE
        started = time.perf_counter()
        rejected = None
        try:
            with _billed():
                text = hearing.clip(wav(audio), seconds=seconds)
        except hearing.HearingUnavailable as exc:
            text, rejected = "", f"Couldn't hear it: {exc}"
        result = Transcript(text=text, language=language or "en", duration_s=seconds,
                            transcribe_ms=int((time.perf_counter() - started) * 1000),
                            device=DEVICE, rejected=rejected)
        # The models that came after Whisper still hear a breath as a sign-off.
        if text.lower().strip() in HALLUCINATION_PHRASES:
            result.rejected = "looks like a silence hallucination"
            result.text = ""
        return result


class CloudSpeaker:
    """`voice/tts.Speaker`'s interface, said by OpenAI. `kept` are lines
    said again and again -- the stalls, the preambles -- kept on disk once
    fetched."""

    def __init__(self, *, voice: str | None = None, kept: Iterable[str] = (),
                 lines_dir: Path | None = None) -> None:
        from core import voicing

        self.voice = voice or voicing.VOICE
        self.sample_rate = voicing.SAMPLE_RATE
        self.device = DEVICE
        self.kept = {to_speech(line) for line in kept}
        self._lines_dir = lines_dir
        self._started = False
        self._startup_s: float | None = None

    @property
    def lines_dir(self) -> Path:
        from core import paths

        return self._lines_dir or paths.data_dir() / "voice" / "lines"

    def start(self, timeout: float = 120.0) -> None:  # noqa: ARG002 - the Speaker's interface
        """Nothing to start; without a key the voice is unavailable, said
        now rather than at the first reply."""
        from core import secrets

        if not secrets.get("OPENAI_API_KEY"):
            raise TTSUnavailable("no OpenAI key is stored")
        self._started = True
        self._startup_s = 0.0

    def stop(self) -> None:
        self._started = False

    @property
    def alive(self) -> bool:
        return self._started

    def status(self) -> dict:
        return {"available": self.alive, "device": DEVICE, "voice": self.voice,
                "sample_rate": self.sample_rate, "startup_s": self._startup_s}

    def say(self, text: str, *, voice: str | None = None, speed: float = 1.0) -> Speech:
        """One line, said. Blocking; callers stream by chunking first."""
        from core import voicing

        if not self.alive:
            raise TTSUnavailable("the voice is not started")
        text = to_speech(text)
        if not text:
            return Speech(audio=np.zeros(0, dtype=np.float32), sample_rate=self.sample_rate,
                          generate_ms=0, device=DEVICE)
        voice = voice or self.voice
        started = time.perf_counter()
        kept = self._kept(text, voice, speed) if text in self.kept else None
        data = None
        if kept is not None:
            with contextlib.suppress(OSError):
                data = kept.read_bytes()
        if data is None:
            try:
                with _billed():
                    data = voicing.pcm(text, voice=voice, speed=speed)
            except voicing.VoicingUnavailable as exc:
                raise TTSUnavailable(str(exc)) from exc
            if kept is not None:
                self._keep(kept, data)
        audio = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        return Speech(audio=audio, sample_rate=self.sample_rate,
                      generate_ms=int((time.perf_counter() - started) * 1000), device=DEVICE)

    def _kept(self, text: str, voice: str, speed: float) -> Path:
        from core import voicing

        name = hashlib.sha256(f"{voicing.MODEL}|{voice}|{speed}|{text}".encode()).hexdigest()
        return self.lines_dir / f"{name[:32]}.pcm"

    def _keep(self, path: Path, data: bytes) -> None:
        # Written whole, then named: a worker stopped mid-write leaves no
        # half a line to be said at every start after.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_suffix(".part")
            part.write_bytes(data)
            part.replace(path)
        except OSError:
            log.warning("could not keep a spoken line; it is fetched again next time")

    def __enter__(self) -> CloudSpeaker:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
