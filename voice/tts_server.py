"""Kokoro text-to-speech, in its own process.

Running TTS out-of-process is not architectural tidiness, it is the only
configuration that is fast. Two findings forced it:

1. **DLL environments conflict.** onnxruntime's CUDA provider only finds the
   pip-installed cuDNN if the NVIDIA wheel directories are on PATH. Put them
   *first* and CTranslate2 (faster-whisper) picks up the wrong cuBLAS and dies
   with CUBLAS_STATUS_INVALID_VALUE. Appending fixes that, but only because
   this machine happens to have system CUDA toolkits on PATH already -- a
   fragile thing to depend on.

2. **Two CUDA runtimes in one process contend.** With a Kokoro CUDA session
   live in the same process, Whisper transcription went from 279ms to
   1000-2200ms. The GPU was not saturated; the runtimes were interfering.
   Separate processes means separate CUDA contexts, and both run at full
   speed.

Protocol: one JSON request per line on stdin, one length-prefixed binary
response on stdout. Deliberately not a socket -- pipes need no port, no
handshake, and die with the parent.
"""

from __future__ import annotations

import contextlib
import json
import os
import site
import struct
import sys
import time
from pathlib import Path

from core import paths


def model_file() -> Path:
    return paths.models_dir() / "kokoro" / "kokoro-v1.0.onnx"


def voices_file() -> Path:
    return paths.models_dir() / "kokoro" / "voices-v1.0.bin"


# What the desk speaks in, as voice/tts.VOICE says (why there); the warm-up
# uses it so the first reply is as fast as the tenth.
DEFAULT_VOICE = "am_echo"
SAMPLE_RATE = 24_000


def _register_cuda_dlls() -> list[str]:
    """Make the pip-installed CUDA libraries findable by onnxruntime.

    APPENDED, never prepended. Prepending shadows the system CUDA toolkit's
    cuBLAS, which is the one CTranslate2 wants -- and although that library
    lives in a different process now, this process may still share a machine
    with tools that care. Appending is correct and costs nothing.
    """
    dirs: list[str] = []
    for base in site.getsitepackages():
        nvidia = Path(base) / "nvidia"
        if nvidia.is_dir():
            dirs += [str(p) for p in sorted(nvidia.glob("*/bin")) if p.is_dir()]
    if dirs:
        os.environ["PATH"] = os.environ["PATH"] + os.pathsep + os.pathsep.join(dirs)
        for d in dirs:
            with contextlib.suppress(OSError):
                os.add_dll_directory(d)
    return dirs


def _write(payload: dict, audio: bytes = b"") -> None:
    """One frame: a JSON header line, then raw little-endian float32 samples."""
    header = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<II", len(header), len(audio)))
    sys.stdout.buffer.write(header)
    if audio:
        sys.stdout.buffer.write(audio)
    sys.stdout.buffer.flush()


def main() -> None:
    _register_cuda_dlls()

    import onnxruntime as ort

    # The CUDA provider emits Memcpy and node-assignment warnings on every
    # session build. They are informational and would otherwise corrupt the
    # binary protocol by landing on stderr mid-stream.
    ort.set_default_logger_severity(3)

    from kokoro_onnx import Kokoro

    model, voices = model_file(), voices_file()
    if not model.exists() or not voices.exists():
        _write({"type": "fatal", "error": f"Kokoro model files missing in {model.parent}"})
        return

    requested = os.environ.get("STEVE_TTS_DEVICE", "auto")
    providers = (
        ["CPUExecutionProvider"]
        if requested == "cpu"
        else ["CUDAExecutionProvider", "CPUExecutionProvider"]
    )

    started = time.perf_counter()
    try:
        session = ort.InferenceSession(str(model), providers=providers)
    except Exception as exc:  # noqa: BLE001 - fall back rather than die
        session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        _write({"type": "warning", "error": f"CUDA unavailable, using CPU: {exc}"[:300]})

    kokoro = Kokoro.from_session(session, str(voices))
    device = "cuda" if "CUDAExecutionProvider" in session.get_providers() else "cpu"

    # First inference builds kernels; paying it here means the user's first
    # spoken reply is as fast as the tenth.
    kokoro.create("Ready.", voice=DEFAULT_VOICE)

    _write({
        "type": "ready",
        "device": device,
        "sample_rate": SAMPLE_RATE,
        "startup_s": round(time.perf_counter() - started, 2),
    })

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            _write({"type": "error", "error": "malformed request"})
            continue

        if request.get("type") == "shutdown":
            return

        text = (request.get("text") or "").strip()
        if not text:
            _write({"type": "error", "error": "empty text", "id": request.get("id")})
            continue

        try:
            t0 = time.perf_counter()
            audio, sample_rate = kokoro.create(
                text,
                voice=request.get("voice", DEFAULT_VOICE),
                speed=float(request.get("speed", 1.0)),
            )
            _write(
                {
                    "type": "audio",
                    "id": request.get("id"),
                    "sample_rate": int(sample_rate),
                    "samples": int(audio.shape[0]),
                    "generate_ms": int((time.perf_counter() - t0) * 1000),
                    "device": device,
                },
                audio.astype("<f4").tobytes(),
            )
        except Exception as exc:  # noqa: BLE001 - a bad phrase must not kill the server
            _write({
                "type": "error",
                "id": request.get("id"),
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })


if __name__ == "__main__":
    main()
