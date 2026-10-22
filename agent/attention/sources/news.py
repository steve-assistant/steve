"""Headlines from the topics the user follows.

Google News RSS: free, keyless, and licensed for exactly this -- a personal,
non-commercial feed reader. The feed shape is pinned by a real payload
captured on 24 September 2026 (tests/unit/fixtures/attention/).

**Almost all of it is never said.** A section feed is sixty-odd headlines a
day, and the judge is told that a story earns a line only if it touches their
plans or is genuinely consequential. The source's job is to hand it each
story once: a story is keyed by its normalised title, so the same wire
report syndicated by five outlets is one observation, and so is a story
that turns up under two topics the user follows.

What is *not* here is news about the firms and people on their calendar. The
judge asks the doer to look those up before the meeting, which is when
that news is worth reading.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace

from agent.attention import fetch, interests
from agent.attention.sources import SourceError
from agent.attention.store import Observation
from core import language

log = logging.getLogger("attention")

SECTION = "https://news.google.com/rss/headlines/section/topic/{section}"
SEARCH = "https://news.google.com/rss/search"

# Headlines taken from each feed per look, newest first.
PER_FEED = 12
# Older than this is not news any more, however new it is to us.
FRESH = dt.timedelta(hours=30)


@dataclass
class Story:
    title: str
    outlet: str
    link: str
    published: dt.datetime | None  # aware


def edition() -> dict:
    """Google News' edition for the user's language and region
    (`core/language`): "US:en" is the American, "FR:fr" the French. It was
    the American, for everyone."""
    tag, region = language.chosen()
    return {"hl": tag, "gl": region, "ceid": f"{region}:{language.primary(tag)}"}


def feed(query: str) -> tuple[str, dict]:
    """The URL and parameters for one interest's query."""
    if query.startswith("section:"):
        return SECTION.format(section=query.split(":", 1)[1]), edition()
    return SEARCH, {**edition(), "q": f"{query} when:1d"}


def parse(xml: str) -> list[Story]:
    """Items from an RSS document, in the order the feed gives them."""
    root = ET.fromstring(xml)
    stories = []
    for item in root.iterfind("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        source = item.find("source")
        outlet = (source.text or "").strip() if source is not None else ""
        # Google appends " - Outlet" to every title; the outlet is kept
        # separately, and leaving it on would make the same story from two
        # outlets two different titles.
        if outlet and title.endswith(f" - {outlet}"):
            title = title[: -len(outlet) - 3].rstrip()
        try:
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = None
        stories.append(Story(title, outlet, (item.findtext("link") or "").strip(), published))
    return stories


def key_for(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return "news:" + hashlib.sha1(" ".join(words).encode("utf-8")).hexdigest()[:16]


def wanted(conn: sqlite3.Connection) -> bool:
    """Whether there are headlines to watch: only for topics the user chose
    to follow. Nothing is followed until they do -- the topics this started
    with were one person's: business, consulting firms, MBA recruiting."""
    try:
        return bool(interests.active(conn))
    except sqlite3.OperationalError:
        return False


def news(conn: sqlite3.Connection, *, now: dt.datetime | None = None) -> list[Observation]:
    following = interests.active(conn)
    now = now or dt.datetime.now(dt.UTC)

    found: dict[str, tuple[Story, list[str]]] = {}
    by_topic: list[list[str]] = []
    failed: list[str] = []
    for interest in following:
        url, params = feed(interest.query)
        try:
            stories = parse(fetch.get_text(url, params))[:PER_FEED]
        except (fetch.FetchError, ET.ParseError) as exc:
            failed.append(f"{interest.topic}: {exc}")
            continue
        keys = []
        for story in stories:
            if story.published and now - story.published > FRESH:
                continue
            key = key_for(story.title)
            keys.append(key)
            if key in found:
                found[key][1].append(interest.topic)
            else:
                found[key] = (story, [interest.topic])
        by_topic.append(keys)

    if failed and len(failed) == len(following):
        raise SourceError("; ".join(failed)[:300])
    for problem in failed:
        log.warning("news feed failed: %s", problem)

    # A story from each topic in turn, not every story of the first topic
    # first. The source's output is capped (`settings.MAX_PER_SOURCE`), and
    # in feed order the three section feeds filled the cap on every look:
    # the consulting and MBA recruiting topics the user asked for were never
    # once judged.
    order: list[str] = []
    for rank in range(max((len(keys) for keys in by_topic), default=0)):
        for keys in by_topic:
            if rank < len(keys) and keys[rank] not in order:
                order.append(keys[rank])

    out = []
    for key in order:
        story, topics = found[key]
        local = story.published.astimezone() if story.published else None
        out.append(Observation(
            source="news",
            key=key,
            title=f"{story.title} ({story.outlet})" if story.outlet else story.title,
            detail=f"following: {', '.join(topics)}"
                   + (f"; published {local:%a %H:%M}" if local else ""),
            when=local.isoformat(timespec="minutes") if local else None,
            ref={"link": story.link, "outlet": story.outlet},
        ))
    return out


def screened(conn: sqlite3.Connection, observation: Observation) -> Observation:
    """A headline as the judge may read it (agent/screening.py).

    Whoever wrote a story wrote its headline, and a search feed carries any
    outlet's: until 11 October 2026 headlines reached the judge unscreened.
    (Weather alerts still do, by the user's choice: the government writes
    those.) A headline the screen did not pass is not shown, nor is its
    outlet -- the publisher names itself -- and what the screen allows
    stands in their place."""
    from agent import screening

    seen = screening.shown_here(conn, "news_headline", observation.key,
                                observation.ref.get("outlet"), observation.title)
    if seen.how == "text":
        return observation
    return replace(observation, title="A news headline",
                   detail=f"{observation.detail}. {seen.text}",
                   ref={"link": observation.ref.get("link")})
