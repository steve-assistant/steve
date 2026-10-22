"""The calendar, end to end: the agenda reads the mailbox's own calendar
(CalDAV) and a calendar feed (ICS) as one; an event is added at once and
found on the server by its UID; one with a guest is refused before any
card, as a calendar by password invites no one; and an event is deleted
only on a yes, and only that one, checked by its UID.

The calendar is the world's provider's (tests/fakes/world.py): Radicale,
with a plain server's habits, served over TLS at the address a user gives
in setup's "Other". The feed is served at its private link, which is a
secret, so the link is a canary as the keys are: setup keeps only its host.
Both are added as setup adds them, through `/control/accounts`, and the
backend reaches them from its own processes through the world's network
(tests/fakes/worldnet.py). A yes to inviting guests needs a calendar that
can invite, which the world does not serve: the unit tests drive it
(tests/unit/test_calendar_slot.py), and the engines' own tests run in the
test's process (tests/unit/test_caldav_engine.py,
tests/unit/test_ics_engine.py).
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.e2e.backend import Backend, running, until
from tests.fakes import ics
from tests.fakes.jev import FakeJev
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.world import CALDAV, world

NOW = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
DENTIST = {"title": "Dentist", "start": NOW + dt.timedelta(hours=26),
           "end": NOW + dt.timedelta(hours=27), "uid": "dentist-1@example.com"}
ESSAY = {"title": "Essay due", "start": NOW + dt.timedelta(hours=30),
         "end": NOW + dt.timedelta(hours=30, minutes=30), "uid": "essay-1@school.example"}
DAY = (dt.date.today() + dt.timedelta(days=2)).isoformat()

ASK_AGENDA = "What's on my calendar in the next few days?"
ASK_ADD = f"Put choir supper on my calendar on {DAY} at 6:30 pm."
ASK_INVITE = f"Invite Theo to choir practice on {DAY} at 8 pm."
ASK_CANCEL = "Cancel my dentist appointment."
SUPPER = {"summary": "Choir supper", "start": f"{DAY}T18:30", "duration_minutes": 90}
PRACTICE = {"summary": "Choir practice", "start": f"{DAY}T20:00",
            "attendees": ["theo@example.com"]}


def first_event(result: str) -> dict:
    """The first event a search found, by its id as the model reads it."""
    return {"event_id": json.loads(result)["events"][0]["id"], "why": "they cancelled it"}


RULES = (
    Rule(said=ASK_AGENDA, call="calendar_agenda", arguments={"days": 3}),
    Rule(after="calendar_agenda", result="", say="The dentist tomorrow, and an essay due."),
    Rule(said=ASK_ADD, call="create_event", arguments=SUPPER),
    Rule(said=ASK_INVITE, call="create_event", arguments=PRACTICE),
    Rule(after="create_event", result="", say="Done."),
    Rule(said=ASK_CANCEL, call="calendar_search", arguments={"query": "dentist"}),
    Rule(after="calendar_search", result="Dentist", call="delete_event", arguments=first_event),
    Rule(after="delete_event", result="", say="It waits for your yes on the card."),
)


@pytest.fixture(scope="module")
def steve(fenced):
    feed = ics.FeedWorld()
    feed.seed("calendar", [ESSAY])
    # What a calendar says is screened as mail is (core/screen.py); these
    # are plain events.
    jev = FakeJev(otherwise=0.1, handling="as_is")
    with (world(fenced / "world", openai=FakeOpenAI(RULES), jev=jev, mail=True, calendar=True,
                feed=feed) as made,
          running(made, fenced / "data") as backend):
        made.calendar.seed("calendar", [DENTIST])
        for body in (made.mailbox(), made.feed_account()):
            added = backend.post("/control/accounts", body).json()
            assert added.get("ok") and added["test"]["ok"], (added, backend.logs())
        yield backend


def action(steve: Backend, note: dict) -> str:
    return next(a["status"] for a in steve.get("/control/activity").json()["items"]
                if a["kind"] == "action" and a["id"] == note["action_id"])


def told(steve: Backend, tool: str) -> str:
    """What the last call of `tool` handed the model."""
    return [text for name, text in steve.world.openai.results if name == tool][-1]


async def test_the_agenda_reads_the_calendar_and_the_feed_as_one(steve):
    """The model is handed both: the dentist from the mailbox's calendar,
    read by CalDAV, and the essay from the feed, fetched at its link."""
    said = await steve.chat(ASK_AGENDA)
    assert said["type"] == "reply", steve.logs()
    agenda = told(steve, "calendar_agenda")
    assert "Dentist" in agenda and "Essay due" in agenda, agenda


async def test_an_event_without_guests_is_added_at_once_and_found_by_its_uid(steve):
    """Private and undone by deleting it, so no card: the server holds
    exactly one event more, read back by its UID, at the time asked for,
    and every other event as it was."""
    cal = steve.world.calendar
    before = cal.snapshot("calendar")
    said = await steve.chat(ASK_ADD)
    assert said["type"] == "reply", steve.logs()

    after = cal.snapshot("calendar")
    [uid] = set(after) - set(before)
    assert {k: after[k] for k in before} == before
    stored = cal.raw(f"https://{CALDAV}{after[uid][0]}")
    [event] = [e for e in stored.walk("VEVENT") if str(e["UID"]) == uid]
    assert str(event["SUMMARY"]) == "Choir supper"
    start = event.decoded("DTSTART")
    assert (start.date().isoformat(), start.astimezone().strftime("%H:%M")) == (DAY, "18:30")


async def test_an_event_with_a_guest_is_refused_before_a_card_where_the_calendar_cannot_invite(
        steve):
    """A calendar by password invites no one. Asked first, the user's yes
    met the account's refusal only when it ran (19 October 2026, this
    scenario); now the model is told before any card, and nothing is
    written."""
    before = steve.world.calendar.snapshot("calendar")
    said = await steve.chat(ASK_INVITE)
    assert said["type"] == "reply", steve.logs()
    assert "cannot invite anyone" in told(steve, "create_event")
    assert not [n for n in steve.get("/control/needs").json()["needs"] if n["kind"] == "yes"]
    assert steve.world.calendar.snapshot("calendar") == before


async def test_an_event_is_deleted_only_on_yes_and_only_that_one(steve):
    """A deletion cannot be taken back, so it waits for a yes on a card
    naming the event. Before the yes the server holds what it held; after
    it, exactly the dentist's UID is gone, and every other event is as it
    was, its etag unchanged."""
    cal = steve.world.calendar
    before = cal.snapshot("calendar")
    said = await steve.chat(ASK_CANCEL)
    assert said["type"] == "reply", steve.logs()
    note = steve.card("Dentist")
    assert cal.snapshot("calendar") == before

    assert steve.decide(note, True).json()["ok"]

    assert until(lambda: action(steve, note) == "done"), steve.logs()
    after = cal.snapshot("calendar")
    assert set(before) - set(after) == {DENTIST["uid"]}
    assert after == {k: v for k, v in before.items() if k != DENTIST["uid"]}
