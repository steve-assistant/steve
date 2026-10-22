"""Web search and page reading.

Search is OpenAI's, or Brave's where it has a key, asked apart from the
conversation and billed under the caps (core/websearch.py); the pages are
read here, by our own fetcher.
Fetching locally rather than taking a search's summary keeps the raw text
inspectable -- a summary gives the agent nothing to quote from and nothing
for a later reflection pass to audit -- and keeps every page behind
`core/egress` and the injection screen.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import httpcore2
import httpx2
import trafilatura

from core import egress, identity, language, websearch

log = logging.getLogger("web")

# Pages larger than this are almost always a download, a dump, or a trap.
MAX_PAGE_BYTES = 4_000_000
# Ten seconds, not twenty: the slowest of forty pages that loaded on 5
# October 2026 took 1.6 s, and the sites that time out (amtrak.com,
# tailspin.example) hold a connection open and never answer. Pages in one
# `search_and_read` are read together, so one of those held the whole call
# -- and the turn -- for the full timeout, 37 times that week.
DEFAULT_FETCH_TIMEOUT_S = 10.0
FETCH_CONNECT_TIMEOUT_S = 5.0

# A browser-ish UA. Many sites 403 an obviously scripted client, and a page we
# cannot read is worse for the user than a slightly impolite header.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# And what a browser sends with it. A Chrome User-Agent arriving with none
# of the headers Chrome sends beside it is the plainest sign of a script: on
# 5 October 2026, 359 of the week's 689 page reads had failed, 172 of them
# 403s -- peabody.contoso.example, w3.org and stackoverflow among them. Tried
# against the 78 sites that had failed, the UA alone opened 17; with these
# headers, 29. (No Accept-Encoding: the client offers what it can decode.)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",  # the user's, per request (`_headers_for`)
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="141", "Google Chrome";v="141", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# Except for Wikimedia, which 403s exactly that header. Found on 1 October
# 2026: every Wikipedia page answered "Please respect our robot policy". Its
# User-Agent policy asks automated clients to say what they are and how to
# reach whoever runs them, and its edge refuses a browser UA from a client
# that plainly is not a browser. Dressing it up further was tried and does
# not help: Accept and Accept-Language beside the Chrome UA still got a 403,
# the library's default UA got "Please set a user-agent", and this one got a
# 200 from every project tried, the API included. The contact is the
# project's public address once it has one (core/identity.py); the user's own
# address never travels in a header.
WIKIMEDIA_USER_AGENT = (
    identity.user_agent("personal assistant, one user")
    + f" httpx2/{httpx2.__version__}"
)

# Every Wikimedia project, matched as the domain itself or any subdomain, so
# en.m.wikipedia.org matches and wikipedia.org.example.net does not.
_WIKIMEDIA_DOMAINS = (
    "wikipedia.org", "wikimedia.org", "wiktionary.org", "wikiquote.org",
    "wikibooks.org", "wikisource.org", "wikinews.org", "wikiversity.org",
    "wikivoyage.org", "wikidata.org", "wikifunctions.org", "mediawiki.org",
    "w.wiki",
)


# Answers that mean "not to you", as against "not here": a site that gives
# one tends to give it to every page. On 30 September northwind.example answered 403
# to all fourteen pages a job's workers asked for, and LinkedIn 999 -- its
# own refusal -- to every profile. The maintainer, 1 October: skip a site only after
# it has refused at least twice. Per task, never stored: a site may let us
# in tomorrow.
REFUSING = frozenset({401, 403, 429, 451, 999})
SKIP_AFTER_REFUSALS = 2
_STATUS = re.compile(r"^HTTP (\d{3})$")


def _media_type(header: str) -> str:
    """A Content-Type header's type and subtype, or "unknown". A page not
    read is not screened (`agent/screening.page`), so what its error says
    is made of words the code chose; until step 11 it quoted the header
    whole, parameters and all, which the site writes as it likes."""
    found = re.match(r"[\w.+-]{1,40}/[\w.+-]{1,100}", header.strip(), re.ASCII)
    return found.group(0).lower() if found else "unknown"


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").rstrip(".").lower().removeprefix("www.")


def refused_before(refused: Counter, url: str) -> str | None:
    """Why `url` is not worth fetching in this task -- its site has refused
    us `SKIP_AFTER_REFUSALS` times already -- or None."""
    host = _host(url)
    if host and refused[host] >= SKIP_AFTER_REFUSALS:
        return (f"not fetched: {host} has refused {refused[host]} fetches in this task "
                "already; look for it elsewhere")
    return None


def note_refusal(refused: Counter, page: PageContent) -> None:
    """Count it if this fetch was refused, against its site."""
    match = _STATUS.match(page.error or "")
    if match and int(match.group(1)) in REFUSING and (host := _host(page.url)):
        refused[host] += 1


def _is_wikimedia(url: str) -> bool:
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    return any(host == domain or host.endswith("." + domain) for domain in _WIKIMEDIA_DOMAINS)


# Who a fetch says it is, tried in order while the site refuses: a browser's
# whole request, then the bare UA, then plainly what it is. Measured on 5
# October 2026 against sites that had refused: the honest UA opened four
# more of the 78 (sec.gov and finra.org ask automated clients to say so),
# and the bare UA is the one LinkedIn's public profiles answer -- the only
# site of 40 that the full headers lost. One identity at a time, and only
# for a refusal that names us: a 429 asks us to slow down, not to come
# back as someone else.
IDENTITIES = ("browser", "bare", "honest")
RETRY_AS_ANOTHER = frozenset({403, 999})


def _headers_for(url: str, identity: str) -> dict[str, str]:
    """The headers for one request. Per hop rather than per client: a
    redirect can leave Wikimedia or arrive at it, and w.wiki short links do
    both -- and Wikimedia is always sent the UA its policy asks for
    (`WIKIMEDIA_USER_AGENT`), whoever is asking."""
    if identity == "honest" or _is_wikimedia(url):
        return {"User-Agent": WIKIMEDIA_USER_AGENT}
    if identity == "bare":
        return {"User-Agent": USER_AGENT}
    # In the user's language (`core/language`): it was en-US for everyone,
    # so a site that has a French page gave a French user its English one.
    return {**BROWSER_HEADERS, "Accept-Language": language.accept_language()}


@dataclass
class SearchResult:
    title: str
    url: str
    description: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageContent:
    url: str
    # Defaults, so a page that was not read is one line: its url and why.
    title: str | None = None
    text: str = ""
    truncated: bool = False
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


async def search(query: str, count: int = 5) -> list[SearchResult]:
    """Search the web: up to `count` pages, each with a line on what it says.

    Raises `websearch.SearchUnavailable`, saying why, when the search has no
    answer, and `spend.Refused` at a spending cap (core/websearch.py)."""
    return [SearchResult(found.title, found.url, found.description)
            for found in await websearch.search(query, count)]


class _NotVetted(httpcore2.ConnectError):
    """`egress` refused the address a name resolved to, in its own words,
    which `fetch` passes on; another connection error is named alone."""


class _Vetted(httpcore2.AnyIOBackend):
    """Dials an address `egress.addresses` vetted, never the name.

    `egress.refusal` resolved a page's name and vetted it, and then the
    client resolved it again to connect. So a name whose DNS answered
    somewhere public for the check, and this computer or the LAN for the
    connection, reached either on any port, and the model read the answer
    as the page (step 11's audit). Here the name is resolved once for each
    connection, and the address vetted is the one dialled, as the
    browser's gate does. TLS still checks the URL's name."""

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        found = await asyncio.to_thread(egress.addresses, host, port)
        if isinstance(found, str):
            raise _NotVetted(f"not fetched: {found}")
        return await super().connect_tcp(found[0], port, timeout, local_address,
                                         socket_options)


def _transport() -> httpx2.AsyncHTTPTransport:
    # httpx2 takes no network backend, so its pool is swapped for one that
    # takes `_Vetted`. Given a transport, the client also ignores a proxy
    # named in the environment, which would resolve the name itself.
    context = httpx2.create_ssl_context()
    transport = httpx2.AsyncHTTPTransport(verify=context)
    transport._pool = httpcore2.AsyncConnectionPool(ssl_context=context,
                                                    network_backend=_Vetted())
    return transport


async def _follow(client, url: str, identity: str):
    """GET `url` as `identity`, following redirects by hand: the response it
    lands on, or a PageContent saying why it stopped."""
    current = url
    for _hop in range(egress.MAX_REDIRECTS + 1):
        if why := await asyncio.to_thread(egress.refusal, current):
            return PageContent(url=current, error=f"not fetched: {why}")
        response = await client.get(current, headers=_headers_for(current, identity))
        location = response.headers.get("location")
        if not (response.is_redirect and location):
            return response
        current = str(response.url.join(location))
    return PageContent(url=current, error=f"more than {egress.MAX_REDIRECTS} redirects")


# Below this much text, a page's main content was probably missed rather
# than absent, and a looser pass is worth its time.
THIN_PAGE_CHARS = 200


def _extract(html: str) -> tuple[str | None, str | None]:
    """A page's readable text, and its title.

    The precise pass first, which keeps menus and footers out. When it
    finds nothing, the recall pass, and then at least the page's own
    description of itself: 28 of the week's page reads to 5 October 2026
    came back "no extractable content", and a looser pass found the text of
    press releases and park pages among them, while sites that build their
    pages in script (Facebook, Virginia's tourism board) still describe
    themselves in their metadata. Something true about a page is more use
    to the model than an error.
    """
    kept = {"include_comments": False, "include_tables": True}
    text = trafilatura.extract(html, favor_precision=True, **kept)
    meta = trafilatura.extract_metadata(html)
    title = getattr(meta, "title", None) if meta else None
    if text and len(text) >= THIN_PAGE_CHARS:
        return text, title
    loose = trafilatura.extract(html, favor_recall=True, **kept)
    text = max((text or "", loose or ""), key=len)
    if len(text) < THIN_PAGE_CHARS and (summary := getattr(meta, "description", None)):
        text = f"{text}\n\n[The page's own summary of itself] {summary}".strip()
    return text or None, title


async def fetch(url: str, *, max_chars: int = 20_000) -> PageContent:
    """Fetch a URL and extract its readable text.

    Never raises: a failed fetch is a normal outcome the model should see and
    reason about, not an exception that aborts a whole turn.
    """
    if not url.lower().startswith(("http://", "https://")):
        return PageContent(url=url, error="only http(s) URLs are supported")

    try:
        async with httpx2.AsyncClient(
            transport=_transport(),
            timeout=httpx2.Timeout(DEFAULT_FETCH_TIMEOUT_S, connect=FETCH_CONNECT_TIMEOUT_S),
            # By hand, so every hop passes `core/egress.refusal`: a public
            # page that redirects to the supervisor's port or the router is
            # a request to it, and the client would have followed.
            follow_redirects=False,
        ) as client:
            for identity in IDENTITIES:
                got = await _follow(client, url, identity)
                if isinstance(got, PageContent):  # not ours to fetch, or a loop
                    return got
                response = got
                if (response.status_code not in RETRY_AS_ANOTHER
                        or _is_wikimedia(str(response.url))):  # it has one identity
                    break
            # Not `raise_for_status`, which passes anything above 599 -- and
            # LinkedIn refuses with 999, which was read as a page.
            if response.status_code >= 400:
                return PageContent(url=url, error=f"HTTP {response.status_code}")

            if len(response.content) > MAX_PAGE_BYTES:
                return PageContent(url=str(response.url),
                                   error=f"page exceeds {MAX_PAGE_BYTES} bytes")

            content_type = response.headers.get("content-type", "")
            if "html" not in content_type and "xml" not in content_type:
                # Plain text, JSON, markdown: no extraction needed.
                if content_type.startswith("text/") or "json" in content_type:
                    body = response.text
                    return PageContent(url=str(response.url), text=body[:max_chars],
                                       truncated=len(body) > max_chars)
                return PageContent(url=str(response.url),
                                   error=f"unsupported content-type: {_media_type(content_type)}")

            html = response.text
            final_url = str(response.url)

    except httpx2.HTTPError as exc:
        if isinstance(exc.__cause__, _NotVetted):
            return PageContent(url=url, error=str(exc.__cause__))
        # Its name alone: the message can quote what the server sent (h11
        # quotes an illegal status or header line whole).
        log.info("fetching from %s failed: %s: %s", _host(url), type(exc).__name__, exc)
        return PageContent(url=url, error=type(exc).__name__)

    # trafilatura is CPU-bound parsing; keep it off the event loop so a big
    # page cannot stall a concurrent voice turn.
    extracted, title = await asyncio.to_thread(_extract, html)

    if not extracted:
        return PageContent(url=final_url, title=title, error="no extractable content")

    return PageContent(
        url=final_url,
        title=title,
        text=extracted[:max_chars],
        truncated=len(extracted) > max_chars,
    )


async def search_and_read(
    query: str, *, count: int = 3, max_chars: int = 8_000, refused: Counter | None = None,
    skip: Callable[[str], Awaitable[str | None]] | None = None,
) -> list[PageContent]:
    """Search, then read the top hits concurrently.

    The common shape of a real question: the snippets are rarely enough, and
    doing this in one call saves a model round trip per page. `refused` is
    the task's count of sites that turned us away (`refused_before`): a hit
    on one that has refused twice is not fetched, and each refusal counts.
    `skip` says why a hit is not to be fetched at all, or None -- LinkedIn,
    which the caller reads another way (agent/toolset/browser.linkedin_skip).
    """
    hits = await search(query, count=count)

    async def read(hit: SearchResult) -> PageContent:
        if skip is not None and (why := await skip(hit.url)):
            return PageContent(url=hit.url, error=why)
        if refused is not None and (why := refused_before(refused, hit.url)):
            return PageContent(url=hit.url, error=why)
        return await fetch(hit.url, max_chars=max_chars)

    pages = await asyncio.gather(*(read(hit) for hit in hits), return_exceptions=False)
    for page, hit in zip(pages, hits, strict=True):
        if not page.title:
            page.title = hit.title
        if refused is not None:
            note_refusal(refused, page)
    return list(pages)
