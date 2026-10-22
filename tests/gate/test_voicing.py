"""The desk's voice is OpenAI's, under the caps, and billed by what it made.

`core/voicing.py` says the desk's replies aloud now that core holds no
local voice. What it must hold:
- the line goes with the model, the voice and raw samples asked for, and
  its samples come back whole, an odd byte dropped;
- it asks the cap before the call, and bills each line in its caller's unit
  by the minutes of audio made and the text read, from prices frozen beside
  the call -- the answer gives no token count, and a bill of nothing would
  let the voice run past any cap;
- with no answer it raises `VoicingUnavailable` saying why, and a spent
  balance is written down as every client writes it;
- and no test can reach the service (tests/gate/fences.py).
"""

from __future__ import annotations

import httpx
import openai
import pytest

from core import knobs, spend, voicing

REAL_CALL = voicing._call
REQUEST = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
# Two seconds of 24 kHz 16-bit samples.
TWO_SECONDS = bytes(2 * voicing.BYTES_PER_SECOND)


def _speaking(sent: list | None = None, audio: bytes = TWO_SECONDS):
    def call(text: str, voice: str, speed: float, key: str) -> bytes:
        if sent is not None:
            sent.append((text, voice, speed))
        return audio

    return call


def _failing(exc: Exception):
    def call(text: str, voice: str, speed: float, key: str) -> bytes:
        raise exc

    return call


def _status(status: int, code: str) -> openai.APIStatusError:
    error = openai.RateLimitError if status == 429 else openai.InternalServerError
    return error("no", response=httpx.Response(status, request=REQUEST),
                 body={"message": "no", "type": code, "code": code})


def _say(conn, text: str, *, category: str = "chat", **kwargs) -> bytes:
    with spend.spending(category, conn=conn):
        return voicing.pcm(text, **kwargs)


# ---------------------------------------------------------------------------
# What is sent, and what comes back
# ---------------------------------------------------------------------------

def test_a_line_goes_in_the_desks_voice_and_its_samples_come_back(conn, monkeypatch):
    sent: list = []
    monkeypatch.setattr(voicing, "_call", _speaking(sent))

    assert _say(conn, "It's four o'clock.") == TWO_SECONDS
    assert sent == [("It's four o'clock.", "cedar", 1.0)]


def test_an_odd_byte_at_the_end_is_dropped_so_no_sample_is_shifted(conn, monkeypatch):
    """Samples are two bytes each; read with a stray byte, numpy refuses the
    buffer, and a reader that pads it would shift every sample after."""
    monkeypatch.setattr(voicing, "_call", _speaking(audio=TWO_SECONDS + b"\x01"))

    assert _say(conn, "Done.") == TWO_SECONDS


def test_the_call_asks_for_raw_samples_from_the_speech_model(monkeypatch):
    made: list = []

    class Answer:
        def read(self):
            return TWO_SECONDS

    class Client:
        def __init__(self, **kwargs):
            made.append(kwargs)
            self.audio = self
            self.speech = self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def create(self, **request):
            made.append(request)
            return Answer()

    monkeypatch.setattr(openai, "OpenAI", Client)

    audio = REAL_CALL("Done.", "cedar", 1.0, "a test's key")

    client, sent = made
    assert audio == TWO_SECONDS
    assert sent == {"model": "gpt-4o-mini-tts", "voice": "cedar", "input": "Done.",
                    "speed": 1.0, "response_format": "pcm"}
    assert client["api_key"] == "a test's key" and client["timeout"] == voicing.TIMEOUT_S


# ---------------------------------------------------------------------------
# What it costs
# ---------------------------------------------------------------------------

def test_a_line_is_billed_by_the_audio_it_made_in_its_callers_unit(conn, monkeypatch):
    """The desk's voice is chat's. Unbilled, an evening of spoken replies
    would spend unseen and past every cap."""
    monkeypatch.setattr(voicing, "_call", _speaking())

    _say(conn, "Done.")

    [row] = conn.execute("SELECT category, model, input_tokens, cost_usd"
                         " FROM usage_events").fetchall()
    assert tuple(row)[:3] == ("chat", "gpt-4o-mini-tts", 5)
    assert row["cost_usd"] == pytest.approx(2 / 60 * 0.015 + 5 * 0.60 / 1e6)


def test_speaking_past_a_cap_is_refused_before_it_is_asked(conn, monkeypatch):
    sent: list = []
    monkeypatch.setattr(voicing, "_call", _speaking(sent))
    knobs.write({"EXTENSIONS_DAY_USD": 0})

    with pytest.raises(voicing.VoicingUnavailable,
                       match=r"^a spending cap is reached \(EXTENSIONS_DAY_USD: "):
        _say(conn, "Done.", category="extensions")

    assert sent == []


# ---------------------------------------------------------------------------
# No answer
# ---------------------------------------------------------------------------

def test_with_no_key_nothing_is_asked_and_it_says_so(conn, monkeypatch):
    sent: list = []
    monkeypatch.setattr(voicing, "_call", _speaking(sent))
    monkeypatch.setattr("core.secrets.get", lambda name, default=None: None)

    with pytest.raises(voicing.VoicingUnavailable, match="^no OpenAI key is stored$"):
        _say(conn, "Done.")

    assert sent == []


@pytest.mark.parametrize("exc,why", [
    (openai.APITimeoutError(REQUEST), "OpenAI gave no answer within 30 seconds"),
    (openai.APIConnectionError(request=REQUEST), "OpenAI could not be reached"),
    (_status(500, "server_error"), "OpenAI answered 500"),
], ids=["timeout", "unreachable", "outage"])
def test_with_no_answer_it_says_why_and_bills_nothing(conn, monkeypatch, exc, why):
    monkeypatch.setattr(voicing, "_call", _failing(exc))

    with pytest.raises(voicing.VoicingUnavailable, match=f"^{why}$"):
        _say(conn, "Done.")

    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
    assert spend.notices(conn, caps=[spend.BALANCE]) == []


def test_a_spent_balance_is_written_down(conn, monkeypatch):
    monkeypatch.setattr(voicing, "_call", _failing(_status(429, "insufficient_quota")))

    with pytest.raises(voicing.VoicingUnavailable, match="^OpenAI's balance is used up$"):
        _say(conn, "Done.")

    [notice] = spend.notices(conn, caps=[spend.BALANCE])
    assert notice.kind == "stopped"


def test_no_test_can_speak_for_real():
    with pytest.raises(RuntimeError, match="OpenAI's text-to-speech"):
        voicing._call("Done.", "cedar", 1.0, "a key")
