"""A calendar listing gives the model the events, not the calendar's bookkeeping,
and never stops short without saying so.

Over their last 119 calendar reads (6 October 2026) Google's event links alone
were 18,000 characters, and every event repeated the listing's zone, its
"confirmed" status and three false flags. And a list cut at its limit read
as everything there was.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from agent import calendars
from agent.toolset.calendar import LISTED_GUESTS, for_the_model
from core import clock
from steve_sdk.classes import Event
from tests.gate import slotfake

NY = ZoneInfo("America/New_York")

Northwind = {
    "id": "id#66b491739f", "summary": "Maya Lindqvist + Northwind Chat",
    "start": "2026-10-07T16:00:00-04:00", "end": "2026-10-07T16:15:00-04:00",
    "timezone": "America/New_York", "all_day": False, "location": None,
    "description": "Maya, please find your coffee chat details with Northwind below.",
    "attendees": [
        {"email": "steve.test.user@gmail.com", "name": None, "response": "accepted",
         "self": True},
        {"email": "maya@lindqvist.example", "name": None, "response": "needsAction", "self": False},
    ],
    "organizer": "namrtascheduling@northwind.example", "organizer_self": False,
    "link": "https://www.google.com/calendar/event?eid=" + "X" * 300,
    "status": "confirmed", "recurring": False,
}


def test_an_event_keeps_what_it_is_when_where_and_who():
    [event] = for_the_model({"timezone": "America/New_York", "events": [Northwind]})["events"]

    assert event == {
        "id": "id#66b491739f", "summary": "Maya Lindqvist + Northwind Chat",
        "start": "2026-10-07T16:00:00-04:00", "end": "2026-10-07T16:15:00-04:00",
        "description": "Maya, please find your coffee chat details with Northwind below.",
        "organizer": "namrtascheduling@northwind.example",
        "user_response": "accepted",
        "guests": ["maya@lindqvist.example (needsAction)"],
    }


def test_their_own_events_do_not_name_them_as_organiser():
    [event] = for_the_model({"events": [{**Northwind, "organizer": "steve.test.user@gmail.com",
                                         "organizer_self": True}]})["events"]
    assert "organizer" not in event


def test_what_is_true_but_unusual_is_kept():
    [event] = for_the_model({"events": [{**Northwind, "all_day": True, "recurring": True,
                                         "status": "tentative", "also_on": "outlook"}]})["events"]
    assert (event["all_day"], event["recurring"], event["status"], event["also_on"]) == (
        True, True, "tentative", "outlook")


def test_a_lecture_s_guest_list_is_named_ten_deep():
    guests = [{"email": f"s{n}@contoso.example", "name": f"Student {n}", "response": None}
              for n in range(70)]
    [event] = for_the_model({"events": [{**Northwind, "attendees": guests}]})["events"]

    assert len(event["guests"]) == LISTED_GUESTS + 1
    assert event["guests"][0] == "Student 0 <s0@contoso.example>"
    assert event["guests"][-1] == "+60 more"


def test_a_long_description_keeps_its_join_link():
    zoom = "https://contoso.zoom.us/j/123456789"
    long = "Agenda. " * 200 + f"Join: {zoom}"
    [event] = for_the_model({"events": [{**Northwind, "description": long}]})["events"]

    assert len(event["description"]) < 800 and zoom in event["description"]


def test_the_listing_s_own_fields_are_untouched_and_the_input_is_not_changed():
    found = {"timezone": "America/New_York", "count": 1, "events": [Northwind], "cut": "x"}
    out = for_the_model(found)

    assert (out["timezone"], out["count"], out["cut"]) == ("America/New_York", 1, "x")
    assert "link" in Northwind and "attendees" in Northwind


DAY = dt.datetime(2026, 10, 7, tzinfo=NY)


@pytest.fixture(autouse=True)
def _in_new_york(monkeypatch):
    monkeypatch.setattr(clock, "local_zone", lambda: NY)


def _e(n: int) -> Event:
    start = DAY.replace(hour=8 + n)
    return Event(native_id=f"e{n}", title=f"Event {n}", start=start,
                 end=start + dt.timedelta(minutes=30))


def _listed(monkeypatch, *, google: range, work: range, limit: int, asked: int) -> dict:
    slotfake.plug(monkeypatch, "calendar",
                  google=slotfake.Calendar(*[_e(n) for n in google]),
                  work=slotfake.Calendar(*[_e(n) for n in work]))
    end = DAY + dt.timedelta(days=1)
    return calendars._listing(None, lambda p: p.events(DAY, end, limit=asked),
                              start=DAY, end=end, limit=limit, asked=asked)


def test_a_list_cut_at_its_limit_says_where_it_stopped(monkeypatch):
    out = _listed(monkeypatch, google=range(3), work=range(3, 6), limit=4, asked=10)

    assert out["count"] == 4
    assert "2026-10-07T11:00:00-04:00" in out["cut"] and "more after" in out["cut"]


def test_a_full_page_from_one_calendar_may_have_more_behind_it(monkeypatch):
    out = _listed(monkeypatch, google=range(4), work=range(0), limit=4, asked=4)
    assert "cut" in out


def test_a_list_with_room_to_spare_says_nothing_about_cutting(monkeypatch):
    out = _listed(monkeypatch, google=range(2), work=range(5, 6), limit=4, asked=10)
    assert "cut" not in out and out["count"] == 3
