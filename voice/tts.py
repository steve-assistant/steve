"""Text-to-speech client: drives the Kokoro subprocess.

See voice/tts_server.py for why TTS lives in its own process. From here it
looks like an ordinary object with a `say()` method; the process boundary
exists so Whisper can keep the GPU to itself.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from itertools import count

import numpy as np

from core import paths
from voice.speakable import to_speech

# Where a sentence ends, for streaming. Splitting on sentences rather than
# emitting the whole reply at once is what makes speech *start* quickly: the
# first clause is spoken while the rest is still being generated.
_SENTENCE_END = tuple(".!?")
MIN_CHUNK_CHARS = 24  # below this, chunking costs more overhead than it saves


@dataclass
class Speech:
    audio: np.ndarray  # float32 mono in [-1, 1]
    sample_rate: int
    generate_ms: int
    device: str

    @property
    def duration_s(self) -> float:
        return len(self.audio) / self.sample_rate


class TTSUnavailable(RuntimeError):
    pass


def split_for_streaming(text: str, min_chars: int = MIN_CHUNK_CHARS) -> list[str]:
    """Break a reply into speakable chunks at sentence boundaries.

    Markdown is stripped first: the model writes for a screen, and Kokoro
    reads "**" aloud as "asterisk-esterisk". See voice/speakable.py.

    Short fragments are merged forward: synthesising "Yes." on its own costs
    the same fixed overhead as a full sentence, so emitting it alone would add
    latency rather than remove it.
    """
    text = to_speech(text)
    chunks: list[str] = []
    current = ""
    for char in text:
        current += char
        if char in _SENTENCE_END and len(current.strip()) >= min_chars:
            chunks.append(current.strip())
            current = ""
    if current.strip():
        if chunks and len(current.strip()) < min_chars:
            chunks[-1] = f"{chunks[-1]} {current.strip()}"
        else:
            chunks.append(current.strip())
    return chunks or ([text.strip()] if text.strip() else [])


# The desk's voice. The user's pick, 7 October 2026, of six Kokoro voices set
# beside GPT-Live's cedar (data/voice-samples/): the nearest to the voice
# their live conversations and calls use, so the desk sounds like one
# assistant. It was af_heart. The server warms up in the same one
# (voice/tts_server.DEFAULT_VOICE).
VOICE = "am_echo"


class Speaker:
    """A long-lived Kokoro process.

    Start it once at worker boot: the CUDA session build plus kernel warmup is
    several seconds, and paying it per utterance would defeat the point.
    """

    def __init__(self, *, device: str = "auto", voice: str = VOICE) -> None:
        self.device_preference = device
        self.voice = voice
        self.proc: subprocess.Popen | None = None
        self.device: str | None = None
        self.sample_rate: int = 24_000
        self._lock = threading.Lock()
        self._ids = count(1)
        self._startup_s: float | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, timeout: float = 120.0) -> None:
        if self.proc and self.proc.poll() is None:
            return

        import os

        env = dict(os.environ, STEVE_TTS_DEVICE=self.device_preference)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "voice.tts_server"],
            cwd=str(paths.SOURCE),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr is NOT piped into stdout: onnxruntime writes CUDA warnings
            # there and mixing them into the binary protocol would corrupt it.
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            header, _ = self._read_frame()
            if header is None:
                raise TTSUnavailable("TTS process exited during startup")
            if header.get("type") == "ready":
                self.device = header["device"]
                self.sample_rate = header["sample_rate"]
                self._startup_s = header.get("startup_s")
                return
            if header.get("type") == "fatal":
                raise TTSUnavailable(header.get("error", "unknown"))
            # "warning" frames (e.g. CUDA fell back to CPU) are informational.
        raise TTSUnavailable("TTS process never reported ready")

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.poll() is None:
                self.proc.stdin.write(b'{"type":"shutdown"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc = None
            self.device = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status(self) -> dict:
        return {
            "available": self.alive,
            "device": self.device,
            "voice": self.voice,
            "sample_rate": self.sample_rate,
            "startup_s": self._startup_s,
        }

    # -- protocol ----------------------------------------------------------

    def _read_frame(self) -> tuple[dict | None, bytes]:
        assert self.proc is not None
        prefix = self.proc.stdout.read(8)
        if len(prefix) < 8:
            return None, b""
        header_len, audio_len = struct.unpack("<II", prefix)
        header = json.loads(self._read_exactly(header_len))
        audio = self._read_exactly(audio_len) if audio_len else b""
        return header, audio

    def _read_exactly(self, n: int) -> bytes:
        assert self.proc is not None
        buf = bytearray()
        while len(buf) < n:
            block = self.proc.stdout.read(n - len(buf))
            if not block:
                break
            buf += block
        return bytes(buf)

    # -- synthesis ---------------------------------------------------------

    def say(self, text: str, *, voice: str | None = None, speed: float = 1.0) -> Speech:
        """Synthesise one chunk. Blocking; callers stream by chunking first."""
        if not self.alive:
            raise TTSUnavailable("TTS process is not running")

        # Belt and braces: stream() already normalises, but say() is public
        # and a caller passing raw markdown should not hear punctuation.
        text = to_speech(text)
        if not text:
            return Speech(
                audio=np.zeros(0, dtype=np.float32),
                sample_rate=self.sample_rate,
                generate_ms=0,
                device=self.device or "?",
            )

        request = {
            "id": next(self._ids),
            "text": text,
            "voice": voice or self.voice,
            "speed": speed,
        }
        with self._lock:  # one request in flight; the protocol is a pipe
            self.proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            header, audio = self._read_frame()

        if header is None:
            raise TTSUnavailable("TTS process died mid-request")
        if header.get("type") == "error":
            raise TTSUnavailable(header.get("error", "unknown TTS error"))

        return Speech(
            audio=np.frombuffer(audio, dtype="<f4"),
            sample_rate=header["sample_rate"],
            generate_ms=header["generate_ms"],
            device=header.get("device", "?"),
        )

    def __enter__(self) -> Speaker:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
