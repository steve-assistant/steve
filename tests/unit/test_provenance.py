"""A calendar entry made from an email says what the email says.

6 October 2026: the confirmation read "October 15, 2026 04:00pm - 05:00pm
MST"; the agent added a guessed 4 pm before finding it, then 4 pm Eastern
after. Code now reads the email it names and refuses what the email
contradicts.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agent import provenance
from agent.toolset import calendar

EASTERN = ZoneInfo("America/New_York")
TAILSPIN = ("Maya, you are confirmed to attend Get to Know Tailspin Denver - October 2026\n"
            "You are confirmed to attend Get to Know Tailspin Denver - October 2026\n"
            "https://tailspin.zoom.us/j/95550100123\n\n   October 15, 2026   \n\n"
            "   04:00pm - 05:00pm MST   \n\n [1] Update your response")


def _check(text, start, zone=None):
    return provenance.refusal(text, dt.datetime.fromisoformat(start), zone, EASTERN)


def test_their_tailspin_email_is_read_for_its_date_times_and_zone():
    assert provenance.dates(TAILSPIN) == {(10, 15)}
    said = provenance.times(TAILSPIN)
    assert [(sorted(s.minutes), s.zone) for s in said] == [([16 * 60], "MST"), ([17 * 60], "MST")]


@pytest.mark.parametrize(("start", "zone", "refused"), [
    ("2026-10-15T16:00", "MST", None),
    ("2026-10-15T16:00", None, 'pass timezone="MST"'),  # the second event, 4 pm Eastern
    ("2026-10-15T18:00", None, "not 6:00 PM"),          # converted by the model
    ("2026-10-16T16:00", "MST", "not Oct 16"),
    ("2026-10-15T16:00", "ET", "in MST, not ET"),
])
def test_an_event_is_refused_where_the_email_contradicts_it(start, zone, refused):
    said = _check(TAILSPIN, start, zone)
    if refused is None:
        assert said is None
    else:
        assert said is not None and refused in said


@pytest.mark.parametrize(("text", "start"), [
    ("Office hours Friday, October 16th, 2-3 p.m.", "2026-10-16T14:00"),
    ("Team lunch 11-1 pm in Larkspur Hall", "2026-10-09T11:00"),
    ("Lunch at noon on 10/9", "2026-10-09T12:00"),
    ("DES 203 Workshop: 10:05 - 11:25, Room 2410", "2026-10-09T10:05"),
    ("Meet the leadership team, Oct. 20 at 1 p.m. (Mountain Time)", "2026-10-20T13:00"),
    ("Kickoff on 2026-10-22 at 16:00", "2026-10-22T16:00"),
])
def test_the_ways_their_emails_write_a_time_are_read(text, start):
    zone = "MT" if "Mountain" in text else None
    assert _check(text, start, zone) is None, text


def test_what_the_email_does_not_say_is_not_refused():
    """No date, no time: nothing to check against, and nothing guessed."""
    assert _check("See you at the networking session this Thursday!", "2026-10-15T16:00") is None


def test_a_month_and_year_is_not_a_date():
    assert provenance.dates("Get to Know Tailspin Denver - October 2026") == set()


# ---------------------------------------------------------------------------
# Through create_event
# ---------------------------------------------------------------------------


@pytest.fixture
def tool(monkeypatch):
    monkeypatch.setattr("modules.google_cloud.calendar._tz", lambda: EASTERN)
    seen = []

    async def act(ctx, name, args, preview):
        seen.append(args)
        return {"created": True}

    monkeypatch.setattr(calendar, "act", act)
    found: dict = {}
    calendar.register(lambda fn: found.setdefault(fn.__name__, fn))
    return found["create_event"], seen


def _ctx():
    return SimpleNamespace(deps=SimpleNamespace(tools_used=[]))


async def test_create_event_checks_the_email_it_names(tool, monkeypatch):
    create, seen = tool
    monkeypatch.setattr("agent.toolset.mail.read_any",
                        lambda message_id, upto: {"subject": "Confirmed", "body": TAILSPIN})

    refused = await create(_ctx(), "Get to Know Tailspin Denver", "2026-10-15T16:00",
                           from_message_id="home:1601327635:4159:INBOX")
    added = await create(_ctx(), "Get to Know Tailspin Denver", "2026-10-15T16:00",
                         timezone="MST", from_message_id="home:1601327635:4159:INBOX")

    assert refused["ok"] is False and "MST" in refused["error"]
    assert added == {"created": True, "converted": added["converted"]}
    assert [a["start"] for a in seen] == ["2026-10-15T18:00"]


async def test_an_email_that_cannot_be_read_stops_the_create(tool, monkeypatch):
    create, seen = tool
    monkeypatch.setattr("agent.toolset.mail.read_any",
                        lambda message_id, upto: {"error": "not found", "kind": "not_found"})

    said = await create(_ctx(), "Call", "2026-10-15T16:00", from_message_id="abc")

    assert said["ok"] is False and "Could not read" in said["error"] and seen == []


# ---------------------------------------------------------------------------
# A turn that read their mail names the email, or says none
# ---------------------------------------------------------------------------
#
# The check above ran only when the model volunteered `from_message_id`, and
# the 6 October failure was the model not naming the email it had just read
# (8 October 2026 audit, R8).


def _read_mail(*tools):
    return SimpleNamespace(deps=SimpleNamespace(tools_used=list(tools), conn=None))


@pytest.mark.parametrize("read", ["read_email", "search_email", "email_thread"])
async def test_an_event_made_after_reading_their_mail_names_no_email_and_is_refused_once(
        tool, read):
    create, seen = tool
    ctx = _read_mail(read)

    refused = await create(ctx, "Get to Know Tailspin Denver", "2026-10-15T16:00")
    again = await create(ctx, "Get to Know Tailspin Denver", "2026-10-15T16:00")

    assert refused["ok"] is False and refused["kind"] == "usage"
    assert refused["error"] == calendar.NO_SOURCE
    assert "from_message_id" in refused["error"] and "none" in refused["error"]
    assert again == {"created": True} and len(seen) == 1, "asked once, then it goes through"


@pytest.mark.parametrize("answer", ["none", "None", "  NONE "])
async def test_saying_none_is_an_answer_and_the_event_goes_in(tool, monkeypatch, answer):
    create, seen = tool

    def read_any(message_id, upto):
        raise AssertionError("none is not an email to read")

    monkeypatch.setattr("agent.toolset.mail.read_any", read_any)

    added = await create(_read_mail("read_email"), "Dentist", "2026-10-15T16:00",
                         from_message_id=answer)

    assert added == {"created": True} and len(seen) == 1


async def test_naming_the_email_is_still_checked_against_it(tool, monkeypatch):
    create, seen = tool
    monkeypatch.setattr("agent.toolset.mail.read_any",
                        lambda message_id, upto: {"subject": "Confirmed", "body": TAILSPIN})

    said = await create(_read_mail("read_email"), "Get to Know Tailspin Denver",
                        "2026-10-15T16:00", from_message_id="home:1:2:INBOX")

    assert said["ok"] is False and "MST" in said["error"] and seen == []


@pytest.mark.parametrize("tools", [[], ["calendar_agenda"], ["web_search", "create_event"]])
async def test_an_event_in_a_turn_that_read_no_mail_is_not_asked_where_it_is_from(
        tool, tools):
    create, seen = tool

    added = await create(_read_mail(*tools), "Dentist", "2026-10-15T16:00")

    assert added == {"created": True} and len(seen) == 1


async def test_each_event_is_asked_once_not_the_turn(tool):
    """An email that lists three dates makes three events: the second and
    third are not waved through because the first was asked."""
    create, seen = tool
    ctx = _read_mail("read_email")
    dates = ["2026-10-15T16:00", "2026-10-16T16:00", "2026-10-17T16:00"]

    first = [await create(ctx, "Info session", d) for d in dates]
    second = [await create(ctx, "Info session", d) for d in dates]

    assert all(r["ok"] is False for r in first)
    assert second == [{"created": True}] * 3 and len(seen) == 3


async def test_a_real_turns_deps_remember_what_was_asked(tool):
    """The stand-in above has no field for it; `Deps` does, one per hop."""
    from agent.turn import Deps

    create, seen = tool
    deps = Deps(conn=None, session_id=1)
    deps.tools_used.append("read_email")
    ctx = SimpleNamespace(deps=deps)

    first = await create(ctx, "Dentist", "2026-10-15T16:00")
    second = await create(ctx, "Dentist", "2026-10-15T16:00")

    assert first["ok"] is False and second == {"created": True}
    assert deps.asked_where_from == {"create_event:dentist|2026-10-15T16:00"}


@pytest.fixture
def update(monkeypatch):
    """`update_event`, on a calendar holding one event; what reached the
    gate; and the event's id."""
    from steve_sdk.classes import Event
    from tests.gate import slotfake

    monkeypatch.setattr("core.clock.local_zone", lambda: EASTERN)
    seen = []

    async def act(ctx, name, args, preview):
        seen.append(args)
        return {"updated": True}

    monkeypatch.setattr(calendar, "act", act)
    held = slotfake.Calendar(Event(native_id="e1", title="Dentist",
                                   start=slotfake.at("2026-10-15T15:00:00-04:00"),
                                   end=slotfake.at("2026-10-15T15:45:00-04:00")))
    event_id = slotfake.plug(monkeypatch, "calendar", google=held)["google"].id("e1")
    found: dict = {}
    calendar.register(lambda fn: found.setdefault(fn.__name__, fn))
    move = found["update_event"]

    async def moved(ctx, _native, **args):
        return await move(ctx, event_id, **args)

    return moved, seen


async def test_moving_an_event_after_reading_their_mail_names_the_email_or_is_asked_once(update):
    move, seen = update
    ctx = _read_mail("search_email")

    refused = await move(ctx, "e1", start="2026-10-15T17:00")
    again = await move(ctx, "e1", start="2026-10-15T17:00")

    assert refused["error"] == calendar.NO_SOURCE and again == {"updated": True}
    assert len(seen) == 1


async def test_renaming_an_event_after_reading_their_mail_has_no_time_to_check(update):
    """No new time, no email to check it against."""
    move, seen = update

    done = await move(_read_mail("read_email"), "e1", summary="Dentist (moved)")

    assert done == {"updated": True} and len(seen) == 1


async def test_a_move_that_says_none_goes_through(update):
    move, seen = update

    done = await move(_read_mail("read_email"), "e1", start="2026-10-15T17:00",
                      from_message_id="none")

    assert done == {"updated": True} and len(seen) == 1
