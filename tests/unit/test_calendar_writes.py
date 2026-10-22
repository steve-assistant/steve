"""Writing to their calendar: the time its source gave, and nothing twice.

One conversation on 6 October 2026: a Tailspin confirmation said "04:00pm -
05:00pm MST". The event went in at 4 pm Eastern, two hours early; then again
under a second title at the same hour, past a duplicate check that wanted the
identical title; then, with no way to delete, the first copy was renamed
"Fabrikam — Remove duplicate calendar entry" and left on their calendar. The user said:
"please don't do that in the future". These hold it in code, not in a promise.
"""

from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agent import zones
from agent.toolset import calendar
from core import clock
from steve_sdk.classes import Event
from steve_sdk.errors import Transient
from tests.gate import slotfake

EASTERN = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _in_pittsburgh(monkeypatch):
    """Their laptop's zone, pinned: the conversions are only right relative to it.
    `zones.in_user_time` reads `core.clock.local_zone`; the calendar's own `_tz`
    is pinned too, for the tools."""
    monkeypatch.setattr(clock, "local_zone", lambda: EASTERN)
    monkeypatch.setattr("modules.google_cloud.calendar._tz", lambda: EASTERN)


def _ctx():
    return SimpleNamespace(deps=SimpleNamespace(tools_used=[], conn=None))


def _tools() -> dict:
    registered: dict = {}
    calendar.register(lambda fn: registered.setdefault(fn.__name__, fn))
    return registered


# ---------------------------------------------------------------------------
# The time as written, converted by code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("written", [
    "MST", "MDT", "MT", "Mountain", "Mountain Time", "Mountain Standard Time",
    "MST (UTC-7)", "America/Denver", "Denver", "Boulder", "UTC-6", "-06:00",
])
def test_four_pm_mountain_in_october_goes_in_at_six_eastern(written):
    """Every way the Tailspin email could have named its zone. "MST" is the
    region: in October Denver keeps daylight time, and so does Pittsburgh."""
    assert zones.in_user_time("2026-10-15T16:00", written)[0] == "2026-10-15T18:00"


def test_daylight_saving_is_the_zone_database_s_job_not_a_fixed_offset():
    """US clocks go back on 1 November 2026; both sides move together, so
    Mountain is still two hours behind. Pacific-to-London is not constant."""
    assert zones.in_user_time("2026-11-05T16:00", "MST")[0] == "2026-11-05T18:00"
    assert zones.in_user_time("2026-10-29T09:00", "Europe/London")[0] == "2026-10-29T05:00"
    assert zones.in_user_time("2026-11-05T09:00", "Europe/London")[0] == "2026-11-05T04:00"


@pytest.mark.parametrize(("written", "theirs"), [
    ("PT", "2026-10-15T19:00"), ("San Francisco", "2026-10-15T19:00"),
    ("Central Time (US & Canada)", "2026-10-15T17:00"), ("Chicago", "2026-10-15T17:00"),
    ("CET", "2026-10-15T10:00"), ("Asia/Tokyo", "2026-10-15T03:00"),
])
def test_other_zones_the_user_sees_are_converted_too(written, theirs):
    assert zones.in_user_time("2026-10-15T16:00", written)[0] == theirs


def test_a_zone_it_cannot_place_is_refused_rather_than_guessed():
    """"IST" is India, Israel or Ireland. A guess lands the event at a wrong
    hour without a word; a refusal makes the model ask or name the city."""
    for unknown in ("IST", "Narnia", "UTC+19"):
        with pytest.raises(ValueError, match="unknown timezone"):
            zones.in_user_time("2026-10-15T16:00", unknown)


def test_their_own_zone_or_none_leaves_the_time_alone():
    assert zones.in_user_time("2026-10-15T16:00", None) == ("2026-10-15T16:00", None)
    assert zones.in_user_time("2026-10-15T16:00", "Eastern") == ("2026-10-15T16:00", None)


def test_the_time_is_converted_to_the_users_own_zone_wherever_that_is(monkeypatch):
    """The examples above are written for one zone; the user may be in any.
    Pinned to Berlin, four in the afternoon in Denver is midnight, the next
    day -- and CI's machine, in UTC, gets the same answer."""
    monkeypatch.setattr(clock, "local_zone", lambda: ZoneInfo("Europe/Berlin"))

    assert zones.in_user_time("2026-10-15T16:00", "MST")[0] == "2026-10-16T00:00"


def test_every_zone_helper_follows_the_one_the_user_is_in(monkeypatch):
    """Nine helpers each asked the machine, with different fallbacks; a test
    or an eval that pinned one zone moved only some of them."""
    from agent import autonomy_eval, history
    from core import modctx
    from modules.google_cloud import calendar as google_calendar
    from modules.google_cloud import common as google_common
    from modules.outlook_classic import outlook
    from tests.fakes import google as fake_google
    from tests.fakes import outlook as fake_outlook
    from tools import memory

    tokyo = ZoneInfo("Asia/Tokyo")
    monkeypatch.undo()
    monkeypatch.setattr(clock, "local_zone", lambda: tokyo)

    # A module's zone is its context's: core's, which was the machine's
    # fixed offset until step 5's item 9.
    assert modctx.local_zone() is tokyo
    with google_common.as_account(fake_google.account()):
        assert google_calendar._tz() is tokyo
    assert history._zone() is tokyo
    with outlook.as_account(fake_outlook.context(None)):
        assert outlook._local_zone() is tokyo
    assert autonomy_eval._tz() is tokyo
    import datetime as dt

    assert memory._today() == dt.datetime.now(tokyo).date()


def test_an_offset_in_the_time_itself_wins_over_a_named_zone():
    assert zones.in_user_time("2026-10-15T16:00-06:00", "PST")[0] == "2026-10-15T18:00"


async def test_create_event_puts_the_converted_time_on_the_card_and_in_the_answer(
        monkeypatch):
    seen = []

    async def act(ctx, tool, args, preview):
        seen.append((tool, args, preview))
        return {"created": True, "start": args["start"]}

    monkeypatch.setattr(calendar, "act", act)

    said = await _tools()["create_event"](
        _ctx(), "Get to Know Tailspin Denver", "2026-10-15T16:00", timezone="MST")

    (tool, args, preview), = seen
    assert args["start"] == "2026-10-15T18:00"
    assert "America/Denver" in preview, "the card shows what it was converted from"
    assert "4:00 PM America/Denver" in said["converted"]
    assert "6:00 PM the user's time" in said["converted"]
    assert "allow_duplicate" not in args, "the ledger keeps its usual shape"


async def test_create_event_with_a_zone_it_cannot_place_does_nothing(monkeypatch):
    async def act(*args, **kwargs):
        raise AssertionError("an unplaceable time must not reach the calendar")

    monkeypatch.setattr(calendar, "act", act)

    said = await _tools()["create_event"](_ctx(), "Call", "2026-10-15T16:00", timezone="IST")

    assert said["ok"] is False and "unknown timezone" in said["error"]


# ---------------------------------------------------------------------------
# Moving and removing
# ---------------------------------------------------------------------------


def _event(native, summary, start, end, **extra) -> Event:
    return Event(native_id=native, title=summary, start=slotfake.at(start),
                 end=slotfake.at(end), **extra)


def _on_calendar(monkeypatch, *events: Event, **kw):
    """One calendar account holding `events`: the provider, and the account
    for the ids it hands out."""
    google = slotfake.Calendar(*events, **kw)
    return google, slotfake.plug(monkeypatch, "calendar", google=google)["google"]


async def test_moving_an_event_keeps_its_length(monkeypatch):
    """Moved with a start alone, it kept its old end: "move my 3pm to 5pm"
    asked Google for 5pm to 3:45pm."""
    seen = []

    async def act(ctx, tool, args, preview):
        seen.append(args)
        return {"updated": True}

    monkeypatch.setattr(calendar, "act", act)
    _, google = _on_calendar(monkeypatch, _event(
        "e1", "Dentist", "2026-10-15T15:00:00-04:00", "2026-10-15T15:45:00-04:00"))

    await _tools()["update_event"](_ctx(), google.id("e1"), start="2026-10-15T17:00")
    await _tools()["update_event"](_ctx(), google.id("e1"), start="2026-10-15T15:00",
                                   timezone="CT")

    assert [(a["start"], a["end"]) for a in seen] == [
        ("2026-10-15T17:00", "2026-10-15T17:45"),
        ("2026-10-15T16:00", "2026-10-15T16:45"),
    ]


async def test_deleting_an_event_goes_on_a_card_that_names_it(monkeypatch):
    """There was no way to delete, so the model renamed its mistake instead.
    A deletion is their to agree to (`delete_event` is gated in
    core/consent.yaml); the card says what goes and who is told, and binds
    the event as it showed it."""
    seen = []

    async def act(ctx, tool, args, preview):
        seen.append((tool, args, preview))
        return {"queued": True}

    monkeypatch.setattr(calendar, "act", act)
    held = _event("f6ig", "Tailspin Denver Networking Session", "2026-10-15T16:00:00-04:00",
                  "2026-10-15T17:00:00-04:00", extra={"my_response": "accepted"})
    _, google = _on_calendar(monkeypatch, held)

    await _tools()["delete_event"](_ctx(), google.id("f6ig"), why="a duplicate I added")

    (tool, args, preview), = seen
    assert tool == "delete_event"
    assert args == {"event_id": google.id("f6ig"), "attendees": [],
                    "shown": held.digest(), "why": "a duplicate I added"}, \
        "she is not her own guest"
    assert "Tailspin Denver Networking Session" in preview and "No guests" in preview
    assert "a duplicate I added" in preview


async def test_an_event_on_a_calendar_that_is_only_read_cannot_be_deleted_from_here(
        monkeypatch):
    async def act(*args, **kwargs):
        raise AssertionError("a read-only calendar's event must not reach the gate")

    monkeypatch.setattr(calendar, "act", act)
    work = slotfake.Calendar(_event("w1", "Contoso review", "2026-10-15T09:00:00-04:00",
                                    "2026-10-15T10:00:00-04:00"), capabilities=frozenset())
    outlook = slotfake.plug(monkeypatch, "calendar", outlook=work)["outlook"]

    said = await _tools()["delete_event"](_ctx(), outlook.id("w1"))
    moved = await _tools()["update_event"](_ctx(), outlook.id("w1"), start="2026-10-15T11:00")

    for refused in (said, moved):
        assert refused["ok"] is False and "outlook calendar" in refused["error"]
        assert "read but not changed" in refused["error"]


# ---------------------------------------------------------------------------
# Nothing twice
# ---------------------------------------------------------------------------


def _like(account, summary, start="2026-10-15T16:00", minutes=60):
    from agent import calendars

    return [e["summary"] for e in
            calendars.existing_like(account, summary, start, duration_minutes=minutes)]


def test_the_same_event_under_another_name_at_the_same_hour_is_a_duplicate(monkeypatch):
    """The second Tailspin entry, two minutes after the first."""
    _, google = _on_calendar(monkeypatch, _event(
        "t1", "Tailspin Denver Networking Session",
        "2026-10-15T16:00:00-04:00", "2026-10-15T17:00:00-04:00"))

    assert _like(google, "Get to Know Tailspin Denver — Networking") == [
        "Tailspin Denver Networking Session"]


def test_the_same_event_the_same_day_at_another_hour_is_a_duplicate(monkeypatch):
    """A time zone got wrong, and the fix added it again instead of moving it."""
    _, google = _on_calendar(monkeypatch, _event(
        "t1", "Get to Know Tailspin Denver — Networking",
        "2026-10-15T18:00:00-04:00", "2026-10-15T19:00:00-04:00"))

    assert _like(google, "Tailspin Denver") == ["Get to Know Tailspin Denver — Networking"]


def test_different_events_are_not_duplicates(monkeypatch):
    """Two firms' coffee chats at one hour; two sessions of one course on one
    day; the same title on another day, or cancelled, or all day."""
    import datetime as dt

    _, google = _on_calendar(
        monkeypatch,
        _event("n1", "Northwind Coffee Chat", "2026-10-15T16:00:00-04:00",
               "2026-10-15T16:30:00-04:00"),
        _event("d1", "DES 203 Lecture", "2026-10-15T10:00:00-04:00",
               "2026-10-15T11:20:00-04:00"),
        _event("f1", "Fabrikam Coffee Chat", "2026-10-15T09:00:00-04:00",
               "2026-10-15T09:30:00-04:00", status="cancelled"),
        _event("f2", "Fabrikam Coffee Chat", "2026-10-16T16:00:00-04:00",
               "2026-10-16T16:30:00-04:00"),
        Event(native_id="trip", title="Fabrikam Coffee Chat trip", all_day=True,
              start=dt.date(2026, 10, 15), end=dt.date(2026, 10, 16)),
    )

    assert _like(google, "Fabrikam Coffee Chat") == []
    assert _like(google, "DES 203 Workshop") == []


def test_a_title_of_only_generic_words_still_matches_itself_at_the_same_minute(monkeypatch):
    _, google = _on_calendar(monkeypatch, _event(
        "c1", "Coffee Chat", "2026-10-15T16:00:00-04:00", "2026-10-15T16:30:00-04:00"))

    assert _like(google, "Coffee chat!") == ["Coffee Chat"]
    assert _like(google, "Coffee chat", start="2026-10-15T17:00") == []


def test_the_check_reads_their_whole_day(monkeypatch):
    asked = []

    class Asked(slotfake.Calendar):
        def events(self, start, end, *, limit):
            asked.append((start.isoformat(), end.isoformat()))
            return super().events(start, end, limit=limit)

    google = slotfake.plug(monkeypatch, "calendar", google=Asked())["google"]

    _like(google, "Anything", start="2026-10-15T23:30", minutes=90)

    assert asked == [("2026-10-15T00:00:00-04:00", "2026-10-16T01:00:00-04:00")], \
        "an event past midnight is covered"


def test_only_the_calendar_it_is_going_to_is_checked(monkeypatch):
    """A hand-made copy, on one calendar, of an invitation on another is how
    a user works, not a duplicate (`agent/calendars.merge` lists them once)."""
    accounts = slotfake.plug(
        monkeypatch, "calendar", google=slotfake.Calendar(),
        outlook=slotfake.Calendar(_event("o1", "Tailspin Denver Networking Session",
                                         "2026-10-15T16:00:00-04:00",
                                         "2026-10-15T17:00:00-04:00")))

    assert _like(accounts["google"], "Tailspin Denver Networking Session") == []


async def test_a_duplicate_is_not_created_and_the_way_out_is_named(monkeypatch):
    held, google = _on_calendar(monkeypatch, _event(
        "x", "Tailspin Denver Networking Session", "2026-10-15T18:00:00-04:00",
        "2026-10-15T19:00:00-04:00"))

    said = await calendar._do_create_event(summary="Get to Know Tailspin Denver",
                                           start="2026-10-15T18:00")

    assert held.writes == []
    assert said["ok"] is False and said["already_on_calendar"][0]["id"] == google.id("x")
    assert "update_event" in said["error"] and "allow_duplicate" in said["error"]


async def test_a_separate_event_the_user_vouched_for_can_be_added(monkeypatch):
    held, _ = _on_calendar(monkeypatch, _event(
        "x", "DES 203 Workshop", "2026-10-15T18:00:00-04:00", "2026-10-15T19:00:00-04:00"))

    said = await calendar._do_create_event(summary="DES 203 Workshop", allow_duplicate=True,
                                           start="2026-10-15T18:00")

    assert said["created"] is True
    assert [(what, e.title) for what, e in held.writes] == [("create", "DES 203 Workshop")]


async def test_a_check_that_fails_stops_the_create(monkeypatch):
    """Creating blind is how the doubled workshop happened."""

    class Unreadable(slotfake.Calendar):
        def events(self, start, end, *, limit):
            raise Transient("the calendar did not answer")

    broken = Unreadable()
    slotfake.plug(monkeypatch, "calendar", google=broken)

    said = await calendar._do_create_event(summary="DES 203 Workshop",
                                           start="2026-10-15T18:00")

    assert said["ok"] is False and broken.writes == []
