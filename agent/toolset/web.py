"""Searching and reading the web.

Where a page may be read from is `core/egress.py`'s, not this module's:
every fetch passes its network rules (`tools/web.fetch`), and in a run
nobody is watching -- the doer, a background job, a worker, the night --
`read_page` reads only a URL the run was shown: a search result, or one the user
wrote. What search returns is recorded as it comes back (`Deps.seen_urls`).
So does a conversation they are in, once it has read outside text (`unshown`).
A site that refuses a fetch but has a saved login of theirs says to open it in
the browser instead (`_with_hint`): on 7 October 2026 LinkedIn refused three
reads with HTTP 999, and the model was told to look for it elsewhere. What
a page or a search result says reaches the model as the injection screen
allows (agent/screening.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlsplit

from pydantic_ai import RunContext

from agent import screening
from agent.actions import act, executable
from agent.turn import Deps
from core import egress
from core.call_policy import is_autonomous
from core.websearch import SearchUnavailable
from tools import web

log = logging.getLogger("toolset.web")


def unattended(deps: Deps) -> bool:
    """A run nobody is watching: a worker, or any channel that is not one of
    theirs (`is_autonomous` is an allowlist of theirs, so a new channel counts)."""
    return deps.worker or is_autonomous(deps.channel)


def unshown(deps: Deps, url: str) -> str | None:
    """Why `read_page` may not fetch `url` here, or None.

    A run nobody watches reads only what it was shown. So does a
    conversation they are in once it has read an email, a page, a calendar
    entry or anything else from outside (`screening.Reading`): a model the
    text fooled could open an address that carries what it read. Its
    shown addresses (`loop.Conversation.reading`) also count the ones that
    came whole in what it read, and last the conversation; past a few on
    one site, those wait for the user's yes (`screening.spelled`)."""
    if unattended(deps):
        return egress.unseen(url, deps.seen_urls)
    if not screening.has_read():
        return None
    if egress.unseen(url, deps.seen_urls):
        return ("this conversation has read text from outside, so it opens only addresses "
                "it was shown: in what it read, a search, or the user's own words -- read "
                "the item again, or search for it")
    if (where := screening.spelled(url)) and not screening.allowed(deps.session_id, where):
        return (f"this conversation has opened {screening.WHOLE_PER_HOST} addresses on {where} "
                f"that came in what it read, and opened one after another such links can "
                f"spell out what it read -- to open more there, ask the user with "
                f"allow_links(site={where!r})")
    return None


def _host(url: str | None) -> str:
    try:
        return urlsplit(url or "").hostname or ""
    except ValueError:
        return ""


def unavailable(exc: SearchUnavailable) -> dict:
    """What a search with no answer says: that it is unavailable, and why.
    The turn goes on without it; every engine set up has been asked."""
    return {"error": f"web search is unavailable: {exc}", "kind": "unavailable"}


def register(tool) -> None:
    """Attach the web tools. `tool` is `register_tools`'s filter."""

    @tool
    async def web_search(ctx: RunContext[Deps], query: str, count: int = 5) -> list[dict] | dict:
        """Search the web. Returns titles, URLs and snippets."""
        try:
            hits = await web.search(query, count=count)
        except SearchUnavailable as exc:
            return unavailable(exc)
        ctx.deps.seen_urls.update(h.url for h in hits if h.url)
        # Titles, snippets and addresses are the sites' own words, screened
        # as one item. An address went whole, unscreened, until step 11's
        # audit: a page's path can carry an instruction as well as its
        # title can. When the screen does not pass them, the model has each
        # result's host alone, and the chat counts as having read outside
        # text (`screening.note`).
        seen = await screening.shown(
            ctx.deps.conn, "search_results", query, "web search",
            "\n\n".join(f"{h.title or ''}\n{h.url or ''}\n{h.description or ''}"
                        for h in hits))
        if seen.how != "text":
            return [*({"host": _host(h.url)} for h in hits),
                    {"screened": seen.how, "text": seen.text}]
        return [h.as_dict() for h in hits]

    @tool
    async def allow_links(ctx: RunContext[Deps], site: str) -> dict:
        """Ask the user to let this conversation open more addresses on
        `site` that came in what it read, when opening one says it has
        opened as many there as it may without asking."""
        where = site.strip().lower()
        preview = (f"Open more of the links to {where} that came in what this conversation "
                   f"read. Opened one after another, such links can spell out what it "
                   f"read.\n(This conversation, {screening.ALLOWED_S // 60} minutes.)")
        return await act(ctx, "allow_links", {"session": ctx.deps.session_id, "site": where},
                         preview)

    @tool
    async def read_page(ctx: RunContext[Deps], url: str) -> dict:
        """Fetch a URL and extract its readable text."""
        if why := unshown(ctx.deps, url):
            log.warning("not reading %s: the run was not shown it", url[:120])
            return {"url": url, "title": None, "text": "", "truncated": False,
                    "error": f"not fetched: {why}"}
        if why := await _linkedin_skip(ctx.deps)(url):
            return {"url": url, "title": None, "text": "", "truncated": False, "error": why}
        if why := web.refused_before(ctx.deps.refused, url):
            if hint := await _login_hint(ctx.deps, url):
                why = f"{why.rsplit('; ', 1)[0]}; {hint}"
            return {"url": url, "title": None, "text": "", "truncated": False, "error": why}
        page = await web.fetch(url)
        web.note_refusal(ctx.deps.refused, page)
        return await _with_hint(ctx.deps, await screening.page(ctx.deps.conn, page))

    @tool
    async def search_and_read(ctx: RunContext[Deps], query: str,
                              count: int = 3) -> list[dict] | dict:
        """Search the web and read the top results in one step."""
        try:
            pages = await web.search_and_read(query, count=count, refused=ctx.deps.refused,
                                              skip=_linkedin_skip(ctx.deps))
        except SearchUnavailable as exc:
            return unavailable(exc)
        ctx.deps.seen_urls.update(p.url for p in pages if p.url)
        pages = await asyncio.gather(*(screening.page(ctx.deps.conn, p) for p in pages))
        return [await _with_hint(ctx.deps, p) for p in pages]


    @tool
    async def research(ctx: RunContext[Deps], question: str) -> dict:
        """Research a question that needs several sources, in one step: a few
        searches planned and read at once, and one answer written from them,
        each claim numbered to its source.

        For "what are my options", "how does X work", "compare", "what's the
        latest on" -- instead of a string of web_search and read_page calls.
        Answer from what it returns and keep its sources as links. Not for one
        quick fact (web_search) or a page the user gave you (read_page).
        """
        from agent import research as researching
        from agent.loop import _now_line

        said = await researching.run(question, when=_now_line(), refused=ctx.deps.refused,
                                     conn=ctx.deps.conn)
        ctx.deps.seen_urls.update(page.url for page in said.pop("pages"))
        return said


async def _login_hint(deps: Deps, url: str) -> str:
    """Where to read a site that refused us instead, if the user saved a login
    for it (agent/toolset/browser.login_hint); imported here, not at the
    top, because the browser's tools import this module."""
    from agent.toolset import browser

    return await browser.login_hint(deps, url)


def _linkedin_skip(deps: Deps):
    """What says a LinkedIn address is not fetched here
    (agent/toolset/browser.linkedin_skip): while the user has a saved login for it
    the LinkedIn tools read it, paced, and a plain fetch from this laptop's
    own address only gets HTTP 999 and counts against the IP. Imported here,
    not at the top, because the browser's tools import this module."""
    from agent.toolset import browser

    return browser.linkedin_skip(deps)


async def _with_hint(deps: Deps, page: web.PageContent) -> dict:
    """A page as the model reads it; one its site refused (`web.REFUSING`),
    on a site the user has a saved login for, says to open it in the browser."""
    said = page.as_dict()
    refused = re.fullmatch(r"HTTP (\d{3})", page.error or "")
    if (refused and int(refused.group(1)) in web.REFUSING
            and (hint := await _login_hint(deps, page.url))):
        said["error"] = f"{page.error} -- {hint}"
    return said


@executable("allow_links")
async def _do_allow_links(*, session: int, site: str) -> dict:
    """Their yes to this conversation opening more of a site's addresses
    that came in what it read, for half an hour."""
    screening.allow(session, site)
    return {"ok": True, "site": site, "minutes": screening.ALLOWED_S // 60}
