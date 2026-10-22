"""Speech becomes text through OpenAI, under the caps, and no guess is said.

`core/hearing.py` hears the voice notes sent over Signal. Before step 4 the
local Whisper did, which only the voice add-on installs, and without it
every voice note was answered "I can't transcribe voice notes right now"
and went no further. What it must hold:
- the note goes as it came, under a name whose extension says its format,
  which is how OpenAI tells; a name without one is named by its first
  bytes, and a format OpenAI does not read is refused before it is sent;
- push-to-talk's clip goes the same way, from memory, as a WAV;
- it asks the cap before the call, and bills each note in its caller's
  unit by the minute: of the seconds the answer gives, else the clip's own,
  else the file's size counted high, from a price frozen beside the call;
- with no answer it raises `HearingUnavailable` saying why, and a spent
  balance is written down as every client writes it;
- and no test can reach the service (tests/gate/fences.py).
"""

from __future__ import annotations

import httpx
import openai
import pytest

from core import hearing, knobs, spend

REAL_CALL = hearing._call
REQUEST = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
# The first bytes of a phone's voice note: an MPEG-4 audio file, as Signal
# keeps one.
M4A = b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00" + b"\x00" * 64


@pytest.fixture
def note(tmp_path):
    path = tmp_path / "Gg-nF1rU7isa226UHPdY.m4a"
    path.write_bytes(M4A)
    return path


def _answer(text: str = "buy milk on the way home", usage: dict | None = None) -> dict:
    answer = {"text": text}
    if usage is not None:
        answer["usage"] = usage
    return answer


TOKENS = {"type": "tokens", "input_tokens": 400, "output_tokens": 12, "total_tokens": 412,
          "input_token_details": {"audio_tokens": 400, "text_tokens": 0}}


def _answering(sent: list | None = None, **kwargs):
    def call(name: str, data: bytes, key: str) -> dict:
        if sent is not None:
            sent.append((name, data))
        return _answer(**kwargs)

    return call


def _failing(exc: Exception):
    def call(name: str, data: bytes, key: str) -> dict:
        raise exc

    return call


def _status(status: int, code: str) -> openai.APIStatusError:
    error = openai.RateLimitError if status == 429 else openai.InternalServerError
    return error("no", response=httpx.Response(status, request=REQUEST),
                 body={"message": "no", "type": code, "code": code})


def _hear(conn, path, *, category: str = "chat", **unit) -> str:
    with spend.spending(category, conn=conn, **unit):
        return hearing.text(path)


# ---------------------------------------------------------------------------
# What is sent, and what comes back
# ---------------------------------------------------------------------------

def test_a_voice_note_goes_as_it_came_and_its_text_comes_back(conn, note, monkeypatch):
    sent: list = []
    monkeypatch.setattr(hearing, "_call", _answering(sent, text="  buy milk  ", usage=TOKENS))

    assert _hear(conn, note) == "buy milk"
    assert sent == [(note.name, M4A)]


@pytest.mark.parametrize("first,named", [
    (M4A, "voice.m4a"),
    (b"OggS\x00\x02" + b"\x00" * 30, "voice.ogg"),
    (b"RIFF\x24\x00\x00\x00WAVEfmt ", "voice.wav"),
], ids=["phone", "opus", "wave"])
def test_a_note_named_without_a_format_is_named_by_its_first_bytes(tmp_path, first, named):
    """signal-cli names a note by its content type; a file named another
    way would reach OpenAI as a format it cannot tell, and fail."""
    path = tmp_path / "attachment"
    path.write_bytes(first)

    assert hearing.named(path, first) == named


def test_a_format_openai_does_not_read_is_refused_before_it_is_sent(conn, tmp_path,
                                                                    monkeypatch):
    sent: list = []
    monkeypatch.setattr(hearing, "_call", _answering(sent))
    path = tmp_path / "attachment"
    path.write_bytes(b"\xff\xf1\x50\x80" + b"\x00" * 40)  # raw AAC, which it does not take

    with pytest.raises(hearing.HearingUnavailable,
                       match="^it is in a format OpenAI does not read$"):
        _hear(conn, path)

    assert sent == []


def test_a_note_longer_than_openai_takes_is_refused_before_it_is_sent(conn, note,
                                                                      monkeypatch):
    sent: list = []
    monkeypatch.setattr(hearing, "_call", _answering(sent))
    monkeypatch.setattr(hearing, "MOST_BYTES", len(M4A) - 1)

    with pytest.raises(hearing.HearingUnavailable, match="^it is longer than OpenAI takes$"):
        _hear(conn, note)

    assert sent == []


def test_the_call_asks_the_transcription_model_and_returns_the_answer_as_json(monkeypatch):
    made: list[dict] = []

    class Answer:
        def model_dump(self, mode):
            assert mode == "json"
            return _answer(usage=TOKENS)

    class Client:
        def __init__(self, **kwargs):
            made.append(kwargs)
            self.audio = self
            self.transcriptions = self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def create(self, **request):
            made.append(request)
            return Answer()

    monkeypatch.setattr(openai, "OpenAI", Client)

    answer = REAL_CALL("note.m4a", M4A, "a test's key")

    client, sent = made
    assert answer == _answer(usage=TOKENS)
    assert sent == {"model": "gpt-transcribe", "file": ("note.m4a", M4A),
                    "response_format": "json"}
    assert client["api_key"] == "a test's key" and client["timeout"] == hearing.TIMEOUT_S


# ---------------------------------------------------------------------------
# What it costs
# ---------------------------------------------------------------------------

def test_a_note_is_billed_in_its_callers_unit_with_its_tokens_recorded(conn, note,
                                                                        monkeypatch):
    """A voice note on Signal is the conversation's, and chat's. Unbilled,
    a week of voice notes would spend unseen and past every cap."""
    monkeypatch.setattr(hearing, "_call",
                        _answering(usage={**TOKENS, "seconds": 20.0}))
    session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid

    _hear(conn, note, session_id=session)

    [row] = conn.execute("SELECT category, session_id, model, input_tokens, output_tokens,"
                         " cost_usd FROM usage_events").fetchall()
    assert tuple(row)[:5] == ("chat", session, "gpt-transcribe", 400, 12)
    assert row["cost_usd"] == pytest.approx(20 / 60 * 0.0045)


def test_an_answer_that_gives_seconds_is_billed_by_the_minute(conn, note, monkeypatch):
    monkeypatch.setattr(hearing, "_call",
                        _answering(usage={"type": "duration", "seconds": 30.0}))

    _hear(conn, note)

    [row] = conn.execute("SELECT cost_usd FROM usage_events").fetchall()
    assert row["cost_usd"] == pytest.approx(0.5 * 0.0045)


def test_an_answer_that_says_nothing_of_its_cost_is_billed_high_by_size(conn, tmp_path,
                                                                       monkeypatch):
    """High, never nothing: a bill of zero would let voice notes run past
    a cap of any size. A minute of a phone's voice note is well over 60 kB,
    so this bills it more than a minute."""
    monkeypatch.setattr(hearing, "_call", _answering(usage=TOKENS))
    path = tmp_path / "a-minute.m4a"
    path.write_bytes(M4A + b"\x00" * (240_000 - len(M4A)))

    _hear(conn, path)

    [row] = conn.execute("SELECT cost_usd FROM usage_events").fetchall()
    assert row["cost_usd"] == pytest.approx(4 * 0.0045)


def test_a_clip_from_the_microphone_is_heard_as_a_wav_and_billed_by_its_length(
        conn, monkeypatch):
    """Push-to-talk's clip is held in memory. It goes under a WAV's name,
    which is how OpenAI tells its format, and is billed by the seconds it
    is: billed by its size instead, a WAV's 32 kB a second would count
    every clip eight times over."""
    sent: list = []
    monkeypatch.setattr(hearing, "_call", _answering(sent, text=" what time is it ",
                                                     usage=TOKENS))
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 320_000

    with spend.spending("chat", conn=conn):
        assert hearing.clip(wav, seconds=10.0) == "what time is it"

    assert sent == [("clip.wav", wav)]
    [row] = conn.execute("SELECT category, model, cost_usd FROM usage_events").fetchall()
    assert tuple(row)[:2] == ("chat", "gpt-transcribe")
    assert row["cost_usd"] == pytest.approx(10 / 60 * 0.0045)


def test_hearing_past_a_cap_is_refused_before_it_is_asked(conn, note, monkeypatch):
    sent: list = []
    monkeypatch.setattr(hearing, "_call", _answering(sent))
    knobs.write({"EXTENSIONS_DAY_USD": 0})

    with pytest.raises(hearing.HearingUnavailable,
                       match=r"^a spending cap is reached \(EXTENSIONS_DAY_USD: "):
        _hear(conn, note, category="extensions")

    assert sent == []


# ---------------------------------------------------------------------------
# No answer
# ---------------------------------------------------------------------------

def test_with_no_key_nothing_is_asked_and_it_says_so(conn, note, monkeypatch):
    sent: list = []
    monkeypatch.setattr(hearing, "_call", _answering(sent))
    monkeypatch.setattr("core.secrets.get", lambda name, default=None: None)

    with pytest.raises(hearing.HearingUnavailable, match="^no OpenAI key is stored$"):
        _hear(conn, note)

    assert sent == []


@pytest.mark.parametrize("exc,why", [
    (openai.APITimeoutError(REQUEST), "OpenAI gave no answer within 60 seconds"),
    (openai.APIConnectionError(request=REQUEST), "OpenAI could not be reached"),
    (_status(500, "server_error"), "OpenAI answered 500"),
], ids=["timeout", "unreachable", "outage"])
def test_with_no_answer_it_says_why_and_bills_nothing(conn, note, monkeypatch, exc, why):
    monkeypatch.setattr(hearing, "_call", _failing(exc))

    with pytest.raises(hearing.HearingUnavailable, match=f"^{why}$"):
        _hear(conn, note)

    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
    assert spend.notices(conn, caps=[spend.BALANCE]) == []


def test_a_spent_balance_is_written_down(conn, note, monkeypatch):
    """As every client writes it (core/spend.py `Guarded`): with the balance
    spent nothing that needs a model runs, and the user is told so."""
    monkeypatch.setattr(hearing, "_call", _failing(_status(429, "insufficient_quota")))

    with pytest.raises(hearing.HearingUnavailable, match="^OpenAI's balance is used up$"):
        _hear(conn, note)

    [notice] = spend.notices(conn, caps=[spend.BALANCE])
    assert notice.kind == "stopped"


def test_no_test_can_hear_for_real(note):
    with pytest.raises(RuntimeError, match="OpenAI's speech-to-text"):
        hearing._call(note.name, M4A, "a key")
