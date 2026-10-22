"""Text as speech: OpenAI's voice, under the caps.

The desk spoke with Kokoro, run on the laptop in a process of its own: a
gigabyte of memory, a graphics chip kept awake, and a voice only the voice
add-on installs. A product laptop has no graphics chip to spare, and core
holds no local model, so without the add-on push-to-talk could hear the
user and not answer aloud. `gpt-4o-mini-tts` is on the key every model
already uses and needs nothing installed.

It shuts down on 6 January 2027, and its successor speaks only on the
realtime socket; the move is a later step's, due before January. Only this
module and `voice/cloud.py` know which voice it is.

What this holds:
- **Under the caps.** Each line asks `spend.may_spend` first and is billed
  in the caller's unit of work, like every model call. The price is frozen
  here, in core/, where the night cannot lower it to buy headroom.
- **Billed by what was made.** The answer is raw samples and says nothing
  of its tokens, so a line is billed by the seconds of audio it holds, at
  OpenAI's own estimate a minute, and its text counted high, a token a
  character -- never nothing.
- **No guess is said.** No key, a cap, a spent balance, a timeout or an
  outage raises `VoicingUnavailable` with the reason, and the reply is
  shown, not spoken.

Blocking: call it from a thread.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from core import endpoints, spend

log = logging.getLogger("voicing")

MODEL = "gpt-4o-mini-tts"
# The voice OpenAI recommends for the model, and the live conversation's.
VOICE = "cedar"
# Raw 16-bit samples, mono, at 24 kHz: the fastest to its first sound, and
# no file to decode.
SAMPLE_RATE = 24_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
# Dollars, OpenAI's prices for the model on 16 October 2026: a minute of
# the audio it makes (its estimate, from $12 a million audio tokens), and a
# million tokens of the text it reads.
USD_PER_MINUTE = 0.015
INPUT_PER_M_USD = 0.60
# OpenAI reads at most 4,096 characters a line.
MOST_CHARS = 4096
TIMEOUT_S = 30.0


class VoicingUnavailable(RuntimeError):
    """No speech for a line, and why, as the app can show it."""


def pcm(text: str, *, voice: str = VOICE, speed: float = 1.0,
        conn: sqlite3.Connection | None = None) -> bytes:
    """`text` said in `voice`: raw 16-bit samples at SAMPLE_RATE, or
    `VoicingUnavailable`. `conn` is the ledger the call is asked about and
    billed in; with none, the current unit's (`spend.ledger`)."""
    from core import secrets

    if len(text) > MOST_CHARS:
        raise VoicingUnavailable("the line is longer than OpenAI says")
    key = secrets.get("OPENAI_API_KEY")
    if not key:
        raise VoicingUnavailable("no OpenAI key is stored")
    with spend.ledger(conn) as ledger:
        if (refused := spend.may_spend(ledger)) is not None:
            raise VoicingUnavailable(f"a spending cap is reached ({refused})")
        started = time.perf_counter()
        try:
            audio = _call(text, voice, speed, key)
        except Exception as exc:
            spend.if_spent_here(ledger, exc)
            raise VoicingUnavailable(spend.openai_why(exc, TIMEOUT_S)) from exc
        try:
            spend.bill(ledger, cost(text, len(audio)), model=MODEL,
                       input_tokens=_tokens(text),
                       latency_ms=int((time.perf_counter() - started) * 1000))
        except Exception:  # noqa: BLE001 - the speech came; only its bill is lost
            log.exception("could not bill a spoken line")
    # An odd byte would shift every sample after it into noise.
    return audio[:len(audio) - len(audio) % 2]


def cost(text: str, size: int) -> float:
    """What a line cost: the minutes of audio `size` bytes hold, and its text."""
    return (size / BYTES_PER_SECOND / 60 * USD_PER_MINUTE
            + _tokens(text) * INPUT_PER_M_USD / 1_000_000)


def _tokens(text: str) -> int:
    # A token is about four characters of English; one a character is high.
    return len(text)


def _call(text: str, voice: str, speed: float, key: str) -> bytes:
    """The one request, its audio. `tests/gate/fences.py` refuses it."""
    import openai

    with openai.OpenAI(api_key=key, base_url=endpoints.openai(), timeout=TIMEOUT_S,
                       max_retries=1) as client:
        return client.audio.speech.create(
            model=MODEL, voice=voice, input=text, speed=speed,
            response_format="pcm").read()
