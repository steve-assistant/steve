"""One way to ask Jev a typed question, for every site that asks one.

A decision is a calibrated answer with no text: the probability that a
statement holds (`Predicate`), or which of a few named choices fits, with
a probability for each (`Choice`). The injection screen, the action critic,
the browser's logged-in clicks, task splitting and the meeting follow-up
each ask one, and each already knows what to do when there is no answer.

Jev (TypeSafe's decision model, `typesafe/jev-1.13`) answers, through
OpenRouter's decisions endpoint. Step 3 planned to move to OpenAI's
Decisions API, so that one key would do, and measured it as the screen
first. It caught 12 of the 50 attacks planted in BIPIA's emails that Jev
caught 40 of, and 19 of the red team's 20 to Jev's 20: it read a task out
of place in an email -- "translate this", "how do I back up my folder?" --
as ordinary mail, and scored most planted lines as no attempt at all.
Reworded questions, smaller windows and Luna itself did not close the gap
(docs/plan/spikes/s4-decisions.md). The maintainer kept Jev, and made its
key a required one (12 October 2026).

What this prevents:
- **A decision no cap could see.** The call lived in three copies (the
  screen, the critic, and one agent module for the rest), each with its
  own handling of errors, and none billed to the ledger. Here every ask
  asks `spend.may_spend` first, and every answered call is billed through
  `spend.bill`, in the caller's unit, before its answer is used.
- **An outage that stalls every read.** Each screened read waited out its
  timeout before reaching its fallback, and a long email is read in up to
  forty parts. After `BREAK_AFTER` failures in a row, every ask is no
  answer at once for `BREAK_S`; then one is let through to see.
- **An answer taken on trust.** A probability outside 0 to 1, a choice the
  question did not offer, or an answer of another kind is no answer to that
  question, never a guess. Each site's safe side is its fallback.

It never raises. No key, a spending cap, an error or a reply with no
answers is a `Decision` with no answers and the reason in `error`. A
question answered badly, or not at all, is missing from `answers`, and
named in `error`.

The transport is one POST through a pooled httpx2 client. Every request
asks OpenRouter for zero data retention and no data collection; Jev's
endpoint is on its zero-data-retention list. The request and reply shapes
are pinned by captured exchanges (tests/gate/test_decide.py).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx2

from core import endpoints, spend

log = logging.getLogger(__name__)

# Under OpenRouter's API (`core/endpoints.openrouter`).
DECISIONS = "/alpha/decisions"
# Pinned: a comparison run against a model that changed halfway through
# measures nothing.
MODEL = "typesafe/jev-1.13"
PROVIDER = {"zdr": True, "data_collection": "deny"}
# Dollars per million input tokens; output is free. Each reply says what it
# cost, and that is the bill; this prices a reply that does not, as the
# captured replies' costs work out (tests/gate/fixtures/decisions).
PRICE_PER_M_INPUT = 0.042
# Jev answered the personal edition's 130 benchmark calls in 283 ms at the
# median, 756 ms at the 99th percentile and 0.86 s at the slowest. Past a
# few seconds something is wrong, and the caller's fallback is better than
# the wait.
TIMEOUT_S = 3.0
# A busy or failing service is asked once more, after the wait it asks for,
# up to `RETRY_WAIT_MAX_S`. Not a timeout: the caller has waited enough.
# Not an account out of credit (402): credit does not come back in a second.
RETRY_WAIT_S = 0.5
RETRY_WAIT_MAX_S = 2.0
BREAK_AFTER = 3
BREAK_S = 30.0
# Inputs asked at once by `ask_many`: a long item's parts.
PARALLEL = 8


@dataclass(frozen=True)
class Predicate:
    """How likely a statement about the input is to hold."""

    name: str
    instructions: str

    def wire(self) -> dict:
        return {"type": "noul", "instructions": self.instructions}


@dataclass(frozen=True)
class Choice:
    """Which of `choices` fits the input: each value with what it means, in
    the order the service is shown them."""

    name: str
    instructions: str
    choices: Mapping[str, str]

    def wire(self) -> dict:
        return {"type": "choice", "instructions": self.instructions,
                "criteria": dict(self.choices)}


Question = Predicate | Choice


@dataclass(frozen=True)
class Answer:
    """One question's answer: a predicate's `probability`, or a choice's
    `choice` with each value's `probabilities` and the `confidence`. What
    the reply left out is None, or missing from `probabilities`."""

    probability: float | None = None
    choice: str | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None


@dataclass
class Decision:
    """The answers by question name, and why any asked is missing."""

    answers: dict[str, Answer] = field(default_factory=dict)
    error: str = ""
    cost_usd: float = 0.0
    latency_ms: int = 0


@dataclass(frozen=True)
class Reply:
    """What the service said: the status, the body, and how long it asked
    to be left before the next try (`Retry-After`), if it said."""

    status: int
    body: dict
    retry_after: float | None = None


class Breaker:
    """No answer at once while the service is down. After `BREAK_AFTER`
    failures in a row it is open for `BREAK_S`; then the next ask goes
    through, and an answer closes it while a failure opens it again."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0

    def open_for(self) -> float:
        """Seconds until an ask may go through: 0 when one may now."""
        with self._lock:
            return max(0.0, self._open_until - self._clock())

    def failed(self, why: str) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= BREAK_AFTER:
                self._open_until = self._clock() + BREAK_S
                log.warning("Jev failed %d times in a row (%s); no answers for %gs",
                            self._failures, why, BREAK_S)

    def answered(self) -> None:
        with self._lock:
            self._failures, self._open_until = 0, 0.0


_breaker = Breaker()
_client: httpx2.Client | None = None
_client_lock = threading.Lock()
_sleep = time.sleep


def ask(conn: sqlite3.Connection | None, input: str, questions: Sequence[Question], *,
        key: str | None = None) -> Decision:
    """`questions` about `input`, in one call. Blocking; never raises.
    `key` None is the keychain's; an empty one is none."""
    return ask_many(conn, [input], questions, key=key)[0]


def ask_many(conn: sqlite3.Connection | None, inputs: Sequence[str],
             questions: Sequence[Question], *, key: str | None = None,
             parallel: int = PARALLEL) -> list[Decision]:
    """The same `questions` about each of `inputs`, at most `parallel` at
    once: the parts of one long item. Blocking; never raises.

    The cap is asked once, before any call, and every bill is written here,
    in the caller's thread: `conn` belongs to it, and so does the unit of
    work a bill is filed under. Threads a pool starts carry neither. With no
    `conn`, the unit's ledger is opened for the ask (`spend.ledger`)."""
    try:
        if not questions:
            raise ValueError("no questions to ask")
        if len({q.name for q in questions}) != len(questions):
            raise ValueError("two questions share a name")
        if key is None:
            from core import secrets

            key = secrets.get("OPENROUTER_API_KEY") or ""
        if not key:
            return [Decision(error="no OpenRouter key") for _ in inputs]
        with spend.ledger(conn) as conn:
            if (refusal := spend.may_spend(conn)) is not None:
                return [Decision(error=f"a spending cap is reached: {refusal}") for _ in inputs]
            payloads = [{"model": MODEL, "state": text,
                         "questions": {q.name: q.wire() for q in questions},
                         "provider": dict(PROVIDER)} for text in inputs]
            if len(payloads) <= 1:
                calls = [_call(payload, key) for payload in payloads]
            else:
                with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(payloads)))) as pool:
                    calls = list(pool.map(lambda payload: _call(payload, key), payloads))
            return [_settle(conn, payload, questions, *call)
                    for payload, call in zip(payloads, calls, strict=True)]
    except Exception as exc:  # noqa: BLE001 - no answer is each caller's to handle
        log.exception("a decision could not be asked")
        return [Decision(error=f"{type(exc).__name__}: {exc}"[:300]) for _ in inputs]


def _call(payload: dict, key: str) -> tuple[Reply | None, str, int]:
    """One request, tried twice if the service was busy or failing: the
    reply that came back with 200, or None and why, and how long it took."""
    if (wait := _breaker.open_for()) > 0:
        return None, f"Jev is not answering; asked again in {wait:.0f}s", 0
    started = time.perf_counter()
    error = ""
    for attempt in (1, 2):
        try:
            reply = _post(payload, key)
        except httpx2.TimeoutException:
            error = f"no answer in {TIMEOUT_S:g}s"
            break
        except Exception as exc:  # noqa: BLE001 - a failed call is no answer
            error = f"{type(exc).__name__}: {exc}"
            break
        if reply.status == 200:
            return reply, "", _ms(started)
        error = _said(reply)
        if attempt == 1 and (reply.status == 429 or reply.status >= 500):
            wait = RETRY_WAIT_S if reply.retry_after is None else reply.retry_after
            _sleep(min(max(0.0, wait), RETRY_WAIT_MAX_S))
            continue
        break
    _breaker.failed(error[:100])
    return None, error[:300], _ms(started)


def _settle(conn: sqlite3.Connection, payload: dict, questions: Sequence[Question],
            reply: Reply | None, error: str, ms: int) -> Decision:
    """The bill for a reply, then its answers."""
    if reply is None:
        return Decision(error=error, latency_ms=ms)
    usage = reply.body.get("usage") if isinstance(reply.body.get("usage"), dict) else {}
    tokens = _count(usage.get("input_tokens"))
    if tokens is None:
        # A reply that does not say what it used is billed as if each
        # character of the request were a token: high, never nothing.
        tokens = len(json.dumps(payload))
    cost = usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0:
        cost = tokens * PRICE_PER_M_INPUT / 1_000_000
    cost = float(cost)
    try:
        spend.bill(conn, cost, model=MODEL, input_tokens=tokens,
                   output_tokens=_count(usage.get("output_tokens")) or 0, latency_ms=ms)
    except Exception:  # noqa: BLE001 - the answer came; only its bill is lost
        log.exception("could not bill a decision")
    read = _read(reply.body, questions)
    if read is None:
        _breaker.failed("a reply with no answers")
        return Decision(error="a reply with no answers", cost_usd=cost, latency_ms=ms)
    _breaker.answered()
    answers, missing = read
    return Decision(answers, "; ".join(missing)[:300], cost, ms)


def _read(body: dict, questions: Sequence[Question]
          ) -> tuple[dict[str, Answer], list[str]] | None:
    """The answers to `questions` in a reply, by name, and why any is
    missing; None if the reply holds no answers at all."""
    found = body.get("answers")
    if not isinstance(found, dict):
        return None
    answers: dict[str, Answer] = {}
    missing: list[str] = []
    for question in questions:
        item = found.get(question.name)
        if not isinstance(item, dict):
            missing.append(f"{question.name}: not answered")
        elif (answer := _answer(question, item)) is None:
            missing.append(f"{question.name}: answered in a shape not asked for")
        else:
            answers[question.name] = answer
    return answers, missing


def _answer(question: Question, item: dict) -> Answer | None:
    """One answer, if it is the kind asked and every number in it is a
    probability; otherwise None."""
    if isinstance(question, Predicate):
        p = _probability(item.get("noul"))
        return Answer(probability=p) if item.get("type") == "noul" and p is not None else None
    if item.get("type") != "choice" or item.get("choice") not in question.choices:
        return None
    given = item.get("probabilities") or {}
    if not isinstance(given, dict):
        return None
    probabilities: dict[str, float] = {}
    for value, p in given.items():
        if value not in question.choices or (p := _probability(p)) is None:
            return None
        probabilities[value] = p
    confidence = item.get("confidence")
    if confidence is not None and (confidence := _probability(confidence)) is None:
        return None
    return Answer(choice=item["choice"], probabilities=probabilities, confidence=confidence)


def _probability(value) -> float | None:
    """A number from 0 to 1, or None. Not a bool, which Python counts as one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if 0.0 <= value <= 1.0 else None


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _said(reply: Reply) -> str:
    """'HTTP 400: Model typesafe/jev-0 does not exist', from OpenRouter's
    error envelope."""
    error = reply.body.get("error") if isinstance(reply.body.get("error"), dict) else {}
    return f"HTTP {reply.status}: {str(error.get('message') or '')[:200]}"


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _post(payload: dict, key: str) -> Reply:
    """The one call out. tests/gate/fences.py refuses it."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx2.Client(timeout=TIMEOUT_S)
        client = _client
    return reply_of(client.post(endpoints.openrouter() + DECISIONS, json=payload,
                                headers={"Authorization": f"Bearer {key}"}))


def reply_of(response: httpx2.Response) -> Reply:
    """A response as a `Reply`: a body that is not a JSON object is an
    empty one, and `Retry-After` is read only as seconds."""
    try:
        body = response.json()
    except ValueError:
        body = {"error": {"message": response.text[:200]}}
    try:
        retry_after = float(response.headers.get("retry-after", ""))
    except ValueError:
        retry_after = None
    return Reply(response.status_code, body if isinstance(body, dict) else {}, retry_after)
