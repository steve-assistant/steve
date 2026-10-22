"""Speech as text: OpenAI's speech-to-text, under the caps.

A voice note sent over Signal was heard by Whisper, run on the laptop --
which only the voice add-on installs. Without it every voice note was
answered "I can't transcribe voice notes right now" and went no further
(25 September 2026): the user, away from the computer, had to guess why,
and say it all again by typing. OpenAI's model is on the key every model
already uses, needs nothing installed, and reads the .m4a a phone sends as
it comes. Push-to-talk at the desk is heard the same way (`clip`): core
holds no local model, so a laptop without a graphics chip is not left
waiting on Whisper.

It was `gpt-4o-mini-transcribe`, which OpenAI shuts down on 26 February
2027; `gpt-transcribe` is its successor, billed by the minute.

What this holds:
- **Under the caps.** Each call asks `spend.may_spend` first and is billed
  in the caller's unit of work, like every model call. Its price is frozen
  here, in core/, where the night cannot lower it to buy headroom.
- **No guess is said.** No key, a cap, a spent balance, a format it cannot
  read, a timeout or an outage raises `HearingUnavailable` with the reason,
  and the channel says it and asks for the message typed.

Blocking: call it through `agent/offloop.py`.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from core import endpoints, spend

log = logging.getLogger("hearing")

MODEL = "gpt-transcribe"
# Dollars a minute of audio heard, OpenAI's price for the model on 16
# October 2026. It is billed by the minute: by the seconds the answer gives,
# else the seconds the caller knows the clip to be, else the seconds the
# file's size makes it if it were 8 kb/s -- lower than any phone records a
# voice note at, so the bill is high, never nothing.
USD_PER_MINUTE = 0.0045
BYTES_PER_MINUTE = 60_000
# OpenAI takes files up to 25 MB.
MOST_BYTES = 25 * 1024 * 1024
TIMEOUT_S = 60.0

# What OpenAI reads, by the file name's extension, which is how it tells.
FORMATS = {"flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "oga", "ogg", "wav", "webm"}
# A file named without one is named by its first bytes.
_MAGIC = ((4, b"ftyp", "m4a"), (0, b"OggS", "ogg"), (0, b"RIFF", "wav"), (0, b"fLaC", "flac"),
          (0, b"\x1a\x45\xdf\xa3", "webm"), (0, b"ID3", "mp3"))


class HearingUnavailable(RuntimeError):
    """No text for a voice note, and why, as a reply can say it."""


def text(path: Path, *, conn: sqlite3.Connection | None = None) -> str:
    """What is said in the voice note at `path`, or `HearingUnavailable`.
    `conn` is the ledger the call is asked about and billed in; with none,
    the current unit's (`spend.ledger`)."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HearingUnavailable("the voice note could not be read") from exc
    if len(data) > MOST_BYTES:
        raise HearingUnavailable("it is longer than OpenAI takes")
    return _heard(named(path, data), data, None, conn)


def clip(wav: bytes, *, seconds: float, conn: sqlite3.Connection | None = None) -> str:
    """What is said in `wav`, a clip held in memory -- push-to-talk's, `seconds`
    long -- or `HearingUnavailable`. As `text`, without a file: the clip came
    from the microphone, and writing it out to read it back would cost a
    cleanup for nothing."""
    if len(wav) > MOST_BYTES:
        raise HearingUnavailable("it is longer than OpenAI takes")
    return _heard("clip.wav", wav, seconds, conn)


def _heard(name: str, data: bytes, seconds: float | None,
           conn: sqlite3.Connection | None) -> str:
    from core import secrets

    key = secrets.get("OPENAI_API_KEY")
    if not key:
        raise HearingUnavailable("no OpenAI key is stored")
    with spend.ledger(conn) as ledger:
        if (refused := spend.may_spend(ledger)) is not None:
            raise HearingUnavailable(f"a spending cap is reached ({refused})")
        started = time.perf_counter()
        try:
            answer = _call(name, data, key)
        except Exception as exc:
            spend.if_spent_here(ledger, exc)
            raise HearingUnavailable(spend.openai_why(exc, TIMEOUT_S)) from exc
        try:
            used = answer.get("usage") or {}
            spend.bill(ledger, cost(answer, len(data), seconds), model=MODEL,
                       input_tokens=_count(used.get("input_tokens")),
                       output_tokens=_count(used.get("output_tokens")),
                       latency_ms=int((time.perf_counter() - started) * 1000))
        except Exception:  # noqa: BLE001 - the answer came; only its bill is lost
            log.exception("could not bill what was heard")
    return str(answer.get("text") or "").strip()


def named(path: Path, data: bytes) -> str:
    """The name the file is sent under: its own, if its extension is one
    OpenAI reads, else one its first bytes say. `HearingUnavailable` if
    neither does."""
    if path.suffix.lower().lstrip(".") in FORMATS:
        return path.name
    for at, magic, extension in _MAGIC:
        if data[at:at + len(magic)] == magic:
            return f"voice.{extension}"
    raise HearingUnavailable("it is in a format OpenAI does not read")


def cost(answer: dict, size: int, seconds: float | None = None) -> float:
    """What an answer cost, by the minute: of the seconds it says it heard,
    else `seconds`, the clip's known length, else the file's size."""
    used = answer.get("usage") or {}
    for heard in (used.get("seconds"), seconds):
        if isinstance(heard, (int, float)) and not isinstance(heard, bool) and heard > 0:
            return heard / 60 * USD_PER_MINUTE
    return size / BYTES_PER_MINUTE * USD_PER_MINUTE


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _call(name: str, data: bytes, key: str) -> dict:
    """The one request, its answer as JSON. `tests/gate/fences.py` refuses
    it."""
    import openai

    with openai.OpenAI(api_key=key, base_url=endpoints.openai(), timeout=TIMEOUT_S,
                       max_retries=1) as client:
        return client.audio.transcriptions.create(
            model=MODEL, file=(name, data), response_format="json").model_dump(mode="json")
