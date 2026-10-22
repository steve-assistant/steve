"""A fake OpenAI, on this computer.

Tests never reach OpenAI: the fences refuse every socket off this computer
(tests/gate/fences.py). A test that needs OpenAI's answers through the code
that really asks starts this, and names it to that code in
`STEVE_OPENAI_URL` (core/endpoints.py), which only a source tree honours.
Setup's key check was its first user, the exit walk's turns its second;
the end-to-end tests run a whole worker against it.

Keys: `GOOD` is accepted, `BROKE` (and any in `broke`) is an account with
no credit left, and
any other is refused, each with the status and body the real service sends.
`FENCED`, what the fences' keychain gives a worker the real app starts, and
a world's own `keys` (tests/fakes/world.py) are accepted too.

Both of OpenAI's ways to converse are served, as each model's role asks
(agent/provider.py): Chat Completions for the quick roles, Responses for
reasoning, web search and the screen's extraction. Each is answered whole,
or streamed when asked, in the shapes OpenAI's own SDK parses (pinned by
the spike in docs/plan/spikes/s10-e2e.md).

What is said comes from a script (`Rule`): "when a request looks like this,
answer with these words, or this tool call". The first rule that matches
answers. With none:
- a web search finds the world's `pages` that hold the query's words, the
  most words first, and nothing when none do;
- a request for structured output gets the smallest answer its schema
  takes, so a role that asks for one gets an answer it can read;
- anything else gets `otherwise`.
The default script is the exit walk's: asked for something every Monday,
with Steve's `create_routine` tool offered, it sets up a routine that
emails, which needs the user's yes, the walk's first card. Anything else
gets `REPLY`.

Every request is kept, its headers and body (`requests`): a canary that
reached a model is found there.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from tests.fakes.served import served

GOOD = "sk-proj-a-tests-good-key"
BROKE = "sk-proj-a-tests-key-with-no-credit"
# tests/gate/childfence/sitecustomize.py `dummy_or_unset`: what a started
# worker's keychain holds for OPENAI_API_KEY.
FENCED = "test-openai_api_key"

REPLY = "Glad to meet you, Maya. Ask me anything, whenever it suits you."
# Asked for this, with the tool offered, the fake sets up a routine.
ROUTINE_ASKED = "every monday"
ROUTINE = {"title": "The week ahead", "instruction": "Write Maya a short plan for the week.",
           "at_time": "08:00", "days": ["mon"], "email_to": ["maya.lindqvist@example.com"]}
ROUTINE_SET = "Set up. It starts once you say yes on the card."
USAGE = {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}
RESPONSES_USAGE = {"input_tokens": 40, "input_tokens_details": {"cached_tokens": 0},
                   "output_tokens": 12, "output_tokens_details": {"reasoning_tokens": 0},
                   "total_tokens": 52}
NOTHING_FOUND = "The search found nothing on that."
NL = chr(10)

# How a `Rule` may fail, each as the real service, or what stands in front
# of it, was seen to.
FAILS = ("cut", "500", "429", "garbage")
SERVER_ERROR = {"error": {
    "message": "The server had an error while processing your request. Sorry about that!",
    "type": "server_error", "param": None, "code": None}}
RATE_LIMITED = {"error": {
    "message": "Rate limit reached for gpt-5-mini in organization org-fake on requests per min"
               " (RPM): Limit 500, Used 500, Requested 1. Please try again in 1s.",
    "type": "requests", "param": None, "code": "rate_limit_exceeded"}}
# A captive portal's page, which a hotel's network answers every request with.
PORTAL = ("<html><head><title>Welcome</title></head><body><h1>Sign in to the network"
          "</h1><form><input name=room></form></body></html>")


class Cut(Exception):
    """Raised partway through an answer's body, so the server closes the
    connection before the body ends, as a dropped connection does."""


@dataclass(frozen=True)
class Rule:
    """When a request looks like this, answer so. Each condition given must
    hold; one not given holds always.

    - `said`: words in the user's message, when the request ends with one;
    - `result`: words in a tool's result, when the request ends with one
      ("" for any result);
    - `after`: the tool whose result the request ends with;
    - `offered`: a tool the request offers;
    - `endpoint`: "chat" or "responses".

    The answer is `say`, words, or `call`, a tool call with `arguments`:
    given, or made from the tool's result as a model reads an id from it
    (a function of the result's text).

    Or it fails, as OpenAI and what stands in front of it do (`fail`, one of
    `FAILS`): "cut", the connection closes partway through `say`, streamed
    or not; "500", OpenAI's own error; "429", a rate limit that says when to
    try again; "garbage", a 200 whose body is a proxy's page, not JSON.
    `times`: how many requests the rule answers before it stops matching,
    so a failure can pass and the try after it be answered."""

    said: str | None = None
    result: str | None = None
    after: str | None = None
    offered: str | None = None
    endpoint: str | None = None
    say: str | None = None
    call: str | None = None
    arguments: dict | Callable[[str], dict] = field(default_factory=dict)
    fail: str | None = None
    times: int | None = None

    def __post_init__(self) -> None:
        if self.fail is not None and self.fail not in FAILS:
            raise ValueError(f"no such failure: {self.fail}")

    def matches(self, asked: Asked) -> bool:
        return ((self.said is None
                 or (asked.last == "user" and self.said.lower() in asked.text.lower()))
                and (self.result is None
                     or (asked.last == "tool" and self.result.lower() in asked.text.lower()))
                and (self.after is None or (asked.last == "tool" and asked.tool == self.after))
                and (self.offered is None or self.offered in asked.offered)
                and (self.endpoint is None or self.endpoint == asked.endpoint))


# The exit walk's script (tests/unit/ui/test_exit_walk.py).
WALK = (Rule(said=ROUTINE_ASKED, offered="create_routine", call="create_routine",
             arguments=ROUTINE),
        Rule(result="", say=ROUTINE_SET))


@dataclass(frozen=True)
class Page:
    """A page of the fake web: what a search finds."""

    url: str
    title: str
    text: str


@dataclass(frozen=True)
class Asked:
    """A conversation's request, whichever endpoint it came by: how it
    ends, the tools it offers, and the structured answer it wants."""

    endpoint: str
    last: str  # "user", "tool", or "" for anything else
    text: str
    offered: frozenset[str]
    schema: dict | None = None
    search: bool = False
    # pydantic-ai asks for structured output by a tool, `final_result`:
    # its name and its parameters' schema.
    output: tuple[str, dict] | None = None
    # The tool whose result the request ends with, by the call it answers.
    tool: str = ""


def _output_tool(tools: dict[str, dict]) -> tuple[str, dict] | None:
    return next(((name, schema) for name, schema in tools.items()
                 if name and name.startswith("final_result")), None)


@dataclass(frozen=True)
class Kept:
    """One request as it arrived."""

    path: str
    headers: dict[str, str]
    body: Any


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(part.get("text", "") for part in content if isinstance(part, dict))


def _vector(text: str, size: int) -> list[float]:
    """The same text, the same vector, of unit length."""
    seed = hashlib.sha256(text.encode()).digest()
    raw = [((seed[i % len(seed)] + i) % 251) - 125.0 for i in range(size)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def smallest(schema: dict, root: dict | None = None) -> Any:
    """The smallest value `schema` takes: a model's structured answer when no
    rule gives one. Required fields only, empty lists, the first choice."""
    root = schema if root is None else root
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        found = (root.get("$defs") or {}).get(name) or (root.get("definitions") or {})[name]
        return smallest(found, root)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    for key in ("anyOf", "oneOf", "allOf"):
        if schema.get(key):
            options = schema[key]
            if any(option.get("type") == "null" for option in options):
                return None
            return smallest(options[0], root)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = "null" if "null" in kind else kind[0]
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        return {name: smallest(properties[name], root)
                for name in schema.get("required") or [] if name in properties}
    if kind == "array":
        return [smallest(schema.get("items") or {}, root)
                for _ in range(schema.get("minItems", 0))]
    if kind == "string":
        if schema.get("format") == "date-time":
            return "2026-01-01T00:00:00Z"
        if schema.get("format") == "date":
            return "2026-01-01"
        return "x" * schema.get("minLength", 0)
    if kind in ("integer", "number"):
        low = schema.get("minimum")
        if low is None and "exclusiveMinimum" in schema:
            low = schema["exclusiveMinimum"] + 1
        value = max(low or 0, 0)
        if "maximum" in schema:
            value = min(value, schema["maximum"])
        return int(value) if kind == "integer" else float(value)
    if kind == "boolean":
        return False
    return None


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


class FakeOpenAI:
    def __init__(self, rules: Sequence[Rule] = WALK, *, otherwise: str = REPLY,
                 pages: Sequence[Page] = (), keys: Iterable[str] = ()) -> None:
        self.rules = list(rules)
        # How many requests each rule has answered (`Rule.times`), by the
        # rule itself: a test may insert one ahead of the rest mid-run.
        self.used: dict[int, int] = {}
        self.otherwise = otherwise
        self.pages = list(pages)
        # Accepted besides `GOOD` and `FENCED`: a world's own (tests/fakes/world.py).
        self.keys = {GOOD, FENCED, *keys}
        # Accounts with no credit left: a world's key moved here (from
        # `keys`) runs its account dry mid-run.
        self.broke = {BROKE}
        # Every request, as it arrived.
        self.requests: list[Kept] = []
        # Each request's path and the key it carried.
        self.asked: list[tuple[str, str]] = []
        # Each Chat Completions conversation's body, as sent.
        self.conversations: list[dict] = []
        # Each tool's result as the model was handed it: (tool, text).
        self.results: list[tuple[str, str]] = []
        self.app = Starlette(routes=[
            Route("/v1/models", self.models),
            Route("/v1/chat/completions", self.chat, methods=["POST"]),
            Route("/v1/responses", self.responses, methods=["POST"]),
            Route("/v1/embeddings", self.embeddings, methods=["POST"]),
        ])

    async def _take(self, request: Request) -> tuple[str, Any]:
        """The key the request carried, and its body; both kept."""
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        self.requests.append(Kept(request.url.path, dict(request.headers), body))
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.asked.append((request.url.path, key))
        return key, body

    def _refused(self, key: str) -> JSONResponse:
        if key in self.broke:
            return JSONResponse({"error": {
                "message": "You exceeded your current quota, please check your plan and"
                           " billing details.",
                "type": "insufficient_quota", "param": None, "code": "insufficient_quota"}},
                status_code=429)
        return JSONResponse({"error": {
            "message": "Incorrect API key provided. You can find your API key at"
                       " https://platform.openai.com/account/api-keys.",
            "type": "invalid_request_error", "param": None, "code": "invalid_api_key"}},
            status_code=401)

    async def models(self, request: Request) -> JSONResponse:
        key, _ = await self._take(request)
        if key in self.keys:
            return JSONResponse({"object": "list", "data": [
                {"id": "gpt-5-mini", "object": "model", "created": 0, "owned_by": "openai"}]})
        return self._refused(key)

    # --- what to say ---------------------------------------------------------

    def _decide(self, asked: Asked) -> dict:
        """`{"say": words}`, `{"call": name, "arguments": {...}}` or
        `{"search": query}`; with `"fail"` beside it, how to fail instead."""
        if asked.last == "tool":
            self.results.append((asked.tool, asked.text))
        for rule in self.rules:
            if rule.times is not None and self.used.get(id(rule), 0) >= rule.times:
                continue
            if rule.matches(asked):
                self.used[id(rule)] = self.used.get(id(rule), 0) + 1
                if rule.fail is not None:
                    return {"fail": rule.fail, "say": rule.say or self.otherwise}
                if rule.call is not None:
                    given = rule.arguments
                    return {"call": rule.call,
                            "arguments": given(asked.text) if callable(given) else given}
                return {"say": rule.say or ""}
        if asked.search:
            return {"search": asked.text}
        if asked.schema is not None:
            return {"say": json.dumps(smallest(asked.schema))}
        if asked.output is not None:
            name, schema = asked.output
            return {"call": name, "arguments": smallest(schema)}
        return {"say": self.otherwise}

    def _found(self, query: str) -> list[Page]:
        wanted = _words(query)
        scored = [(len(wanted & _words(f"{page.title} {page.text}")), i, page)
                  for i, page in enumerate(self.pages)]
        return [page for score, _, page in sorted(scored, key=lambda s: (-s[0], s[1]))
                if score > 0]

    # --- Chat Completions ----------------------------------------------------

    @staticmethod
    def _asked_chat(body: dict) -> Asked:
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        tools = {t.get("function", {}).get("name"): t.get("function", {}).get("parameters") or {}
                 for t in body.get("tools") or []}
        role = last.get("role")
        format_ = body.get("response_format") or {}
        schema = ((format_.get("json_schema") or {}).get("schema")
                  if format_.get("type") == "json_schema" else None)
        answered = next((call["function"].get("name", "")
                         for message in messages for call in message.get("tool_calls") or []
                         if role == "tool" and call.get("id") == last.get("tool_call_id")), "")
        return Asked("chat", role if role in ("user", "tool") else "", _text(last.get("content")),
                     frozenset(tools), schema, output=_output_tool(tools), tool=answered)

    async def chat(self, request: Request):
        key, body = await self._take(request)
        if key not in self.keys:
            return self._refused(key)
        self.conversations.append(body)
        answer = self._decide(self._asked_chat(body))
        n = len(self.requests)
        if "call" in answer:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{n}", "type": "function",
                "function": {"name": answer["call"],
                             "arguments": json.dumps(answer["arguments"])}}]}
        else:
            message = {"role": "assistant", "content": answer.get("say", self.otherwise)}
        streamed = None
        if body.get("stream"):
            usage = (body.get("stream_options") or {}).get("include_usage")
            streamed = self._streamed(body["model"], message, usage)
        whole = {
            "id": f"chatcmpl-{n}", "object": "chat.completion", "created": int(time.time()),
            "model": body["model"],
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}],
            "usage": USAGE}
        if "fail" in answer:
            return _failing(answer["fail"], streamed, whole)
        if streamed is not None:
            return StreamingResponse(streamed, media_type="text/event-stream")
        return JSONResponse(whole)

    @staticmethod
    def _streamed(model: str, message: dict, usage: bool) -> Iterator[str]:
        def chunk(delta: dict, finish: str | None = None, **more) -> str:
            return "data: " + json.dumps({
                "id": "chatcmpl-fake", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                **more}) + NL + NL

        if "tool_calls" in message:
            call = message["tool_calls"][0]
            yield chunk({"role": "assistant", "content": None, "tool_calls": [{
                "index": 0, "id": call["id"], "type": "function",
                "function": {"name": call["function"]["name"], "arguments": ""}}]})
            yield chunk({"tool_calls": [{"index": 0, "function": {
                "arguments": call["function"]["arguments"]}}]})
            yield chunk({}, "tool_calls")
        else:
            words = message["content"].split(" ")
            yield chunk({"role": "assistant", "content": ""})
            for i, word in enumerate(words):
                yield chunk({"content": word if i == 0 else " " + word})
            yield chunk({}, "stop")
        if usage:
            yield "data: " + json.dumps({
                "id": "chatcmpl-fake", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model, "choices": [],
                "usage": USAGE}) + NL + NL
        yield "data: [DONE]" + NL + NL

    # --- Responses -----------------------------------------------------------

    @staticmethod
    def _asked_responses(body: dict) -> Asked:
        given = body.get("input")
        items = [{"role": "user", "content": given}] if isinstance(given, str) else given or []
        last = items[-1] if items else {}
        if last.get("type") == "function_call_output":
            ends, text = "tool", str(last.get("output") or "")
        elif last.get("role") == "user":
            ends, text = "user", _text(last.get("content"))
        else:
            ends, text = "", ""
        tools = body.get("tools") or []
        offered = frozenset(t.get("name") or t.get("type") for t in tools)
        functions = {t.get("name"): t.get("parameters") or {}
                     for t in tools if t.get("type") == "function"}
        format_ = (body.get("text") or {}).get("format") or {}
        schema = format_.get("schema") if format_.get("type") == "json_schema" else None
        answered = next((item.get("name", "") for item in items
                         if ends == "tool" and item.get("type") == "function_call"
                         and item.get("call_id") == last.get("call_id")), "")
        return Asked("responses", ends, text, offered, schema,
                     search=any(t.get("type") == "web_search" for t in tools),
                     output=_output_tool(functions), tool=answered)

    def _output(self, answer: dict, n: int) -> list[dict]:
        if "call" in answer:
            return [{"id": f"fc_{n}", "type": "function_call", "call_id": f"call_{n}",
                     "name": answer["call"], "arguments": json.dumps(answer["arguments"]),
                     "status": "completed"}]
        if "search" not in answer:
            return [_message(answer["say"], n)]
        pages = self._found(answer["search"])
        search = {"id": f"ws_{n}", "type": "web_search_call", "status": "completed",
                  "action": {"type": "search", "query": answer["search"],
                             "sources": [{"type": "url", "url": p.url} for p in pages]}}
        if not pages:
            return [search, _message(NOTHING_FOUND, n)]
        # As OpenAI cites: a marker at the line's end, which the citation
        # spans, and which core/websearch.py `read` takes out of the line.
        lines, notes, at = [], [], 0
        for page in pages:
            said = f"- {page.title}: {page.text} "
            marker = f"([{urlsplit(page.url).hostname}]({page.url}))"
            notes.append({"type": "url_citation", "url": page.url, "title": page.title,
                          "start_index": at + len(said),
                          "end_index": at + len(said) + len(marker)})
            lines.append(said + marker)
            at += len(said) + len(marker) + 1
        message = _message(NL.join(lines), n)
        message["content"][0]["annotations"] = notes
        return [search, message]

    def _response(self, body: dict, output: list[dict], n: int) -> dict:
        return {"id": f"resp_{n}", "object": "response", "created_at": int(time.time()),
                "model": body.get("model", ""), "status": "completed", "output": output,
                "parallel_tool_calls": True, "tool_choice": body.get("tool_choice", "auto"),
                "tools": body.get("tools") or [], "usage": RESPONSES_USAGE, "error": None,
                "incomplete_details": None}

    async def responses(self, request: Request):
        key, body = await self._take(request)
        if key not in self.keys:
            return self._refused(key)
        n = len(self.requests)
        answer = self._decide(self._asked_responses(body))
        output = self._output(answer, n)
        streamed = self._streamed_response(body, output, n) if body.get("stream") else None
        if "fail" in answer:
            return _failing(answer["fail"], streamed, self._response(body, output, n))
        if streamed is not None:
            return StreamingResponse(streamed, media_type="text/event-stream")
        return JSONResponse(self._response(body, output, n))

    def _streamed_response(self, body: dict, output: list[dict], n: int) -> Iterator[str]:
        number = iter(range(1_000_000))

        def event(kind: str, **fields) -> str:
            data = {"type": kind, "sequence_number": next(number), **fields}
            return f"event: {kind}{NL}data: {json.dumps(data)}{NL}{NL}"

        started = self._response(body, [], n) | {"status": "in_progress", "usage": None}
        yield event("response.created", response=started)
        for i, item in enumerate(output):
            if item["type"] == "message":
                text = item["content"][0]["text"]
                yield event("response.output_item.added", output_index=i,
                            item=item | {"status": "in_progress", "content": []})
                yield event("response.content_part.added", output_index=i, item_id=item["id"],
                            content_index=0,
                            part={"type": "output_text", "text": "", "annotations": []})
                for j, word in enumerate(text.split(" ")):
                    yield event("response.output_text.delta", output_index=i,
                                item_id=item["id"], content_index=0,
                                delta=word if j == 0 else " " + word, logprobs=[])
                yield event("response.output_text.done", output_index=i, item_id=item["id"],
                            content_index=0, text=text, logprobs=[])
            elif item["type"] == "function_call":
                yield event("response.output_item.added", output_index=i,
                            item=item | {"arguments": "", "status": "in_progress"})
                yield event("response.function_call_arguments.delta", output_index=i,
                            item_id=item["id"], delta=item["arguments"])
            else:
                yield event("response.output_item.added", output_index=i,
                            item=item | {"status": "in_progress"})
            yield event("response.output_item.done", output_index=i, item=item)
        yield event("response.completed", response=self._response(body, output, n))

    # --- the rest ------------------------------------------------------------

    async def embeddings(self, request: Request) -> JSONResponse:
        key, body = await self._take(request)
        if key not in self.keys:
            return self._refused(key)
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        size = body.get("dimensions") or 1536
        return JSONResponse({
            "object": "list", "model": body["model"],
            "data": [{"object": "embedding", "index": i, "embedding": _vector(text, size)}
                     for i, text in enumerate(texts)],
            "usage": {"prompt_tokens": 8 * len(texts), "total_tokens": 8 * len(texts)}})

    @contextlib.contextmanager
    def running(self) -> Iterator[str]:
        """Serves for the length of the block on a port of the system's
        choosing; gives the address OpenAI's clients take, ending `/v1`."""
        with served(self.app, "the fake OpenAI") as port:
            yield f"http://127.0.0.1:{port}/v1"


def _message(text: str, n: int) -> dict:
    return {"id": f"msg_{n}", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def _failing(kind: str, streamed: Iterator[str] | None, whole: dict) -> Response:
    """The failure a `Rule` names, in place of its answer."""
    if kind == "500":
        return JSONResponse(SERVER_ERROR, status_code=500)
    if kind == "429":
        return JSONResponse(RATE_LIMITED, status_code=429, headers={"retry-after": "1"})
    if kind == "garbage":
        return HTMLResponse(PORTAL)
    # Cut: the first half, then the connection closes before the body ends.
    if streamed is not None:
        events = list(streamed)
        first, media = events[:max(1, len(events) // 2)], "text/event-stream"
    else:
        text = json.dumps(whole)
        first, media = [text[:len(text) // 2]], "application/json"

    def cut() -> Iterator[str]:
        yield from first
        raise Cut("the connection drops here")

    return StreamingResponse(cut(), media_type=media)
