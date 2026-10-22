"""The news, summarised: what happened on the topics the user follows, and what
their newsletters said.

A briefing used to list headlines -- "In the news:" and a title per story --
and the user asked for what actually happened instead, on request as well, and
for the key points of the Substack newsletters the user subscribes to.

Two things this has to get right:

- **The articles are found, not followed.** A Google News link is a
  JavaScript redirect with nothing to read (checked 25 September 2026), so
  each story is found again by its headline and outlet (`tools/web`
  search_and_read) and that page is read. Where nothing is found, the
  summary works from the headline and says less.
- **Newsletters are the one received mail whose text reaches a model**, and
  only from senders on `NEWSLETTERS`: public mass mail the user chose to receive.
  Their own received mail stays headers-only everywhere else, and the list is
  pinned by a test. Articles and newsletters alike reach the model as the
  injection screen allows (`_screened`).

Summaries are cached by the sources they were made from, so a briefing
written again does not pay for the same summary twice,
and a newsletter is in one briefing, not every briefing that day.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass

from agent.cut import cut
from core import db, spend

log = logging.getLogger("attention")

# Sender domains whose mail is a newsletter, and nothing else. Public mass
# mail the user subscribed to; adding a domain here widens what received mail a
# model reads, so it is a decision, not a convenience.
NEWSLETTERS = ("substack.com",)
# Subscription housekeeping, not posts.
NOT_A_POST = re.compile(
    r"(?i)confirm|verif|verification code|welcome to|your subscription|password|sign[ -]?in"
)
MAX_STORIES = 6
MAX_NEWSLETTERS = 4
ARTICLE_CHARS = 2500
NEWSLETTER_CHARS = 3500
MAX_LINES = 12

BRIEF = """You write the news part of the user's {kind}. They follow: {topics}.

From the articles and newsletters below, and nothing else, write:
- for each story that matters, one or two sentences on what actually happened
  and why it matters, with the outlet in parentheses. Group related stories;
- for each newsletter, its two or three most useful points, the newsletter's
  name first.

Leave out anything trivial. Where an article's text is missing, work from its
headline and say less. Plain lines, no headers or markdown, at most
{max_lines} lines."""


@dataclass
class Source:
    kind: str  # "article" or "newsletter"
    title: str
    outlet: str
    url: str = ""
    text: str = ""
    key: str = ""  # what it is known by, for the cache and for "already said"

    def block(self) -> str:
        head = f"[{self.kind}] {self.title} -- {self.outlet}"
        return f"{head}\n{self.text.strip() or '(headline only)'}"


def _sender_domain(sender: str) -> str:
    address = sender[sender.rfind("<") + 1:].rstrip(">") if "<" in sender else sender
    return address.rsplit("@", 1)[-1].strip().lower()


def is_newsletter(message: dict) -> bool:
    """A post from an allowed newsletter sender, not subscription housekeeping."""
    domain = _sender_domain(str(message.get("from") or ""))
    allowed = any(domain == d or domain.endswith("." + d) for d in NEWSLETTERS)
    return allowed and not NOT_A_POST.search(str(message.get("subject") or ""))


def newsletters(conn: sqlite3.Connection, days: int = 2, limit: int = 10) -> list[Source]:
    """Recent newsletter posts in every mail account, with their text, each
    known by its slot id (`letter:<id>`). Blocking. An account that cannot
    be read is passed over: the headlines still go.

    Until 14 October 2026 only Gmail's were read, by Gmail's own ids."""
    import datetime as dt

    from agent import connectors
    from steve_sdk.errors import ConnectorError

    query = "(" + " OR ".join(f"from:{d}" for d in NEWSLETTERS) + ")"
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    out = []
    for found in connectors.accounts(conn, "mail"):
        try:
            listed = connectors.blocking(
                found, lambda p: p.search(query, limit=limit, since=since), limit_s=None)
        except ConnectorError as exc:
            log.info("no newsletters from %s: %s", found.name, exc)
            continue
        for summary in listed:
            # Checked on the headers before the body is read, whatever the
            # query matched: the allowlist is the rule, the query only a
            # filter, and an account may drop what it cannot search by.
            if not is_newsletter({"from": summary.sender, "subject": summary.subject}):
                continue
            try:
                full = connectors.blocking(
                    found, lambda p, n=summary.native_id: p.fetch(n), limit_s=None)
            except ConnectorError as exc:
                log.info("could not read a newsletter in %s: %s", found.name, exc)
                continue
            if full is None or not is_newsletter({"from": full.sender,
                                                  "subject": full.subject}):
                continue
            name = full.sender.split("<")[0].strip().strip('"') or "a newsletter"
            out.append(Source("newsletter", full.subject, name,
                              text=cut(full.body or "", NEWSLETTER_CHARS, sentence=True),
                              key=f"letter:{found.id(summary.native_id)}"))
    return out


async def read_stories(stories: list[dict]) -> list[Source]:
    """Each story's article, found by its headline. `stories`: title, outlet, key."""
    from tools import web

    async def one(story: dict) -> Source:
        title, outlet = story["title"], story.get("outlet") or ""
        try:
            pages = await web.search_and_read(f"{title} {outlet}".strip(), count=1,
                                              max_chars=ARTICLE_CHARS)
        except Exception as exc:  # noqa: BLE001 - the headline still says something
            log.info("could not find %r: %s", title[:60], exc)
            pages = []
        page = next((p for p in pages if p.text), None)
        return Source("article", title, outlet, url=page.url if page else "",
                      text=page.text if page else "", key=story.get("key") or title)

    return list(await asyncio.gather(*(one(s) for s in stories[:MAX_STORIES])))


_summaries: dict[str, str] = {}


def _cache_key(keys: list[str]) -> str:
    return hashlib.sha256("|".join(sorted(keys)).encode()).hexdigest()


async def summarize(conn: sqlite3.Connection, sources: list[Source], *, kind: str,
                    topics: list[str]) -> str:
    """The summary, from these sources only. Cached by the sources."""
    if not sources:
        return ""
    key = _cache_key([s.key for s in sources])
    if key in _summaries:
        return _summaries[key]
    system = BRIEF.format(kind=kind, topics=", ".join(topics) or "the news", max_lines=MAX_LINES)
    with spend.spending("background", part="work", conn=conn):
        if spend.may_spend(conn):
            return ""
        blocks = await asyncio.gather(*(_screened(conn, s) for s in sources))
        written, _ = await _write(system, "\n\n".join(blocks))
    lines = [line.rstrip() for line in written.splitlines() if line.strip()]
    text = "\n".join(lines[:MAX_LINES])
    _summaries[key] = text
    return text


async def _screened(conn: sqlite3.Connection, source: Source) -> str:
    """A source as the summary's model reads it: through the injection
    screen (agent/screening.py) -- an article as a web page, a newsletter as
    the received mail it is. Neither was screened before 11 October 2026."""
    from agent import screening

    seen = await screening.shown(conn, "newsletter" if source.kind == "newsletter" else
                                 "web_page", source.url or source.key, source.outlet,
                                 source.block())
    return source.block() if seen.how == "text" else f"[{source.kind}] {seen.text}"


async def _write(system: str, prompt: str):
    """The one model call here. The tests replace it."""
    from agent.llm import ask_once
    from agent.models import Role

    output, record = await ask_once(Role.CONVERSE, system, prompt)
    return str(output or ""), record


def _unused(conn: sqlite3.Connection, letters: list[Source], briefing: str) -> list[Source]:
    """Newsletters no other briefing has carried. The same briefing, written
    again, keeps the ones it already has."""
    said = {r["key"] for r in conn.execute(
        "SELECT key FROM newsletters_said WHERE briefing <> ?", (briefing,))}
    return [s for s in letters if s.key not in said]


def mark_said(conn: sqlite3.Connection, sources: list[Source], briefing: str) -> None:
    """The newsletters a briefing carries, so the next one does not repeat them."""
    with db.transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO newsletters_said (key, briefing, said_at)"
            " VALUES (?, ?, datetime('now'))",
            [(s.key, briefing) for s in sources if s.kind == "newsletter"],
        )


async def for_briefing(conn: sqlite3.Connection, stories: list[dict], *, kind: str,
                       topics: list[str], briefing: str) -> str:
    """The briefing's news, summarised; empty if there is nothing to say or
    the summary could not be made. `briefing` is the briefing's key
    (`briefing.key_for`), so its newsletters are its own."""
    try:
        from agent import offloop

        letters = _unused(conn, await offloop.with_own_conn(conn, newsletters, 1), briefing)
    except Exception as exc:  # noqa: BLE001 - the headlines still go
        log.warning("could not read newsletters: %s", exc)
        letters = []
    letters = letters[:MAX_NEWSLETTERS]
    stories = stories[:MAX_STORIES]
    # Before any article is searched for: a briefing written again, with
    # the same stories, costs nothing the second time.
    key = _cache_key([s.get("key") or s["title"] for s in stories] + [s.key for s in letters])
    if key in _summaries:
        return _summaries[key]
    try:
        text = await summarize(conn, await read_stories(stories) + letters, kind=kind,
                               topics=topics)
    except Exception as exc:  # noqa: BLE001 - the briefing goes with headlines
        log.warning("no news summary for the %s: %s", kind, exc)
        return ""
    if text:
        _summaries[key] = text
        mark_said(conn, letters, briefing)
    return text


async def on_request(conn: sqlite3.Connection, topic: str = "") -> dict:
    """The news now: on one topic, or on everything the user follows, with their
    newsletters. For "what's the news on X?"."""
    from agent import offloop
    from agent.attention import fetch, interests
    from agent.attention.sources import news

    topics = [topic] if topic.strip() else [i.topic for i in interests.active(conn)]
    queries = [topic] if topic.strip() else [i.query for i in interests.active(conn)]
    stories: list[dict] = []
    for query in queries:
        url, params = news.feed(query)
        try:
            found = news.parse(await asyncio.to_thread(fetch.get_text, url, params))
        except Exception as exc:  # noqa: BLE001 - one feed is not the news
            log.info("feed %r failed: %s", query, exc)
            continue
        per = 3 if topic.strip() else 1
        stories += [{"title": s.title, "outlet": s.outlet, "key": news.key_for(s.title)}
                    for s in found[:per]]
    letters: list[Source] = []
    if not topic.strip():
        try:
            # On a connection of its own: `to_thread(newsletters, 2)` handed
            # it 2 as the database, which raised, was logged, and left every
            # answer to "what's the news?" without the user's newsletters.
            letters = await offloop.with_own_conn(conn, newsletters, 2)
        except Exception as exc:  # noqa: BLE001 - the news without the newsletters
            log.info("could not read newsletters: %s", exc)
    sources = await read_stories(stories) + letters[:MAX_NEWSLETTERS]
    text = await summarize(conn, sources, kind="news summary", topics=topics)
    return {
        "summary": text or "(no summary: nothing found, or today's writing budget is spent)",
        "sources": [{"title": s.title, "outlet": s.outlet, "url": s.url} for s in sources],
    }
