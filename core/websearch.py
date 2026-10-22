"""Web search, asked apart from the conversation: OpenAI's hosted search
tool, or Brave's search API where the user has given it a key.

`tools/web.search` asks this for a query's results, each a title, an address
and a line about the page, and the toolset screens them as the sites' own
words. A search carries the query and nothing else: no conversation, no
memory and no location, so a place is searched for only where the query
names it.

OpenAI's search is the default, on the key every model already uses: one
Responses request with its search tool, whose results are the pages the
answer cites, then the other pages it returned. It is slow for a search:
over 20 queries on 12 October 2026 its median was 5.0 seconds, where Brave
answers in under one. So a user who gives Brave a key (`BRAVE_API_KEY`) is
searched there, and OpenAI is asked when Brave has no answer -- its free
tier's month used up, say -- rather than the search failing.

Why apart, and not OpenAI's tool handed to the conversation's model: what
the search found would reach that model unscreened, and the pages it read
would pass neither `core/egress` nor `seen_urls`, while a run nobody is
watching may read only addresses it was shown. Here the search sees the web
and nothing of the user's, and the pages are read on this machine, behind
both.

Why in core/, frozen: every search has a fee. It is asked about under the
caps before it is made and billed after, in its caller's unit of work, and
a fee in code the night may change could be lowered to buy headroom.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from core import endpoints, language, spend
from core.models_guard import PRICED, usd

log = logging.getLogger("websearch")

# The BULK role's model, with no reasoning and the least of each page put
# before it: the search is a lookup, and its answer only says what it found.
# Measured over the same 20 queries on 12 October 2026, low effort took 5.8
# seconds at the median and none 5.0, with as many pages readable.
MODEL = "gpt-6-luna"
EFFORT = "none"
CONTEXT = "low"
# Dollars a search, besides its tokens: $10.00 per 1,000 calls, OpenAI's
# price for the tool on 12 October 2026. One call a request (`max_tool_calls`).
FEE_USD = 0.01
TIMEOUT_S = 20.0

BRAVE_KEY = "BRAVE_API_KEY"
# Under Brave's API (`core/endpoints.brave`).
BRAVE_SEARCH = "/res/v1/web/search"
# $5.00 per 1,000 searches, Brave's paid price. A free tier's searches are
# billed it too: the cap errs on the side of spending less.
BRAVE_FEE_USD = 0.005
BRAVE_MODEL = "brave-search"
# It answers in under a second, so a slow answer is a failure, and OpenAI is
# asked in its place.
BRAVE_TIMEOUT_S = 5.0
# Its free tier answers one request a second and refuses the rest (429), so
# searches are spaced: research asks four at once.
BRAVE_EVERY_S = 1.05
# The countries Brave searches by, as its documentation listed them in 2025
# (its pages no longer print the table). A region outside them is searched
# from Brave's default, the US, as every search was, rather than sent and
# refused: a refusal would put each search on OpenAI, five times slower.
BRAVE_COUNTRIES = frozenset({
    "AR", "AT", "AU", "BE", "BR", "CA", "CH", "CL", "CN", "DE", "DK", "ES", "FI", "FR", "GB",
    "HK", "ID", "IN", "IT", "JP", "KR", "MX", "MY", "NL", "NO", "NZ", "PH", "PL", "PT", "RU",
    "SA", "SE", "TR", "TW", "US", "ZA",
})
_brave_next = 0.0
_clock = time.monotonic
_pause = asyncio.sleep

MOST = 10
DESCRIPTION_CHARS = 300

# A list's mark at the start of a line: "- ", "* ", "1. ".
_MARK = re.compile(r"^(?:[-*•]|\d+[.)])\s+")
# Brave marks the query's words in its snippets: "<strong>ramen</strong>".
_TAG = re.compile(r"<[^>]+>")

INSTRUCTIONS = (
    "Search the web once for the query. List the {count} most relevant pages, one line"
    " each: what the page says that answers the query, citing the page. No other text.")


class SearchUnavailable(Exception):
    """No answer from any search -- an outage, a timeout, a refusal, a
    balance spent, no key. Its message says why, for the model to pass on."""


@dataclass(frozen=True)
class Found:
    title: str
    url: str
    description: str


async def search(query: str, count: int = 5) -> list[Found]:
    """Up to `count` pages for `query`: Brave's where it has a key, and
    otherwise, or when Brave has no answer, OpenAI's. Raises `spend.Refused`
    at a cap, as every model call does, and `SearchUnavailable` when neither
    answers."""
    count = max(1, min(count, MOST))
    unit = spend.current()
    await spend.allowed(unit)
    brave = ""
    if key := _brave_key():
        try:
            return await _by_brave(unit, query, count, key)
        except Exception as exc:  # noqa: BLE001 - OpenAI is asked in its place
            brave = _brave_why(exc)
            log.warning("Brave had no answer (%s); asking OpenAI", brave)
    started = time.perf_counter()
    try:
        answer = await _call(request_for(query, count))
    except Exception as exc:
        await spend.if_spent(unit, exc)
        why = _short(spend.openai_why(exc, TIMEOUT_S))
        raise SearchUnavailable(f"{brave}, and {why}" if brave else why) from exc
    await spend.billed(unit, cost(answer), **_used(answer, started))
    return read(answer, count)


# --- OpenAI -------------------------------------------------------------------

def request_for(query: str, count: int) -> dict:
    """The whole request: the query and the user's country (`core/language`),
    and nothing of the user's besides. With no country, a search for "the
    election" or "bank holidays" was answered for the US, wherever they were."""
    return {
        "model": MODEL,
        "instructions": INSTRUCTIONS.format(count=count),
        "input": query,
        "tools": [{"type": "web_search", "search_context_size": CONTEXT,
                   "user_location": {"type": "approximate", "country": language.region()}}],
        "tool_choice": "required",
        "max_tool_calls": 1,
        "include": ["web_search_call.action.sources"],
        "reasoning": {"effort": EFFORT},
        "store": False,
        "max_output_tokens": 2_000,
    }


async def _call(request: dict) -> dict:
    """The one request, its answer as JSON. `tests/gate/fences.py` refuses
    it."""
    import openai

    from core import secrets

    async with openai.AsyncOpenAI(api_key=secrets.require("OPENAI_API_KEY"),
                                  base_url=endpoints.openai(), timeout=TIMEOUT_S,
                                  max_retries=1) as client:
        response = await client.responses.create(**request)
    return response.model_dump(mode="json")


def searches(answer: dict) -> int:
    """The searches an answer is charged the fee for: as OpenAI reports
    them (`tool_usage`) or as its output shows them, whichever is more. One
    search may run several queries and is charged once. At least one: the
    request requires it, and a fee billed for nothing errs on the cap's side."""
    reported = ((answer.get("tool_usage") or {}).get("web_search") or {}).get("num_requests")
    made = sum(item.get("type") == "web_search_call" for item in answer.get("output") or [])
    return max(1, int(reported or 0), made)


def cost(answer: dict) -> float:
    """Dollars for one answer: its tokens at the model's frozen price, and
    the fee for each search."""
    used = _tokens(answer)
    return (usd(PRICED[f"openai/{MODEL}"].prices, input_tokens=used["input_tokens"],
                cached_tokens=used["cached_tokens"], output_tokens=used["output_tokens"])
            + searches(answer) * FEE_USD)


def _tokens(answer: dict) -> dict[str, int]:
    usage = answer.get("usage") or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_tokens": int((usage.get("input_tokens_details") or {}).get("cached_tokens")
                             or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int((usage.get("output_tokens_details") or {})
                                .get("reasoning_tokens") or 0),
    }


def _used(answer: dict, started: float) -> dict:
    return {"model": MODEL, "effort": EFFORT, **_tokens(answer),
            "latency_ms": int((time.perf_counter() - started) * 1000)}


def read(answer: dict, count: int) -> list[Found]:
    """The pages an answer cites, in its order, each with the line that
    cited it; then the other pages its search returned, to make up `count`.
    Each address once, without the tag OpenAI adds to it."""
    found: dict[str, Found] = {}
    returned: list[str] = []
    for item in answer.get("output") or []:
        if item.get("type") == "web_search_call":
            returned += [s.get("url") or "" for s in
                         (item.get("action") or {}).get("sources") or []]
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            text = part.get("text") or ""
            notes = [n for n in part.get("annotations") or []
                     if n.get("type") == "url_citation"]
            spans = sorted((int(n.get("start_index") or 0), int(n.get("end_index") or 0))
                           for n in notes)
            for note in notes:
                url = untagged(note.get("url") or "")
                if _web(url) and url not in found:
                    found[url] = Found(note.get("title") or _host(url), url,
                                       _cited(text, spans, int(note.get("start_index") or 0)))
    for url in map(untagged, returned):
        if _web(url) and url not in found:
            found[url] = Found(_host(url), url, "")
    return list(found.values())[:count]


def untagged(url: str) -> str:
    """`url` without the `utm_source=openai` OpenAI adds to every address it
    cites: the page's own address, as the user would share it."""
    head, mark, rest = url.partition("?")
    if not mark:
        return url
    query, hash_, fragment = rest.partition("#")
    kept = [p for p in query.split("&") if p and p != "utm_source=openai"]
    return head + ("?" + "&".join(kept) if kept else "") + hash_ + fragment


def _cited(text: str, spans: list[tuple[int, int]], at: int) -> str:
    """The line of the answer holding the citation at `at`, without the
    citations themselves or the list's marks."""
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    end = len(text) if end < 0 else end
    kept, upto = [], start
    for left, right in spans:
        if right <= start or left >= end:
            continue
        kept.append(text[upto:max(left, upto)])
        upto = max(upto, right)
    kept.append(text[upto:end])
    return _short(_MARK.sub("", " ".join("".join(kept).replace("**", "").split())))


# --- Brave --------------------------------------------------------------------

def _brave_key() -> str | None:
    from core import secrets

    return secrets.get(BRAVE_KEY)


def brave_params(query: str, count: int) -> dict:
    """Brave's request: the query, how many, and the user's country where
    Brave searches by it."""
    params: dict = {"q": query, "count": count}
    if (region := language.region()) in BRAVE_COUNTRIES:
        params["country"] = region
    return params


async def _by_brave(unit: spend.Unit | None, query: str, count: int, key: str) -> list[Found]:
    await _brave_turn()
    started = time.perf_counter()
    payload = await _brave_get(brave_params(query, count), key)
    await spend.billed(unit, BRAVE_FEE_USD, model=BRAVE_MODEL,
                       latency_ms=int((time.perf_counter() - started) * 1000))
    return read_brave(payload, count)


async def _brave_turn() -> None:
    """Wait for this search's turn, `BRAVE_EVERY_S` after the last one's.
    Each caller takes the next turn before it waits, so searches asked at
    once go a second apart, in order, with no lock to share between loops."""
    global _brave_next
    now = _clock()
    turn = max(now, _brave_next)
    _brave_next = turn + BRAVE_EVERY_S
    await _pause(turn - now)


async def _brave_get(params: dict, key: str) -> dict:
    """Brave's one request (`brave_params`). `tests/gate/fences.py`
    refuses it."""
    import httpx2

    async with httpx2.AsyncClient(timeout=BRAVE_TIMEOUT_S) as client:
        response = await client.get(endpoints.brave() + BRAVE_SEARCH, params=params,
                                    headers={"Accept": "application/json",
                                             "X-Subscription-Token": key})
        response.raise_for_status()
        return response.json()


def read_brave(payload: dict, count: int) -> list[Found]:
    """Brave's web results, in its order, each address once, with the marks
    in its snippets taken out."""
    found: dict[str, Found] = {}
    for item in (payload.get("web") or {}).get("results") or []:
        url = item.get("url") or ""
        if _web(url) and url not in found:
            found[url] = Found(_plain(item.get("title") or "") or _host(url), url,
                               _plain(item.get("description") or ""))
    return list(found.values())[:count]


def _brave_why(exc: BaseException) -> str:
    import httpx2

    if isinstance(exc, httpx2.TimeoutException):
        return f"Brave gave no answer within {BRAVE_TIMEOUT_S:.0f} seconds"
    if isinstance(exc, httpx2.HTTPStatusError):
        return f"Brave answered {exc.response.status_code}"
    if isinstance(exc, httpx2.TransportError):
        return "Brave could not be reached"
    return _short(f"Brave: {type(exc).__name__}: {exc}")


# --- Both ---------------------------------------------------------------------

def _web(url: str) -> bool:
    return url.startswith(("https://", "http://"))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or url).removeprefix("www.")


def _plain(text: str) -> str:
    return _short(html.unescape(_TAG.sub("", text)))


def _short(text: str) -> str:
    line = " ".join(text.split())
    if len(line) <= DESCRIPTION_CHARS:
        return line
    return line[:DESCRIPTION_CHARS - 1].rstrip() + "…"
