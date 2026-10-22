"""What news the user follows.

Nothing, until they say: "keep an eye on Anthropic" is `follow`, "stop
telling me about markets" is `unfollow`. The topics this once started with
were one person's, and a stranger's interests are noise. The people and
organisations on their calendar are not topics here; the judge asks the doer
to look them up before the meeting, which is when news about them is worth
reading.

A topic's `query` is what the feed is asked. A topic that names one of
Google News's own sections (`SECTIONS`) reads that section's feed, edited
rather than matched, which is what "world news" means better than any
keyword; anything else is a search.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core import db

# A cap on topics, because each is a feed fetched every ninety minutes and
# a handful of headlines judged. Past this, "follow" says to drop one first.
MAX_TOPICS = 15

# Google News's section feeds, by the names a user would give them.
SECTIONS = {
    "world": "WORLD", "world news": "WORLD", "national news": "NATION",
    "business": "BUSINESS", "business news": "BUSINESS", "markets": "BUSINESS",
    "technology": "TECHNOLOGY", "tech": "TECHNOLOGY", "tech news": "TECHNOLOGY",
    "science": "SCIENCE", "health": "HEALTH", "sports": "SPORTS",
    "entertainment": "ENTERTAINMENT",
}


@dataclass
class Interest:
    topic: str
    query: str


def active(conn: sqlite3.Connection) -> list[Interest]:
    rows = conn.execute(
        "SELECT topic, query FROM interests WHERE dropped_at IS NULL ORDER BY id"
    ).fetchall()
    return [Interest(r["topic"], r["query"]) for r in rows]


def query_for(topic: str) -> str:
    """What the feed is asked for a topic: its section, or a search for it."""
    section = SECTIONS.get(topic.lower())
    return f"section:{section}" if section else f'"{topic}"'


def follow(conn: sqlite3.Connection, topic: str) -> dict:
    topic = " ".join((topic or "").split())[:80]
    if not topic:
        return {"ok": False, "error": "name something to follow"}
    existing = conn.execute(
        "SELECT dropped_at FROM interests WHERE topic = ?", (topic,)
    ).fetchone()
    if existing and existing["dropped_at"] is None:
        return {"ok": True, "topic": topic, "already": True}
    if existing is None and len(active(conn)) >= MAX_TOPICS:
        return {
            "ok": False,
            "error": f"already following {MAX_TOPICS} topics; drop one first",
            "following": [i.topic for i in active(conn)],
        }
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO interests (topic, query) VALUES (?, ?)"
            " ON CONFLICT(topic) DO UPDATE SET dropped_at = NULL",
            (topic, query_for(topic)),
        )
    return {"ok": True, "topic": topic}


def unfollow(conn: sqlite3.Connection, topic: str) -> dict:
    """Stop following a topic, matched by name without regard to case."""
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE interests SET dropped_at = datetime('now')"
            " WHERE topic = ? AND dropped_at IS NULL",
            (" ".join((topic or "").split()),),
        )
    if not cur.rowcount:
        return {
            "ok": False,
            "error": "not following anything by that name",
            "following": [i.topic for i in active(conn)],
        }
    return {"ok": True, "dropped": topic}
