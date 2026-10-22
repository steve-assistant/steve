"""The sidebar's places say what each holds, and their pages list it.

Steve's best work was hidden behind a chat box: reminders, call notes and
the extensions it builds were things to know to ask for, and nothing on
screen counted them (agent/worker/places.py).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from starlette.applications import Starlette

from agent import features, runtime, sqltime
from agent.worker import journal, places
from core import db

# Noon on a Thursday, UTC: a reminder a day either side of it is in the
# past or the future in every zone.
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    monkeypatch.setattr(features, "can_build", lambda conn: True)
    yield c
    c.close()


def _call(path: str) -> dict:
    async def go():
        transport = httpx.ASGITransport(app=Starlette(routes=places.ROUTES))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return (await c.get(path)).json()

    return asyncio.run(go())


def _remind(conn, text: str, wall: str, status: str = "active") -> None:
    conn.execute("INSERT INTO reminders (kind, text, next_fire_at, status)"
                 " VALUES ('remind', ?, ?, ?)", (text, wall, status))


def test_each_place_counts_what_it_holds_from_the_database_alone(conn, monkeypatch):
    """Asked every five seconds while the app is on screen, so no calendar
    is asked: only reminders still to go off, extensions not built in and
    builds under way, and the notes taken by listening."""
    monkeypatch.setattr(places.calendars, "agenda", lambda *a, **k: pytest.fail("asked"))
    _remind(conn, "Call the vet", "2026-10-16T17:00:00")
    _remind(conn, "Water the ferns", "2026-10-20T09:00:00")
    _remind(conn, "Yesterday's", "2026-10-14T09:00:00")
    _remind(conn, "Cancelled", "2026-10-17T09:00:00", status="cancelled")
    for key, origin in (("mail", "builtin"), ("tides", "built"), ("herbarium", "imported")):
        conn.execute("INSERT INTO modules (id, key, version, tier, origin)"
                     " VALUES (?, ?, '1', 'sandbox', ?)", (key, key, origin))
    for state in ("running", "needs_allow", "failed", "carded"):
        conn.execute("INSERT INTO module_builds (request, origin, state)"
                     " VALUES ('a tide table', 'user', ?)", (state,))
    for kind in ("call", "lecture", "dictation", "quick"):
        conn.execute("INSERT INTO notes (kind, title) VALUES (?, 'a note')", (kind,))

    assert places.places(conn, NOW) == {
        "coming": 2,
        "extensions": {"installed": 2, "building": 2, "can_build": True},
        "calls": 2,
    }
    assert _call("/control/places")["ok"]


def test_coming_up_lists_the_reminders_by_when_they_go_off_and_the_weeks_events(
        conn, monkeypatch):
    """A reminder's time is the user's wall time in the table; the page gets
    it as a moment, as every other time the app shows."""
    asked = []

    def agenda(conn, days, *, limit):
        asked.append(days)
        return {"events": [{"summary": "Dentist", "start": "2026-10-16T09:30:00+02:00",
                            "end": "2026-10-16T10:00:00+02:00", "location": "Main St"}]}

    monkeypatch.setattr(places.calendars, "agenda", agenda)
    _remind(conn, "Water the ferns", "2026-10-20T09:00:00")
    _remind(conn, "Call the vet", "2026-10-16T17:00:00")
    _remind(conn, "Yesterday's", "2026-10-14T09:00:00")

    shown = places.coming(conn, NOW)
    assert [r["text"] for r in shown["reminders"]] == ["Call the vet", "Water the ferns"]
    vet = datetime.fromisoformat(shown["reminders"][0]["at"].replace("Z", "+00:00"))
    assert vet.astimezone(places.clock.local_zone()).replace(tzinfo=None) == datetime(
        2026, 10, 16, 17, 0)
    assert shown["events"] == [{"summary": "Dentist", "start": "2026-10-16T09:30:00+02:00",
                                "end": "2026-10-16T10:00:00+02:00", "all_day": False,
                                "location": "Main St"}]
    assert asked == [places.WEEK]


def test_coming_up_strikes_off_the_reminders_done_on_todays_page_and_no_others(
        conn, monkeypatch):
    """A reminder that went off leaves the list to go; struck through on
    today's page, it says it was dealt with. Yesterday's are yesterday's,
    a minute either side of the page's turn, and a cancelled one never went
    off at all."""
    monkeypatch.setattr(places.calendars, "agenda", lambda conn, days, *, limit: {})
    start = sqltime.moment(journal.bounds(journal.page_of(NOW))[0])

    def finished(text: str, at: datetime, status: str = "done") -> None:
        _remind(conn, text, "2026-10-15T09:00:00", status)
        conn.execute("UPDATE reminders SET finished_at = ? WHERE text = ?",
                     (sqltime.stamp(at), text))

    finished("Feed the cat", start + timedelta(hours=3))
    finished("Take the bins out", start + timedelta(minutes=1))
    finished("Last night's", start - timedelta(minutes=1))
    finished("Called off", start + timedelta(hours=1), "cancelled")

    shown = places.coming(conn, NOW)
    assert [r["text"] for r in shown["done"]] == ["Take the bins out", "Feed the cat"]
    bins = datetime.fromisoformat(shown["done"][0]["at"].replace("Z", "+00:00"))
    assert bins == start + timedelta(minutes=1)
    assert shown["reminders"] == []


def test_call_notes_are_listed_newest_first_each_with_the_page_that_opens_at_it(conn):
    """The day is the one place a note is read and deleted, so the list
    gives each note's page and its entry's key, as a search's result does."""
    for kind, title, started in (("call", "Budget review", "2026-10-14 15:00:00"),
                                 ("dictation", "A thought", "2026-10-15 09:00:00"),
                                 ("lecture", "Seminar", "2026-10-15 01:30:00")):
        conn.execute("INSERT INTO notes (kind, title, started_at) VALUES (?, ?, ?)",
                     (kind, title, started))

    listed = _call("/control/calls")["calls"]
    assert [(c["title"], c["kind"]) for c in listed] == [("Seminar", "lecture"),
                                                         ("Budget review", "call")]
    seminar = listed[0]
    assert seminar["at"] == "2026-10-15T01:30:00Z"
    assert seminar["key"] == f"listening:{seminar['id']}"
    assert seminar["date"] == journal.page_of(datetime(2026, 10, 15, 1, 30, tzinfo=UTC)).isoformat()
