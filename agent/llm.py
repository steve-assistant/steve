"""Calling models, and riding out the failures that clear by themselves.

A rate limit or a 502 is gone in seconds. A turn that gave up on the first
one would fail the user for nothing; one that retried an invalid key would
make them wait twenty seconds for the same error. So `run_agent` retries
what is transient, with jittered backoff, and lets the rest through at once,
and every call leaves a `CallRecord` for the ledger.

Until 12 October 2026 models were also reached through OpenRouter, which
reports an upstream failure as HTTP 200 with the error in the body; a hook
here turned those into typed errors. Models are OpenAI's alone now, whose
status codes are honest, so the hook went with it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx2
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import UserContent

from agent.models import MODELS, Role
from core import spend

# Upstream statuses worth trying again: timeouts, conflicts, rate limits,
# server errors, and the edge's own (52x), none of them a mistake of ours.
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524}

# A rate limit clears in seconds, so patience is worth more than failing fast.
log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
BASE_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0


def plain_http_client() -> httpx2.AsyncClient:
    """The models' HTTP client.

    httpx2 rather than httpx: pydantic-ai deprecated httpx clients for
    OpenAI-compatible providers and drops them in v3.
    """
    return httpx2.AsyncClient(timeout=httpx2.Timeout(300.0, connect=10.0),
                              transport=_PoolPerLoop())


class _PoolPerLoop(httpx2.AsyncBaseTransport):
    """A connection pool for each event loop that uses the client.

    The process has one models' client (`agent/provider._openai_provider`),
    and more than one loop uses it: the worker's own, and each
    `asyncio.run` in a module build's thread (`agent/modbuild.build` runs
    the pipeline and admission each under its own). A connection belongs
    to the loop that opened it. With one pool, a later loop took up a
    connection whose loop had closed: in the module-building eval of 15
    October the code reviewer failed so in two builds of four, and a
    failed review is a no on the card. A closed loop's pool is dropped,
    not closed: closing needs the loop it belonged to.
    """

    def __init__(self) -> None:
        self._pools: dict[asyncio.AbstractEventLoop, httpx2.AsyncHTTPTransport] = {}
        self._lock = threading.Lock()

    def _pool(self) -> httpx2.AsyncHTTPTransport:
        loop = asyncio.get_running_loop()
        with self._lock:
            for gone in [other for other in self._pools if other.is_closed()]:
                del self._pools[gone]
            if (pool := self._pools.get(loop)) is None:
                pool = self._pools[loop] = httpx2.AsyncHTTPTransport()
            return pool

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._pool().handle_async_request(request)

    async def aclose(self) -> None:
        with self._lock:
            pool = self._pools.pop(asyncio.get_running_loop(), None)
        if pool is not None:
            await pool.aclose()


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ModelHTTPError):
        # A spent balance comes as a 429 too, and waiting will not fill it.
        return exc.status_code in RETRYABLE_STATUS and not spend.out_of_credit(exc)
    if isinstance(exc, httpx2.TimeoutException | httpx2.TransportError):
        return True
    if isinstance(exc, ModelAPIError):
        # The connection dropped, or never opened: pydantic-ai's word for the
        # SDK's connection error. The SDK tries a request again itself, but
        # not a stream it has already handed over, so a reply cut off
        # mid-stream ended the turn at once ("Something broke on my side"),
        # found by step 10's hostile scenarios (tests/e2e/test_hostile_model.py).
        return True
    # A malformed response is usually an upstream hiccup (a truncated body,
    # a proxy's error page). Worth trying again.
    return isinstance(exc, UnexpectedModelBehavior)


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter, so concurrent retries spread out."""
    ceiling = min(BASE_BACKOFF_S * (2**attempt), MAX_BACKOFF_S)
    return random.uniform(0, ceiling)


@dataclass
class CallRecord:
    """What one model call cost and how long it took.

    Cost is computed from our own price table (agent/models.py): OpenAI
    reports a call's tokens, not what they cost.
    """

    role: Role
    slug: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: int
    attempts: int
    # How many requests the run made, and how many tools it called. A turn
    # that called 25 tools resent every result to the model 25 times; the
    # token totals said it was heavy, not why (context redesign, 10 October
    # 2026: kept in `turn_stats`).
    requests: int = 0
    tool_calls: int = 0


def record_for(role: Role, usage: Any, latency_ms: int, attempts: int) -> CallRecord:
    spec = MODELS[role]
    details = getattr(usage, "details", None) or {}
    return CallRecord(
        role=role,
        slug=spec.slug,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cached_tokens=getattr(usage, "cache_read_tokens", 0) or 0,
        reasoning_tokens=int(details.get("reasoning_tokens", 0) or 0),
        cost_usd=spec.cost_usd(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cached_tokens=getattr(usage, "cache_read_tokens", 0) or 0,
        ),
        latency_ms=latency_ms,
        attempts=attempts,
        requests=int(getattr(usage, "requests", 0) or 0),
        tool_calls=int(getattr(usage, "tool_calls", 0) or 0),
    )


async def run_agent(
    agent: Agent,
    prompt: str | Sequence[UserContent],
    role: Role,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    on_retry: Callable[[], None] | None = None,
    **run_kwargs: Any,
) -> tuple[Any, CallRecord]:
    """Run an agent, retrying transient upstream failures.

    Returns the pydantic-ai result alongside a CallRecord for the ledger.
    Non-retryable errors propagate immediately -- an invalid API key should
    fail on the first attempt, not four attempts and twenty seconds later.

    `on_retry` is called as a retry is about to start: the run begins again
    from its first word, so anything that was listening to the stream (a
    voice speaking it) must know it is hearing the start a second time.
    """
    started = time.perf_counter()
    last: BaseException | None = None

    for attempt in range(max_attempts):
        try:
            result = await agent.run(prompt, **run_kwargs)
        except Exception as exc:
            last = exc
            if not is_retryable(exc) or attempt == max_attempts - 1:
                raise
            delay = backoff_delay(attempt)
            # Said, because a silent retry is a turn that was slow for no
            # reason anyone can find afterwards.
            log.warning("%s call failed after %.0fs (%s: %s); retry %d of %d in %.1fs",
                        role.value, time.perf_counter() - started, type(exc).__name__,
                        str(exc)[:160], attempt + 1, max_attempts - 1, delay)
            if on_retry is not None:
                on_retry()
            await asyncio.sleep(delay)
            continue

        latency_ms = int((time.perf_counter() - started) * 1000)
        return result, record_for(role, result.usage, latency_ms, attempt + 1)

    raise last  # unreachable; the loop either returns or raises


async def ask_once(role: Role, system: str, prompt: str, *, output_type: Any = str,
                   retries: int = 1) -> tuple[Any, CallRecord]:
    """One question to `role`'s model, with no tools and no history: the
    answer (an `output_type`) and the call's bill, retried as `run_agent`
    retries an upstream failure. `retries` is pydantic-ai's own, for an
    answer that does not fit `output_type`; its default is one.

    A dozen places built this agent by hand, each with the same four
    imports, naming the role twice -- once for the model, once for its
    settings -- where nothing stopped the two from disagreeing."""
    # Here, not at the top: agent.provider imports this module.
    from agent.provider import model_for, settings_for
    from core import language

    # The user's language with every question: the briefings, summaries and
    # notes written here came out in English whatever the user's was.
    agent = Agent(model_for(role), output_type=output_type, system_prompt=system,
                  instructions=language.instruction,
                  model_settings=settings_for(MODELS[role]), retries=retries)
    result, record = await run_agent(agent, prompt, role)
    return result.output, record
