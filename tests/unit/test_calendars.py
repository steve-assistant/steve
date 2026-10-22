"""Every calendar as one: each read, and each event counted once.

Two failures matter. The meeting never seen: their Fabrikam coffee chat was on the
Contoso calendar only, so no brief, no check-in, no "who's meeting with me".
And the meeting seen twice: the user copies Contoso events onto Google by hand, and
a merge that missed a copy would brief them on "Tailspin First Year
Presentations" twice -- or worse, one that merged two different meetings
would hand one firm's guests to the other's.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agent import calendars
from core import clock
from modules.outlook_classic import calendar as outlook_calendar
from modules.outlook_classic import provider as outlook_providers
from steve_sdk import classes
from steve_sdk.classes import Event
from steve_sdk.errors import Transient
from tests import persona
from tests.fakes import outlook as fake_outlook
from tests.gate import slotfake

NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _in_new_york(monkeypatch):
    monkeypatch.setattr(clock, "local_zone", lambda: NY)


def _e(summary, start, end=None, **kw):
    return {"id": kw.pop("id", summary), "summary": summary, "start": start,
            "end": end or start, **kw}


def _account(name: str, *, writes: bool) -> SimpleNamespace:
    can = frozenset({classes.UPDATE}) if writes else frozenset()
    return SimpleNamespace(name=name, provider=SimpleNamespace(has=lambda c: c in can))


def _merged(google: list[dict], on_outlook: list[dict]) -> list[dict]:
    """`merge` over two accounts: one whose events the tools can change, and
    one they can only read."""
    return calendars.merge([(_account("outlook", writes=False), on_outlook),
                            (_account("google", writes=True), google)])


# --------------------------------------------------------------------------
# Counted once
# --------------------------------------------------------------------------


def test_a_copy_on_both_calendars_is_one_event_with_the_outlook_guests():
    google = [_e("Tailspin First Year Presentations", "2026-09-28T19:00:00-04:00",
                 "2026-09-28T20:00:00-04:00", id="g1")]
    on_outlook = [_e("Tailspin First Year Presentations", "2026-09-28T19:00:00-04:00",
                     "2026-09-28T21:00:00-04:00", id="outlook-1",
                     attendees=[{"email": "nina@tailspin.example", "name": "Nina D"}],
                     organizer="nina@tailspin.example")]

    [event] = _merged(google, on_outlook)

    # The copy the editing tools can change, though its account was asked
    # second; the end differs because their hand-made copy did not keep it.
    assert event["id"] == "g1" and event["also_on"] == "outlook"
    assert event["attendees"] == [{"email": "nina@tailspin.example", "name": "Nina D"}]
    assert event["organizer"] == "nina@tailspin.example"


def test_two_chats_at_the_same_hour_are_two_chats():
    """Sharing "Coffee Chat" is sharing a kind of meeting, not a meeting --
    merged, the Fabrikam guests would have been briefed as Northwind's."""
    google = [_e("Northwind Coffee Chat", "2026-09-28T16:00:00-04:00")]
    on_outlook = [_e("Fabrikam Coffee Chat", "2026-09-28T16:00:00-04:00",
                     attendees=[{"email": "lena@fabrikam.example"}])]

    merged = _merged(google, on_outlook)

    assert len(merged) == 2
    assert not next(e for e in merged if e["summary"].startswith("Northwind")).get("attendees")


@pytest.mark.parametrize(("left", "right", "same"), [
    ("Coffee Chat", "Coffee chat", True),  # nothing but generic words: equal titles only
    ("Coffee Chat", "Coffee", False),
    ("Fabrikam 2026 Contoso Design Summit Kick-Off",
     "Fabrikam: Confirmed for 2026 Contoso Design Summit Kick-Off", True),
])
def test_titles_are_compared_by_what_they_are_about(left, right, same):
    a = _e(left, "2026-09-23T19:00:00-04:00")
    b = _e(right, "2026-09-23T19:00:00-04:00")

    assert (calendars.likeness(a, b) >= calendars.SAME_TITLE) is same


def test_the_best_match_wins_among_several_at_the_same_time():
    """The CDO's Friday: three sessions at 11:00, and the Contoso entry is the
    one whose title is the same, not the one that shares "Designing"."""
    at = "2026-09-25T11:00:00-04:00"
    google = [_e("Designing Your Career (Required — choose one)", at, id="choose"),
              _e("CDO Fall Fridays 3: Designing Your Career", at, id="exact")]
    on_outlook = [_e("CDO Fall Fridays 3: Designing Your Career", at)]

    merged = _merged(google, on_outlook)

    assert [e["id"] for e in merged if e.get("also_on")] == ["exact"]
    assert len(merged) == 2


def test_different_times_or_kinds_are_never_one_event():
    google = [_e("Breakout Room", "2026-10-01T14:30:00-04:00"),
              _e("Breakout Room", "2026-10-01", all_day=True)]
    on_outlook = [_e("Breakout Room", "2026-10-01T15:30:00-04:00")]

    assert len(_merged(google, on_outlook)) == 3


def test_one_outlook_entry_per_google_event():
    at = "2026-10-01T14:30:00-04:00"
    merged = _merged([_e("Breakout Room", at)],
                     [_e("Breakout Room", at, id="y1"), _e("Breakout Room", at, id="y2")])

    assert len(merged) == 2


def test_both_calendars_come_back_in_one_order():
    merged = _merged(
        [_e("Later", "2026-09-26T13:30:00-04:00"), _e("Birthday", "2026-09-26", all_day=True)],
        [_e("Start Up Recruiting Group Kickoff", "2026-09-25T12:45:00-04:00")],
    )

    assert [e["summary"] for e in merged] == [
        "Start Up Recruiting Group Kickoff", "Birthday", "Later"]


# --------------------------------------------------------------------------
# When one calendar fails
# --------------------------------------------------------------------------


class _Down(slotfake.Calendar):
    """A calendar whose server does not answer."""

    def events(self, start, end, *, limit):
        raise Transient("the server did not answer")


def _soon(hours: int, title: str, native: str, **kw) -> Event:
    start = dt.datetime.now(NY).replace(second=0, microsecond=0) + dt.timedelta(hours=hours)
    return Event(native_id=native, title=title, start=start,
                 end=start + dt.timedelta(hours=1), **kw)


def test_a_failing_calendar_is_named_and_the_others_still_answer(monkeypatch):
    slotfake.plug(monkeypatch, "calendar", work=_Down(),
                  google=slotfake.Calendar(_soon(2, "Basics of Economics", "g1")))

    result = calendars.agenda(None, 7)

    assert [e["summary"] for e in result["events"]] == ["Basics of Economics"]
    assert len(result["problems"]) == 1 and "work" in result["problems"][0]
    assert "error" not in result


def test_with_every_calendar_failing_it_is_an_error(monkeypatch):
    """What the attention sources record as a failed run, not as "nothing"."""
    slotfake.plug(monkeypatch, "calendar", work=_Down(), google=_Down())

    assert "error" in calendars.agenda(None, 7)


def test_a_series_edited_once_is_one_change(monkeypatch):
    """A calendar that cannot list what changed is read whole: every
    occurrence of an edited series carries the edit, and each would have
    been its own "Changed or new"."""
    fresh = dt.datetime.now(NY).isoformat(timespec="seconds")
    stale = (dt.datetime.now(NY) - dt.timedelta(days=3)).isoformat(timespec="seconds")
    accounts = slotfake.plug(monkeypatch, "calendar", work=slotfake.Calendar(
        *[_soon(24 * d, "DES 203 Workshop", f"w{d}", extra={"series": "s1", "updated": fresh})
          for d in (1, 8)],
        _soon(30, "Untouched", "u1", extra={"series": "s2", "updated": stale})))

    changed = calendars.changed(None, since_hours=24)

    assert [e["id"] for e in changed["events"]] == [accounts["work"].id("w1")]


def test_a_cancelled_event_without_times_is_still_a_change(monkeypatch):
    """A deleted event comes back from a server's list of changes with no
    times; read by its window, the most useful change of all was dropped."""

    class _Lists(slotfake.Calendar):
        def changed(self, since, start, end, *, limit):
            return [Event(native_id="gone", title="Review", status="cancelled")]

    slotfake.plug(monkeypatch, "calendar", google=_Lists())

    [event] = calendars.changed(None, since_hours=24)["events"]

    assert event["status"] == "cancelled"


@pytest.fixture
def on_outlook(monkeypatch):
    state = {"events": [], "error": None, "windows": []}

    def calendar(start, end, *, limit=100, match=None):
        state["windows"].append((start, end))
        if state["error"]:
            return {"error": state["error"], "events": []}
        found = [e for e in state["events"] if match is None or match(SimpleNamespace(
            Subject=e["summary"], Location="", Organizer="", RequiredAttendees=e.get("names", ""),
            OptionalAttendees=""))]
        return {"events": found}

    monkeypatch.setattr(outlook_calendar, "calendar", calendar)
    return state


def test_search_reads_the_outlook_names_without_resolving_every_guest(on_outlook):
    on_outlook["events"] = [
        _e("Breakout Room", "2026-10-01T14:30:00-04:00", names="Brook, Ada; Vale, Iris"),
        _e("Basics of Accounting", "2026-10-01T14:35:00-04:00"),
    ]
    now = dt.datetime.now(NY)

    found = outlook_providers.OutlookCalendar(fake_outlook.context(None)).search(
        ["ada"], now, now + dt.timedelta(days=365), limit=20)

    assert [e.title for e in found] == ["Breakout Room"]


# --------------------------------------------------------------------------
# Reading Outlook
# --------------------------------------------------------------------------


UTC = dt.UTC


class _Guest:
    def __init__(self, email, name="", kind=1, response=3):
        self.Address = email
        self.Name = name
        self.Type = kind
        self.MeetingResponseStatus = response


class _Guests:
    def __init__(self, guests):
        self._guests = guests
        self.Count = len(guests)

    def Item(self, i):  # noqa: N802 - COM's name
        return self._guests[i - 1]


class _Entry:
    def __init__(self, subject, start, minutes=30, *, status=3, guests=(),
                 organizer="x@fabrikam.example",
                 all_day=False, body="Agenda: ...", recurring=False):
        self.Subject = subject
        self.StartUTC = start.astimezone(UTC)
        self.EndUTC = (start + dt.timedelta(minutes=minutes)).astimezone(UTC)
        self.AllDayEvent = all_day
        self.MeetingStatus = status
        self.GlobalAppointmentID = f"gid-{subject}"
        self.EntryID = f"eid-{subject}"
        self.Recipients = _Guests(list(guests))
        self.Location = "Larkspur Hall"
        self.Body = body
        self.IsRecurring = recurring
        self.LastModificationTime = self.StartUTC
        self.CreationTime = self.StartUTC
        self._organizer = organizer

    def GetOrganizer(self):  # noqa: N802
        return SimpleNamespace(Address=self._organizer)


ME = {persona.WORK_EMAIL}
AT = dt.datetime(2026, 9, 24, 15, 0, tzinfo=NY)


def test_a_outlook_meeting_reads_as_a_google_one():
    entry = _Entry("Lena Fischer SA OCR Coffee Chats", AT, guests=[
        _Guest("lena.fischer@fabrikam.example", "Fischer, Lena"),
        _Guest("maya@contoso.example", "Lindqvist, Maya"),
        _Guest("lena.fischer@fabrikam.example", "Fischer, Lena"),  # organiser, twice
        _Guest("larkspur-2400@contoso.example", "Larkspur 2400",
               kind=outlook_calendar.RECIPIENT_RESOURCE),
    ], organizer="lena.fischer@fabrikam.example")

    event = outlook_calendar._appointment(entry, tz=NY, mine=ME)

    assert event["start"] == "2026-09-24T15:00:00-04:00"
    assert event["end"] == "2026-09-24T15:30:00-04:00"
    # Found again from its id: its EntryID, and the start of the occurrence.
    assert event["id"] == f"{entry.EntryID}@20260924T190000Z" and event["calendar"] == "outlook"
    assert event["attendees"] == [
        {"email": "lena.fischer@fabrikam.example", "name": "Lena Fischer",
         "response": "accepted", "self": False},
        {"email": "maya@contoso.example", "name": "Maya Lindqvist",
         "response": "accepted", "self": True},
    ]
    assert event["organizer"] == "lena.fischer@fabrikam.example"
    assert event["organizer_self"] is False


def test_an_invitations_body_is_not_read_only_their_own_appointments():
    """Received Contoso mail reaches a model as headers only, and an invitation
    is received mail; what the meeting is, who, where and when, is enough."""
    theirs = outlook_calendar._appointment(
        _Entry("Coffee", AT, status=outlook_calendar.MEETING_RECEIVED), tz=NY, mine=ME)
    mine = outlook_calendar._appointment(
        _Entry("Study group", AT, status=outlook_calendar.MEETING_NONE,
               organizer="maya@contoso.example"), tz=NY, mine=ME)

    assert theirs["description"] == "" and mine["description"] == "Agenda: ..."
    assert mine["organizer_self"] is True


def test_an_all_school_invitation_is_counted_not_read():
    """352 guests is seconds of Exchange lookups, for a list nobody prepares for."""
    many = [_Guest(f"s{i}@contoso.example") for i in range(outlook_calendar.MAX_GUESTS_READ + 1)]

    event = outlook_calendar._appointment(
        _Entry("CDO Fall Friday", AT, guests=many), tz=NY, mine=ME)

    assert event["attendees"] == []
    assert event["attendee_count"] == outlook_calendar.MAX_GUESTS_READ + 1


def test_cancelled_and_all_day_entries_keep_their_meaning():
    cancelled = outlook_calendar._appointment(_Entry("Chat", AT, status=5), tz=NY, mine=ME)
    retreat = outlook_calendar._appointment(
        _Entry("Retreat", dt.datetime(2026, 10, 1, tzinfo=NY), minutes=24 * 60, all_day=True),
        tz=NY, mine=ME)

    assert cancelled["status"] == "cancelled"
    assert (retreat["start"], retreat["end"], retreat["all_day"]) == (
        "2026-10-01", "2026-10-02", True)


def test_each_occurrence_of_a_series_is_its_own_event():
    week = [outlook_calendar._appointment(
        _Entry("DES 203", AT + dt.timedelta(days=7 * i), recurring=True), tz=NY, mine=ME)
        for i in range(2)]

    assert week[0]["id"] != week[1]["id"] and week[0]["series"] == week[1]["series"]


class _Items:
    """An Outlook Items collection, sorted, that records how far it was read."""

    def __init__(self, entries):
        self.entries = entries
        self.read = 0
        self.IncludeRecurrences = False

    def Sort(self, key):  # noqa: N802
        assert key == "[Start]"

    def GetFirst(self):  # noqa: N802
        self.read = 0
        return self.GetNext()

    def GetNext(self):  # noqa: N802
        if self.read >= len(self.entries):
            return None
        self.read += 1
        return self.entries[self.read - 1]


def test_the_walk_stops_at_the_end_of_the_window():
    """With recurrences included the collection has no end; stopping at the
    first entry past the window is what makes reading it finish."""
    entries = [_Entry(f"e{i}", AT + dt.timedelta(days=i)) for i in range(-3, 30)]
    items = _Items(entries)

    found = outlook_calendar._walk_calendar(
        SimpleNamespace(Items=items), AT - dt.timedelta(hours=1),
        AT + dt.timedelta(days=2), tz=NY, mine=ME)

    assert [e["summary"] for e in found] == ["e0", "e1"]
    assert items.IncludeRecurrences is True
    assert items.read == 6  # three before, two inside, one past -- and no further


async def test_the_night_learns_their_guests_but_not_them(tmp_path):
    """Both calendars mark them `self`; recorded, the user would be a contact of their
    own, met at every meeting the user has."""
    from core import db
    from tools import ingest

    def agenda(conn, days, limit):
        return {"events": [_e("Breakout Room", "2026-10-01T14:30:00-04:00", attendees=[
            {"email": "ada.brook@contoso.example", "name": "Ada Brook"},
            {"email": "maya@contoso.example", "self": True},
        ])]}

    conn = db.open_store(tmp_path / "ingest.db")
    try:
        await ingest.calendar(conn, agenda=agenda)
        emails = {r[0] for r in conn.execute("SELECT email FROM people")}
    finally:
        conn.close()

    assert emails == {"ada.brook@contoso.example"}


# --------------------------------------------------------------------------
# Read, not changed
# --------------------------------------------------------------------------


async def test_an_event_on_a_calendar_that_is_only_read_cannot_be_moved_from_here(
        monkeypatch):
    """Through the toolset the model is given, not the module's own list."""
    from agent import loop
    from agent.models import Role
    from steve_sdk.classes import Event
    from tests.gate import slotfake

    tools = loop.build_agent(Role.CONVERSE)._function_toolset.tools
    ctx = SimpleNamespace(deps=SimpleNamespace(tools_used=[], conn=None, performed={}))
    held = slotfake.Calendar(Event(native_id="w1", title="Review",
                                   start=slotfake.at("2026-10-01T10:00:00-04:00"),
                                   end=slotfake.at("2026-10-01T11:00:00-04:00")),
                             capabilities=frozenset())
    work = slotfake.plug(monkeypatch, "calendar", outlook=held)["outlook"]

    result = await tools["update_event"].function(ctx, work.id("w1"), start="2026-10-01T15:00")

    assert result["ok"] is False and "outlook calendar" in result["error"]
    assert held.writes == []
