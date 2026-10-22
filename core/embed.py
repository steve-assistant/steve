"""Text as vectors, for memory and facts: OpenAI's embeddings, under the caps.

Memory and facts are found by meaning: a note or a fact is kept with a
vector, and what is asked is turned into one and compared. The vectors came
from a local model run on onnxruntime -- which only the voice
add-on installs. On a plain install every note failed to be kept and every
recall failed, and facts were kept with no vector, findable by name alone
(audit C2). OpenAI's `text-embedding-3-small` is on the key every model
already uses, and needs nothing installed.

What this holds:
- **Under the caps.** Each call asks `spend.may_spend` first and is billed
  in the caller's unit of work, like every model call. Its price is frozen
  here, in core/, where the night cannot lower it to buy headroom.
- **One kind of vector.** The model and the size are the database's
  (`db.EMBEDDING_MODEL`, `db.EMBEDDING_DIM`), which rebuilds the vectors it
  holds when either changes (`db.open_store`). A reply of another size is
  refused here rather than stored beside them.
- **No answer is said.** No key, a cap, a spent balance, a timeout or an
  outage raises `EmbedderUnavailable` with the reason, which the toolset
  passes on: a note says it cannot be kept, recall falls back to the facts
  found by name, and a fact is kept without a vector.

Blocking: call it through `agent/offloop.py`.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from core import db, endpoints, spend

log = logging.getLogger("embed")

MODEL = db.EMBEDDING_MODEL
DIM = db.EMBEDDING_DIM
# Dollars per million input tokens: OpenAI's price for the model on 12
# October 2026. Embeddings have no output tokens.
PRICE_PER_M_USD = 0.02
# The most texts in one request. OpenAI takes up to 2,048; at 256 a request
# of full-length memory notes stays far under its 300,000-token limit.
BATCH = 256
# A text past this is cut: the model reads at most 8,191 tokens, and a
# request with one longer input fails whole. The callers cut far shorter.
MOST_CHARS = 4_000
TIMEOUT_S = 20.0


class EmbedderUnavailable(RuntimeError):
    """No vectors now, and why, as the model can pass it on."""


def vectors(texts: list[str], *, conn: sqlite3.Connection | None = None) -> list[list[float]]:
    """Each text as a vector of `DIM` floats, in order, or
    `EmbedderUnavailable`. `conn` is the ledger the calls are asked about
    and billed in; with none, the current unit's (`spend.ledger`)."""
    if not texts:
        return []
    from core import secrets

    key = secrets.get("OPENAI_API_KEY")
    if not key:
        raise EmbedderUnavailable("memory search is unavailable: no OpenAI key is stored")
    out: list[list[float]] = []
    with spend.ledger(conn) as ledger:
        if (refused := spend.may_spend(ledger)) is not None:
            raise EmbedderUnavailable(f"memory search is paused: a spending cap is reached"
                                      f" ({refused})")
        for start in range(0, len(texts), BATCH):
            out += _batch(ledger, texts[start:start + BATCH], key)
    return out


def request_for(texts: list[str]) -> dict:
    """The whole request: the texts, cut to `MOST_CHARS`, and nothing else.
    An empty text is a space: the service refuses an empty input."""
    return {"model": MODEL, "input": [text[:MOST_CHARS] or " " for text in texts],
            "dimensions": DIM, "encoding_format": "float"}


def cost(answer: dict, request: dict) -> float:
    return tokens(answer, request) * PRICE_PER_M_USD / 1_000_000


def tokens(answer: dict, request: dict) -> int:
    """The tokens an answer says it read. One that does not say is billed
    a token for each character asked about: high, never nothing."""
    usage = answer.get("usage") or {}
    said = usage.get("prompt_tokens") or usage.get("total_tokens")
    if isinstance(said, int) and not isinstance(said, bool) and said > 0:
        return said
    return sum(len(text) for text in request["input"])


def read(answer: dict, count: int) -> list[list[float]]:
    """The answer's vectors, in the order of the texts asked about: `count`
    of them, each `DIM` floats, or `EmbedderUnavailable`."""
    items = [item for item in answer.get("data") or [] if isinstance(item, dict)]
    items.sort(key=lambda item: item["index"] if isinstance(item.get("index"), int) else -1)
    found = [list(map(float, item.get("embedding") or [])) for item in items]
    if [item.get("index") for item in items] != list(range(count)) or any(
            len(vector) != DIM for vector in found):
        raise EmbedderUnavailable(f"memory search is unavailable: OpenAI's answer was not"
                                  f" {count} vectors of {DIM}")
    return found


def _batch(ledger: sqlite3.Connection, texts: list[str], key: str) -> list[list[float]]:
    request = request_for(texts)
    started = time.perf_counter()
    try:
        answer = _call(request, key)
    except Exception as exc:
        spend.if_spent_here(ledger, exc)
        raise EmbedderUnavailable(
            f"memory search is unavailable: {spend.openai_why(exc, TIMEOUT_S)}") from exc
    try:
        spend.bill(ledger, cost(answer, request), model=MODEL,
                   input_tokens=tokens(answer, request),
                   latency_ms=int((time.perf_counter() - started) * 1000))
    except Exception:  # noqa: BLE001 - the answer came; only its bill is lost
        log.exception("could not bill an embedding")
    return read(answer, len(texts))


def _call(request: dict, key: str) -> dict:
    """The one request, its answer as JSON. `tests/gate/fences.py` refuses
    it."""
    import openai

    with openai.OpenAI(api_key=key, base_url=endpoints.openai(), timeout=TIMEOUT_S,
                       max_retries=1) as client:
        return client.embeddings.create(**request).model_dump(mode="json")
