"""The news, summarised, and their newsletters' key points.

The maintainer, 25 September 2026: summaries in the briefings and on request instead
of a list of headlines, including key details from the Substacks the user
subscribed to. The newsletters are the one received mail whose text reaches a
model, so which mail counts as one is pinned here.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from agent.attention import briefing, newsdesk
from agent.llm import CallRecord
from agent.models import Role
from steve_sdk.classes import Message
from tests.gate import mailfake

FIXTURES = Path(__file__).parent / "fixtures" / "attention"


def _record():
    return CallRecord(role=Role.CONVERSE, slug="test/luna", input_tokens=500, output_tokens=80,
                      cached_tokens=0, reasoning_tokens=0, cost_usd=0.0002, latency_ms=500,
                      attempts=1)


@pytest.fixture
def desk(monkeypatch):
    """A model that summarises on cue, articles read on cue, no newsletters."""
    state = {"writes": 0, "reads": 0, "prompts": [], "letters": []}
    newsdesk._summaries.clear()

    async def write(system, prompt):
        state["writes"] += 1
        state["prompts"].append(prompt)
        return "- Fed held rates steady (Reuters)\n- Fieldnotes: three hiring signals", _record()

    async def read(stories):
        state["reads"] += 1
        return [newsdesk.Source("article", s["title"], s.get("outlet", ""), text="body",
                                key=s.get("key") or s["title"]) for s in stories]

    monkeypatch.setattr(newsdesk, "_write", write)
    monkeypatch.setattr(newsdesk, "read_stories", read)
    def newsletters(conn, days=2, limit=10):
        # A connection, always: one call passed its day count as the
        # database, and a fake that ignored it hid that for weeks.
        assert isinstance(conn, sqlite3.Connection), conn
        return list(state["letters"])

    monkeypatch.setattr(newsdesk, "newsletters", newsletters)
    return state


STORIES = [{"title": "Fed holds rates", "outlet": "Reuters", "key": "news:1"}]


# ---------------------------------------------------------------------------
# Which mail is a newsletter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("sender", "subject", "is_one"), [
    ("Ben at Fieldnotes <fieldnotes@substack.com>", "Three hiring signals this week", True),
    ("Someone <writer@mail.substack.com>", "Issue 12", True),
    ("Ben at Fieldnotes <fieldnotes@substack.com>", "Please confirm your subscription", False),
    ("Ray Ellis <opsweekly@substack.com>", "419144 is your Substack verification code", False),
    ("Kim Hale <kimhaleletters@substack.com>", "Welcome to Kim Hale's Letters", False),
    ("Clara Moss <clara@northwind.example>", "Coffee Thursday?", False),
    ("Spoof <x@substack.com.evil.io>", "Issue 12", False),
])
def test_only_posts_from_newsletter_senders_count(sender, subject, is_one):
    assert newsdesk.is_newsletter({"from": sender, "subject": subject}) is is_one


class _Inbox(mailfake.Mailbox):
    """An account whose search returns everything, whatever it was asked,
    and which says what it read whole."""

    def __init__(self, *messages: Message) -> None:
        super().__init__("maya@example.com", *messages)
        self.read: list[str] = []

    def search(self, query, *, limit, since=None):
        return [dataclasses.replace(m, body=None) for m in self.items.values()]

    def fetch(self, native_id, *, body_chars=20_000):
        self.read.append(native_id)
        return super().fetch(native_id, body_chars=body_chars)


def test_a_personal_email_is_never_read(monkeypatch):
    """Whatever the search returns, only an allowed sender's post has its
    body read: their own received mail stays headers-only."""
    inbox = _Inbox(
        Message("a", sender="Fieldnotes <fieldnotes@substack.com>", subject="Issue 3",
                body="post text"),
        Message("b", sender="Clara Moss <clara@northwind.example>", subject="Coffee?",
                body="private"),
        Message("c", sender="Fieldnotes <fieldnotes@substack.com>",
                subject="Confirm your email", body="housekeeping"))
    accounts = mailfake.connect(monkeypatch, home=inbox)

    found = newsdesk.newsletters(None)

    assert inbox.read == ["a"]
    assert [(s.outlet, s.title, s.text) for s in found] == [("Fieldnotes", "Issue 3", "post text")]
    assert found[0].key == f"letter:{accounts['home'].id('a')}", \
        "known by an id that names its account, so one switched off is not remembered"


# ---------------------------------------------------------------------------
# The briefing's news
# ---------------------------------------------------------------------------


async def test_a_briefing_written_again_does_not_pay_twice(conn, desk):
    """The handover rewrites the next briefing on a timer; the same stories
    must not be searched for and summarised each time."""
    first = await newsdesk.for_briefing(conn, STORIES, kind="morning briefing",
                                        topics=["markets"], briefing="briefing:morning:d")
    again = await newsdesk.for_briefing(conn, STORIES, kind="morning briefing",
                                        topics=["markets"], briefing="briefing:morning:d")

    assert first == again and "Fed held rates" in first
    assert (desk["reads"], desk["writes"]) == (1, 1)


async def test_a_newsletter_is_in_one_briefing_not_every_one(conn, desk):
    letter = newsdesk.Source("newsletter", "Issue 3", "Fieldnotes", text="post", key="letter:a")
    desk["letters"] = [letter]

    await newsdesk.for_briefing(conn, [], kind="morning briefing", topics=[],
                                briefing="briefing:morning:d")
    assert "Fieldnotes" in desk["prompts"][-1]

    newsdesk._summaries.clear()
    await newsdesk.for_briefing(conn, STORIES, kind="evening briefing", topics=[],
                                briefing="briefing:evening:d")
    assert "Fieldnotes" not in desk["prompts"][-1], "the evening repeated the morning's newsletter"

    newsdesk._summaries.clear()
    await newsdesk.for_briefing(conn, [], kind="morning briefing", topics=[],
                                briefing="briefing:morning:d")
    assert "Fieldnotes" in desk["prompts"][-1], "the same briefing, written again, lost it"


async def test_a_spent_budget_means_headlines(conn, desk):
    from core import spend

    spend.bill(conn, 0.25, model="m", unit=spend.Unit("background", "work"))

    assert await newsdesk.for_briefing(conn, STORIES, kind="morning briefing", topics=[],
                                       briefing="b") == ""
    assert desk["writes"] == 0


def test_the_summary_replaces_the_headline_list():
    import datetime as dt

    facts = briefing.Facts(kind="morning", about=dt.date(2026, 9, 26),
                           news_summary="- Fed held rates steady (Reuters)")

    text = briefing.render(facts)

    assert "In the news:\n  · Fed held rates steady (Reuters)" in text


# ---------------------------------------------------------------------------
# On request
# ---------------------------------------------------------------------------


async def test_the_news_on_a_topic_when_the_user_asks(conn, desk, monkeypatch):
    from agent.attention import fetch

    xml = (FIXTURES / "google_news_world.xml").read_text(encoding="utf-8")
    asked: list[dict] = []
    monkeypatch.setattr(fetch, "get_text", lambda url, params=None: asked.append(params) or xml)

    found = await newsdesk.on_request(conn, "Saudi Arabia")

    assert asked[0]["q"].startswith("Saudi Arabia")
    assert "Fed held rates" in found["summary"] and len(found["sources"]) == 3
    assert desk["letters"] == [] and "newsletter" not in desk["prompts"][-1]


async def test_the_news_asked_for_on_everything_brings_the_users_newsletters(conn, desk):
    """"What's the news?" reads the user's newsletters with the headlines.
    It handed the reader its day count as the database until 21 October
    2026, and the error it raised was logged and passed over: no answer had
    a newsletter in it."""
    desk["letters"] = [newsdesk.Source("newsletter", "Issue 3", "Fieldnotes", text="post",
                                       key="letter:a")]

    found = await newsdesk.on_request(conn)

    assert "Fieldnotes" in desk["prompts"][-1]
    assert [s["outlet"] for s in found["sources"]] == ["Fieldnotes"]
