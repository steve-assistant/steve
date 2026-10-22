"""Text becomes vectors through OpenAI, under the caps, and no answer is said so.

`core/embed.py` turns memories, facts and what is asked of them into
vectors. What it must hold:
- the vectors are read from the answer OpenAI gives, as recorded
  (tests/gate/fixtures/embed/captured.json), in the order of the texts
  asked about. Out of order, every note would be filed under another's
  vector, and recall would answer with the wrong ones, plausibly;
- an answer of another size or count is refused, not stored: the vector
  table takes one size, and a vector short of one text shifts the rest;
- the request is the texts and nothing else, an empty one a space and a
  long one cut, at the model and size the database records;
- it asks the cap before the call, and bills each request in its caller's
  unit, from a price frozen beside the call;
- with no answer it raises `EmbedderUnavailable` saying why, and a spent
  balance is written down as every client writes it;
- and no test can reach the service (tests/gate/fences.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import openai
import pytest

from core import db, embed, knobs, spend

CAPTURED = json.loads((Path(__file__).parent / "fixtures" / "embed" / "captured.json")
                      .read_text(encoding="utf-8"))
REAL_CALL = embed._call
REQUEST = httpx.Request("POST", "https://api.openai.com/v1/embeddings")


def _answer(request: dict, tokens: int | None = 7) -> dict:
    """An answer like OpenAI's: each text's vector, all of it the first
    letter's code, so a test can tell whose is whose."""
    answer = {"object": "list", "model": request["model"],
              "data": [{"object": "embedding", "index": i,
                        "embedding": [float(ord(text[0]))] * request["dimensions"]}
                       for i, text in enumerate(request["input"])]}
    if tokens is not None:
        answer["usage"] = {"prompt_tokens": tokens, "total_tokens": tokens}
    return answer


def _answering(sent: list[dict] | None = None, **kwargs):
    def call(request: dict, key: str) -> dict:
        if sent is not None:
            sent.append(request)
        return _answer(request, **kwargs)

    return call


def _failing(exc: Exception):
    def call(request: dict, key: str) -> dict:
        raise exc

    return call


def _status(status: int, code: str) -> openai.APIStatusError:
    error = openai.RateLimitError if status == 429 else openai.InternalServerError
    return error("no", response=httpx.Response(status, request=REQUEST),
                 body={"message": "no", "type": code, "code": code})


def _embed(conn, texts: list[str], *, category: str = "chat", **unit) -> list[list[float]]:
    with spend.spending(category, conn=conn, **unit):
        return embed.vectors(texts)


# ---------------------------------------------------------------------------
# The answer, as OpenAI gives it
# ---------------------------------------------------------------------------

def test_the_vectors_are_read_from_the_answer_openai_gives():
    answer = CAPTURED["answer"]

    found = embed.read(answer, 2)

    assert found == [item["embedding"] for item in answer["data"]]
    assert all(len(vector) == embed.DIM for vector in found)


def test_the_request_is_the_one_that_answer_was_given_for():
    """The model and size the database records, floats, and the texts."""
    assert embed.request_for(CAPTURED["request"]["input"]) == CAPTURED["request"]
    assert CAPTURED["request"]["model"] == db.EMBEDDING_MODEL
    assert CAPTURED["request"]["dimensions"] == db.EMBEDDING_DIM


def test_the_vectors_come_back_in_the_order_the_texts_were_asked_about():
    """OpenAI numbers each vector by its text; nothing promises the list is
    in that order."""
    answer = _answer(embed.request_for(["alpha", "beta", "gamma"]))
    answer["data"].reverse()

    found = embed.read(answer, 3)

    assert [vector[0] for vector in found] == [ord("a"), ord("b"), ord("g")]


@pytest.mark.parametrize("change", [
    lambda data: data[1].update(embedding=[0.5] * (embed.DIM - 1)),
    lambda data: data.pop(),
    lambda data: data[1].update(index=0),
    lambda data: data[1].update(index="1"),
    lambda data: data.append({"index": 3, "embedding": [0.5] * embed.DIM}),
], ids=["short", "missing", "twice", "unnumbered", "extra"])
def test_an_answer_of_another_size_or_count_is_refused(change):
    answer = _answer(embed.request_for(["alpha", "beta", "gamma"]))
    change(answer["data"])

    with pytest.raises(embed.EmbedderUnavailable,
                       match=f"OpenAI's answer was not 3 vectors of {embed.DIM}$"):
        embed.read(answer, 3)


def test_an_empty_text_is_sent_as_a_space_and_a_long_one_cut():
    """The service refuses an empty input, and fails a whole request for
    one past the model's length."""
    request = embed.request_for(["", "x" * (embed.MOST_CHARS + 50), "kept"])

    assert request["input"] == [" ", "x" * embed.MOST_CHARS, "kept"]


def test_the_call_sends_the_request_as_given_and_returns_the_answer_as_json(monkeypatch):
    made: list[dict] = []

    class Answer:
        def model_dump(self, mode):
            assert mode == "json"
            return CAPTURED["answer"]

    class Client:
        def __init__(self, **kwargs):
            made.append(kwargs)
            self.embeddings = self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def create(self, **request):
            made.append(request)
            return Answer()

    monkeypatch.setattr(openai, "OpenAI", Client)

    answer = REAL_CALL(CAPTURED["request"], "a test's key")

    client, sent = made
    assert answer == CAPTURED["answer"] and sent == CAPTURED["request"]
    assert client["api_key"] == "a test's key" and client["timeout"] == embed.TIMEOUT_S


# ---------------------------------------------------------------------------
# What it costs
# ---------------------------------------------------------------------------

def test_embedding_is_billed_in_its_callers_unit(conn, monkeypatch):
    """A recall in a conversation is chat's; the hourly re-embedding is
    background work's. Unbilled, embedding a whole memory folder would
    spend unseen and past every cap."""
    monkeypatch.setattr(embed, "_call", _answering(tokens=16))

    _embed(conn, ["a note"])
    _embed(conn, ["a fact"], category="background", part="routines", job_id=3)

    rows = conn.execute("SELECT category, part, job_id, model, input_tokens, output_tokens,"
                        " cost_usd FROM usage_events ORDER BY id").fetchall()
    assert [tuple(r)[:6] for r in rows] == [
        ("chat", None, None, "text-embedding-3-small", 16, 0),
        ("background", "routines", 3, "text-embedding-3-small", 16, 0),
    ]
    assert rows[0]["cost_usd"] == pytest.approx(16 * 0.02 / 1e6)


def test_an_answer_that_does_not_say_its_tokens_is_billed_a_token_a_character(conn,
                                                                               monkeypatch):
    """High, never nothing: a bill of zero would let embedding run past a
    cap of any size."""
    monkeypatch.setattr(embed, "_call", _answering(tokens=None))

    _embed(conn, ["four", "seven!!"])

    [row] = conn.execute("SELECT input_tokens, cost_usd FROM usage_events").fetchall()
    assert row["input_tokens"] == 11
    assert row["cost_usd"] == pytest.approx(11 * 0.02 / 1e6)


def test_many_texts_go_a_batch_to_a_request_each_billed_and_in_order(conn, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(embed, "_call", _answering(sent))
    monkeypatch.setattr(embed, "BATCH", 2)

    found = _embed(conn, ["alpha", "beta", "gamma"])

    assert [r["input"] for r in sent] == [["alpha", "beta"], ["gamma"]]
    assert [vector[0] for vector in found] == [ord("a"), ord("b"), ord("g")]
    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 2


def test_no_texts_ask_nothing(conn, monkeypatch):
    monkeypatch.setattr(embed, "_call", _failing(AssertionError("asked")))

    assert _embed(conn, []) == []


def test_embedding_past_a_cap_is_refused_before_it_is_asked(conn, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(embed, "_call", _answering(sent))
    knobs.write({"EXTENSIONS_DAY_USD": 0})

    with pytest.raises(embed.EmbedderUnavailable,
                       match=r"^memory search is paused: a spending cap is reached"
                             r" \(EXTENSIONS_DAY_USD: "):
        _embed(conn, ["a note"], category="extensions")

    assert sent == []


# ---------------------------------------------------------------------------
# No answer
# ---------------------------------------------------------------------------

def test_with_no_key_nothing_is_asked_and_it_says_so(conn, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(embed, "_call", _answering(sent))
    monkeypatch.setattr("core.secrets.get", lambda name, default=None: None)

    with pytest.raises(embed.EmbedderUnavailable,
                       match="^memory search is unavailable: no OpenAI key is stored$"):
        _embed(conn, ["a note"])

    assert sent == []


@pytest.mark.parametrize("exc,why", [
    (openai.APITimeoutError(REQUEST), "OpenAI gave no answer within 20 seconds"),
    (openai.APIConnectionError(request=REQUEST), "OpenAI could not be reached"),
    (_status(500, "server_error"), "OpenAI answered 500"),
    (_status(429, "rate_limit_exceeded"), "OpenAI answered 429"),
], ids=["timeout", "unreachable", "outage", "busy"])
def test_with_no_answer_it_says_why_and_bills_nothing(conn, monkeypatch, exc, why):
    monkeypatch.setattr(embed, "_call", _failing(exc))

    with pytest.raises(embed.EmbedderUnavailable,
                       match=f"^memory search is unavailable: {why}$"):
        _embed(conn, ["a note"])

    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
    assert spend.notices(conn, caps=[spend.BALANCE]) == []


def test_a_spent_balance_is_written_down(conn, monkeypatch):
    """As every client writes it (core/spend.py `Guarded`): with the balance
    spent nothing that needs a model runs, and the user is told so."""
    monkeypatch.setattr(embed, "_call", _failing(_status(429, "insufficient_quota")))

    with pytest.raises(embed.EmbedderUnavailable,
                       match="^memory search is unavailable: OpenAI's balance is used up$"):
        _embed(conn, ["a note"])

    [notice] = spend.notices(conn, caps=[spend.BALANCE])
    assert notice.kind == "stopped"
