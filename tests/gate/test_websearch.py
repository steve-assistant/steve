"""A web search is the query alone, under the caps, and no answer is said so.

`core/websearch.py` asks OpenAI's hosted search tool, apart from the
conversation, or Brave's search API where the user has given it a key.
What it must hold:
- the results are read from the answer OpenAI gives, as recorded
  (tests/gate/fixtures/websearch/captured.json): the pages it cites, each
  with the line that cited it, then the other pages it returned, without
  the tag OpenAI adds to each address. A shape drifted from the real one
  would be no results, forever, and every test of a caller would still pass
  against its fake;
- the request carries the query and nothing else of the user's: no
  conversation, no memory, no location, and storage off;
- it asks the cap before the call, and bills the answer in its caller's
  unit, the per-search fee included, from a price frozen beside the call;
- with no answer it raises `SearchUnavailable` saying why, and a spent
  balance is written down as every client writes it;
- with a Brave key, Brave is asked first, sent the query alone, billed its
  fee, and spaced a second apart for its free tier; OpenAI is asked when
  Brave has no answer;
- and no test can reach either service (tests/gate/fences.py).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import httpx2
import openai
import pytest

from core import endpoints, knobs, spend, websearch

FIXTURES = Path(__file__).parent / "fixtures" / "websearch"
CAPTURED = json.loads((FIXTURES / "captured.json").read_text(encoding="utf-8"))
BRAVE = json.loads((FIXTURES / "brave.json").read_text(encoding="utf-8"))["reply"]
REAL_CALL = websearch._call


@pytest.fixture(autouse=True)
def _no_brave_key(monkeypatch):
    """OpenAI's search, unless a test gives Brave a key: in a test every API
    key reads as set (tests/gate/fences.py)."""
    monkeypatch.setattr(websearch, "_brave_key", lambda: None)


def _answering(answer: dict, sent: list[dict] | None = None):
    async def call(request: dict) -> dict:
        if sent is not None:
            sent.append(request)
        return answer

    return call


def _failing(exc: Exception):
    async def call(request: dict) -> dict:
        raise exc

    return call


def _search(conn, query: str, count: int = 5, *, category: str = "chat", **unit):
    async def run():
        with spend.spending(category, conn=conn, **unit):
            return await websearch.search(query, count)

    return asyncio.run(run())


REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status(status: int, code: str) -> openai.APIStatusError:
    error = openai.RateLimitError if status == 429 else openai.InternalServerError
    return error("no", response=httpx.Response(status, request=REQUEST),
                 body={"message": "no", "type": code, "code": code})


# ---------------------------------------------------------------------------
# The answer, as OpenAI gives it
# ---------------------------------------------------------------------------

def test_the_results_are_the_pages_the_answer_cites_then_those_it_returned():
    answer = CAPTURED["answer"]

    found = websearch.read(answer, 10)

    cited = [n for item in answer["output"] if item["type"] == "message"
             for part in item["content"] for n in part["annotations"]
             if n["type"] == "url_citation"]
    first = websearch.untagged(cited[0]["url"])
    assert found[0].url == first and found[0].title == cited[0]["title"]
    assert found[0].description == CAPTURED["first_description"]
    assert len(found) == 10 and len({f.url for f in found}) == 10
    for page in found:
        assert page.url.startswith("https://") and "utm_source=openai" not in page.url
        assert "](" not in page.description and "**" not in page.description
    # Cited pages first, each with its line; then the ones only returned.
    described = [bool(f.description) for f in found]
    assert described == sorted(described, reverse=True) and described[0]


def test_at_most_the_count_asked_for_is_given():
    assert len(websearch.read(CAPTURED["answer"], 3)) == 3


def test_an_address_keeps_everything_but_the_tag_openai_adds():
    assert websearch.untagged("https://a.example/p?utm_source=openai") == "https://a.example/p"
    assert (websearch.untagged("https://a.example/s?q=Best+Ramen&utm_source=openai&p=2#top")
            == "https://a.example/s?q=Best+Ramen&p=2#top")
    assert (websearch.untagged("https://a.example/s?utm_source=newsletter")
            == "https://a.example/s?utm_source=newsletter")


def test_the_line_given_is_the_one_that_cited_the_page_without_its_marks():
    text = ("- **Hours:** Open 7am to 3pm daily. ([a.example](https://a.example/?utm_source=openai))\n"
            "2. Closed on public holidays. ([b.example](https://b.example/h))")
    first = text.index("([a.")
    second = text.index("([b.")
    answer = {"output": [{"type": "message", "content": [{"type": "output_text", "text": text,
              "annotations": [
                  {"type": "url_citation", "url": "https://a.example/?utm_source=openai",
                   "title": "A", "start_index": first, "end_index": text.index("\n")},
                  {"type": "url_citation", "url": "https://b.example/h", "title": "B",
                   "start_index": second, "end_index": len(text)}]}]}]}

    found = websearch.read(answer, 5)

    assert [(f.title, f.url, f.description) for f in found] == [
        ("A", "https://a.example/", "Hours: Open 7am to 3pm daily."),
        ("B", "https://b.example/h", "Closed on public holidays."),
    ]


# ---------------------------------------------------------------------------
# What is sent
# ---------------------------------------------------------------------------

def test_a_search_carries_the_query_and_country_and_nothing_else(conn, monkeypatch):
    """A search made in a conversation sees none of it: what goes is the
    query, fixed instructions, the one tool, and storage off. The tool is
    told the user's country (`core/language`) and nothing finer: no city,
    area or time zone. Without a country, "bank holidays" was answered for
    the US wherever the user was; a place nearer than that is searched for
    only where the query names it."""
    sent: list[dict] = []
    monkeypatch.setattr(websearch, "_call", _answering(CAPTURED["answer"], sent))

    _search(conn, "how to descale an espresso machine", 4)

    assert sent == [{
        "model": "gpt-6-luna",
        "instructions": "Search the web once for the query. List the 4 most relevant pages,"
                        " one line each: what the page says that answers the query, citing"
                        " the page. No other text.",
        "input": "how to descale an espresso machine",
        "tools": [{"type": "web_search", "search_context_size": "low",
                   "user_location": {"type": "approximate", "country": "US"}}],
        "tool_choice": "required",
        "max_tool_calls": 1,
        "include": ["web_search_call.action.sources"],
        "reasoning": {"effort": "none"},
        "store": False,
        "max_output_tokens": 2_000,
    }]


def test_the_call_sends_the_request_as_given_and_returns_the_answer_as_json(monkeypatch):
    made: list[dict] = []

    class Answer:
        def model_dump(self, mode):
            assert mode == "json"
            return CAPTURED["answer"]

    class Client:
        def __init__(self, **kwargs):
            made.append(kwargs)
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def create(self, **request):
            made.append(request)
            return Answer()

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    monkeypatch.setattr("core.secrets.require", lambda name: f"a test's {name}")
    request = websearch.request_for("q", 5)

    answer = asyncio.run(REAL_CALL(request))

    client, sent = made
    assert answer == CAPTURED["answer"] and sent == request
    assert client["api_key"] == "a test's OPENAI_API_KEY"
    assert client["timeout"] == websearch.TIMEOUT_S


# ---------------------------------------------------------------------------
# What it costs
# ---------------------------------------------------------------------------

def test_a_search_is_billed_in_its_callers_unit_fee_included(conn, monkeypatch):
    """A conversation's search is chat's; a routine's is background work's.
    The fee is most of a search's cost: billed from tokens alone, a search
    would count a tenth of what it costs."""
    monkeypatch.setattr(websearch, "_call", _answering(CAPTURED["answer"]))

    _search(conn, "q")
    _search(conn, "q", category="background", part="routines", job_id=3)

    rows = conn.execute("SELECT category, part, job_id, model, input_tokens, cost_usd"
                        " FROM usage_events ORDER BY id").fetchall()
    usage = CAPTURED["answer"]["usage"]
    cached = usage["input_tokens_details"]["cached_tokens"]
    tokens = ((usage["input_tokens"] - cached) * 0.10 + cached * 0.01
              + usage["output_tokens"] * 0.50) / 1e6
    assert [tuple(r)[:5] for r in rows] == [
        ("chat", None, None, "gpt-6-luna", usage["input_tokens"]),
        ("background", "routines", 3, "gpt-6-luna", usage["input_tokens"]),
    ]
    assert rows[0]["cost_usd"] == pytest.approx(tokens + 0.01)


def test_each_search_an_answer_made_is_billed_the_fee():
    """As OpenAI counts them, or as the answer shows them, whichever is
    more. One search that ran four queries was charged once on 12 October
    2026 (`tool_usage`), and so is billed once."""
    call = {"type": "web_search_call", "status": "completed"}
    assert websearch.searches(CAPTURED["answer"]) == 1
    assert websearch.cost({"output": [call, call]}) == pytest.approx(0.02)
    assert websearch.cost({"output": [call], "tool_usage": {"web_search": {"num_requests": 3}}}
                          ) == pytest.approx(0.03)
    # The request requires one: an answer that shows none is billed one.
    assert websearch.cost({"output": []}) == pytest.approx(0.01)


def test_a_search_past_a_cap_is_refused_before_it_is_made(conn, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(websearch, "_call", _answering(CAPTURED["answer"], sent))
    knobs.write({"EXTENSIONS_DAY_USD": 0})

    with pytest.raises(spend.Refused) as refused:
        _search(conn, "q", category="extensions")

    assert refused.value.cap == "EXTENSIONS_DAY_USD" and sent == []


# ---------------------------------------------------------------------------
# No answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc,why", [
    (openai.APITimeoutError(REQUEST), "OpenAI gave no answer within 20 seconds"),
    (openai.APIConnectionError(request=REQUEST), "OpenAI could not be reached"),
    (_status(500, "server_error"), "OpenAI answered 500"),
    (_status(429, "rate_limit_exceeded"), "OpenAI answered 429"),
], ids=["timeout", "unreachable", "outage", "busy"])
def test_a_search_with_no_answer_says_why_and_bills_nothing(conn, monkeypatch, exc, why):
    monkeypatch.setattr(websearch, "_call", _failing(exc))

    with pytest.raises(websearch.SearchUnavailable, match=f"^{why}$"):
        _search(conn, "q")

    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
    assert spend.notices(conn, caps=[spend.BALANCE]) == []


def test_a_search_with_no_key_says_so(conn, monkeypatch):
    from core.secrets import MissingSecret

    def no_key(name):
        raise MissingSecret(f"{name} is not set")

    monkeypatch.setattr(websearch, "_call", REAL_CALL)
    monkeypatch.setattr("core.secrets.require", no_key)

    with pytest.raises(websearch.SearchUnavailable, match="^no OpenAI key is stored$"):
        _search(conn, "q")


def test_a_search_refused_for_a_spent_balance_is_written_down(conn, monkeypatch):
    """As every client writes it (core/spend.py `Guarded`): with the balance
    spent nothing that needs a model runs, and the user is told so."""
    monkeypatch.setattr(websearch, "_call", _failing(_status(429, "insufficient_quota")))

    with pytest.raises(websearch.SearchUnavailable, match="^OpenAI's balance is used up$"):
        _search(conn, "q")

    [notice] = spend.notices(conn, caps=[spend.BALANCE])
    assert notice.kind == "stopped"


# ---------------------------------------------------------------------------
# Brave, where the user gave it a key
# ---------------------------------------------------------------------------

def _brave_refused(status: int):
    async def get(params: dict, key: str) -> dict:
        request = httpx2.Request("GET", endpoints.BRAVE + websearch.BRAVE_SEARCH)
        raise httpx2.HTTPStatusError("no", request=request,
                                     response=httpx2.Response(status, request=request))

    return get


@pytest.fixture
def brave(monkeypatch):
    """Brave with a key, answering with its documented reply; what it was
    asked, in order. Its turns come at once."""
    asked: list[tuple[dict, str]] = []

    async def get(params: dict, key: str) -> dict:
        asked.append((params, key))
        return BRAVE

    async def pause(seconds: float) -> None:
        pass

    monkeypatch.setattr(websearch, "_brave_key", lambda: "a test's Brave key")
    monkeypatch.setattr(websearch, "_brave_get", get)
    monkeypatch.setattr(websearch, "_pause", pause)
    return asked


def test_with_a_brave_key_the_search_is_braves_and_sends_only_the_query_and_country(
        conn, monkeypatch, brave):
    """Brave answers in under a second where OpenAI's search takes about
    five (12 October 2026), so a user who gave it a key is searched there.
    It is sent the query and the user's country, as OpenAI is."""
    sent: list[dict] = []
    monkeypatch.setattr(websearch, "_call", _answering(CAPTURED["answer"], sent))

    found = _search(conn, "ramen near the station", 4)

    assert brave == [({"q": "ramen near the station", "count": 4, "country": "US"},
                      "a test's Brave key")]
    assert sent == []
    # Its marks and entities taken out; each address once; web pages only.
    assert [(f.title, f.url, f.description) for f in found] == [
        ("Noodle Bar & Kitchen - Ramen by the Station", "https://noodles.example/station",
         "Open late. Our ramen broth simmers for 18 hours & is served until midnight."),
        ("The ten best ramen spots in town", "https://guide.example/best-ramen",
         "From tonkotsu to shoyu: where to eat ramen this autumn."),
        ("map.example", "https://map.example/place/1234", ""),
    ]


def test_a_brave_search_is_billed_its_fee_in_the_callers_unit(conn, brave):
    _search(conn, "q", category="background", part="routines", job_id=3)

    [row] = conn.execute("SELECT category, part, job_id, model, cost_usd FROM usage_events"
                         ).fetchall()
    assert tuple(row) == ("background", "routines", 3, "brave-search", 0.005)


def test_a_brave_search_past_a_cap_is_refused_before_it_is_made(conn, brave):
    knobs.write({"EXTENSIONS_DAY_USD": 0})

    with pytest.raises(spend.Refused):
        _search(conn, "q", category="extensions")

    assert brave == []


def test_when_brave_has_no_answer_openai_is_asked(conn, monkeypatch):
    """Its free tier's month used up, or an outage: the search still has an
    answer, and only the one that answered is billed."""
    monkeypatch.setattr(websearch, "_brave_key", lambda: "a test's Brave key")
    monkeypatch.setattr(websearch, "_brave_get", _brave_refused(429))
    monkeypatch.setattr(websearch, "_call", _answering(CAPTURED["answer"]))

    found = _search(conn, "q")

    assert found == websearch.read(CAPTURED["answer"], 5)
    assert [r[0] for r in conn.execute("SELECT model FROM usage_events")] == ["gpt-6-luna"]


def test_when_neither_answers_both_reasons_are_said(conn, monkeypatch):
    monkeypatch.setattr(websearch, "_brave_key", lambda: "a test's Brave key")
    monkeypatch.setattr(websearch, "_brave_get", _brave_refused(401))
    monkeypatch.setattr(websearch, "_call",
                        _failing(openai.APIConnectionError(request=REQUEST)))

    with pytest.raises(websearch.SearchUnavailable,
                       match="^Brave answered 401, and OpenAI could not be reached$"):
        _search(conn, "q")


def test_brave_searches_asked_at_once_go_a_second_apart(conn, monkeypatch, brave):
    """Its free tier answers one request a second and refuses the rest, and
    research asks four searches at once."""
    waits: list[float] = []

    async def pause(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(websearch, "_pause", pause)
    monkeypatch.setattr(websearch, "_clock", lambda: 100.0)
    monkeypatch.setattr(websearch, "_brave_next", 0.0)

    async def at_once():
        with spend.spending("chat", conn=conn):
            await asyncio.gather(*(websearch.search(q) for q in ("a", "b", "c")))

    asyncio.run(at_once())

    assert sorted(waits) == pytest.approx([0.0, 1.05, 2.10])
    assert len(brave) == 3
