"""Jev, and OpenRouter's credit, over HTTP on this computer.

The in-process tests put `tests/gate/decisions.py`'s `Decisions` in place
of `core.decide._post`. A worker in its own process cannot be reached that
way, so this serves the same fake at the paths OpenRouter serves, and the
worker is pointed here by `STEVE_OPENROUTER_URL` (core/endpoints.py). The
answers are scripted as they are there: by question name, a probability
for a predicate, a value or a dict of chances for a choice.

`otherwise` answers every predicate no entry names. Unanswered, a question
falls to the local readers (core/decide.py), which a test's worker has not
got, so a world without it would leave every screening unanswered.

The credit is OpenRouter's too (`agent/attention/sources/health.py`, and
setup's check of a key), so it is served here, not by the fake OpenAI.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable, Iterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.fakes.openai import Kept
from tests.fakes.served import served
from tests.gate.decisions import Decisions

GOOD = "sk-or-v1-a-tests-good-key"
BROKE = "sk-or-v1-a-tests-key-with-no-credit"
# What the fenced keychain holds for OPENROUTER_API_KEY, in a test's own
# process and in a worker it starts (tests/gate/childfence/sitecustomize.py
# `dummy_or_unset`): a key the worker sends without being given one.
FENCED = "test-openrouter_api_key"

REFUSED = {"error": {"message": "No auth credentials found", "code": 401}}
NO_CREDIT = {"error": {"message": "Insufficient credits", "code": 402}}
DOWN = {"error": {"message": "Service temporarily unavailable", "code": 503}}


class FakeJev:
    def __init__(self, *, otherwise: float | None = None, keys: Iterable[str] = (),
                 **answers) -> None:
        self.decisions = Decisions(**answers)
        self.otherwise = otherwise
        # Accepted besides `GOOD` and `FENCED`: a world's own (tests/fakes/world.py).
        self.keys = {GOOD, FENCED, *keys}
        # The hostile modes, set mid-run: each decision waits `slow_s`
        # first, and while `down` is above 0 one is refused as a service
        # that is down refuses, and counts it off.
        self.slow_s = 0.0
        self.down = 0
        # Each request's path and the key it carried; and each whole, for
        # the canaries (tests/fakes/world.py).
        self.asked: list[tuple[str, str]] = []
        self.requests: list[Kept] = []
        self.app = Starlette(routes=[
            Route("/api/alpha/decisions", self.decide, methods=["POST"]),
            Route("/api/v1/credits", self.credits),
        ])

    async def _key(self, request: Request) -> str:
        raw = await request.body()
        self.requests.append(Kept(request.url.path, dict(request.headers),
                                  raw.decode("utf-8", "replace")))
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.asked.append((request.url.path, key))
        return key

    async def decide(self, request: Request) -> JSONResponse:
        key = await self._key(request)
        if self.slow_s:
            await asyncio.sleep(self.slow_s)
        if self.down > 0:
            self.down -= 1
            return JSONResponse(DOWN, status_code=503)
        if key == BROKE:
            return JSONResponse(NO_CREDIT, status_code=402)
        if key not in self.keys:
            return JSONResponse(REFUSED, status_code=401)
        payload = await request.json()
        if self.otherwise is not None:
            for name, question in payload.get("questions", {}).items():
                if question.get("type") == "noul":
                    self.decisions.answers.setdefault(name, self.otherwise)
        reply = self.decisions(payload, key)
        return JSONResponse(reply.body, status_code=reply.status)

    async def credits(self, request: Request) -> JSONResponse:
        key = await self._key(request)
        if key not in self.keys | {BROKE}:
            return JSONResponse(REFUSED, status_code=401)
        used = 10.0 if key == BROKE else 2.5
        return JSONResponse({"data": {"total_credits": 10.0, "total_usage": used}})

    @contextlib.contextmanager
    def running(self) -> Iterator[str]:
        """Serves for the length of the block; gives the address
        `STEVE_OPENROUTER_URL` takes, ending `/api`."""
        with served(self.app, "the fake Jev") as port:
            yield f"http://127.0.0.1:{port}/api"
