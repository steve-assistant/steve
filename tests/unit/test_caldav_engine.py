"""Core's CalDAV engine, against Radicale behind the habits of real servers.

Each server a preset meets runs the same calendar contract
(`steve_sdk.testing.calendar`) through core's write path: a plain server
at the address the user typed ("Other"), found from its root or given
whole; Gmail's legacy address with Google's habits (a new event stored
under its UID, a filter ignored); iCloud's discovery across its hosts; and
a server that names new events itself. The rest are the engine's own: an
address the server returns on a host the module did not declare never
sees the password, a wrong password is said as one, a change keeps what
the event held, each preset finds its calendar, and setup adds "Other" to
the calendar class only with a calendar's address.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from agent import accounts, calendars, connectors
from core import db, modules, writes
from steve_sdk.classes import Event
from steve_sdk.errors import AfterWrite, NotConnected, TargetChanged, Unsupported, WrongPassword
from steve_sdk.testing import calendar
from tests.fakes import caldav as rigs
from tests.fakes import mailrig

MAYA = "maya@mail.example"
GMAIL_MAYA = "maya.lindqvist@gmail.com"
ICLOUD_MAYA = "maya.icloud@mail.example"
HOST = "caldav.mail.example"
# The "Other" preset as setup keeps it: the address typed, its host pinned.
OTHER = {"hosts": {HOST: "public"}}


@pytest.fixture
def make_rig(tmp_path):
    made: list[rigs.CalRig] = []

    def build(mode: str = rigs.STANDARD, **kw) -> rigs.CalRig:
        folder = tmp_path / f"rig{len(made)}"
        folder.mkdir()
        rig = rigs.rig(mode, folder=folder, **kw)
        made.append(rig)
        return rig

    yield build
    for rig in made:
        rig.close()


def _other(rig: rigs.CalRig, url: str):
    return rig.provider(mailrig.module("imap"), preset="other", fields={**OTHER, "url": url})


# --- the contract, on every server ------------------------------------------------

@pytest.mark.parametrize("check", calendar.CHECKS, ids=lambda c: c.__name__)
def test_a_calendar_at_the_address_typed_keeps_the_contract(make_rig, check):
    """The plainest case: "Other", with the calendar's own address."""
    rig = make_rig()
    check(calendar.CalendarCase(_other(rig, rig.world.calendar_url()), rig.world, writes, MAYA))


@pytest.mark.parametrize("check", calendar.CHECKS, ids=lambda c: c.__name__)
def test_a_calendar_found_from_the_servers_root_keeps_the_contract(make_rig, check):
    """The address typed is the server's: the principal, then its calendars."""
    rig = make_rig()
    check(calendar.CalendarCase(_other(rig, f"https://{HOST}/"), rig.world, writes, MAYA))


@pytest.mark.parametrize("check", calendar.CHECKS, ids=lambda c: c.__name__)
def test_gmail_keeps_the_contract_with_googles_habits(make_rig, check):
    """Google's legacy address, where a new event lives under its UID and a
    calendar-query is answered with every event, whatever its filter."""
    rig = make_rig(rigs.GOOGLE, host="www.google.com", user=GMAIL_MAYA, calendars=("events",))
    provider = rig.provider(mailrig.module("gmail_password"), address=GMAIL_MAYA)
    check(calendar.CalendarCase(provider, rig.world, writes, GMAIL_MAYA))


@pytest.mark.parametrize("check", calendar.CHECKS, ids=lambda c: c.__name__)
def test_icloud_keeps_the_contract_across_its_hosts(make_rig, check):
    """Discovery from caldav.icloud.com answers with the account's own host
    under it, which the preset declares."""
    rig = make_rig(host="caldav.icloud.com", principal_host="p01-caldav.icloud.com",
                   user=ICLOUD_MAYA)
    provider = rig.provider(mailrig.module("imap"), preset="icloud", address=ICLOUD_MAYA)
    check(calendar.CalendarCase(provider, rig.world, writes, ICLOUD_MAYA))


@pytest.mark.parametrize("check", calendar.CHECKS, ids=lambda c: c.__name__)
def test_a_server_that_names_new_events_itself_keeps_the_contract(make_rig, check):
    """The new event is not where it was PUT: found again by its UID, and
    only where exactly one event has it, though the server answers the
    search for it with every event."""
    rig = make_rig(rigs.OWN_NAMES, ignores_filters=True)
    check(calendar.CalendarCase(_other(rig, rig.world.calendar_url()), rig.world, writes, MAYA))


# --- the engine's own ---------------------------------------------------------------

def test_an_address_on_a_host_the_module_did_not_declare_never_sees_the_password(make_rig):
    """A server, hostile or misconfigured, that names its principal on
    another host would have the engine send the password there."""
    rig = make_rig(principal_host="elsewhere.example")
    provider = _other(rig, f"https://{HOST}/")

    with pytest.raises(NotConnected, match="elsewhere.example"):
        provider.calendars()

    assert "elsewhere.example" not in rig.network.reached
    assert {host for host, _, _ in rig.habits.requests} == {HOST}


@pytest.mark.parametrize("write", ["update", "delete"])
def test_a_change_made_on_the_phone_between_the_read_and_the_write_is_kept(make_rig, write):
    """The engine reads the event just before it writes; only the write's
    own condition (`If-Match` on that read's etag) catches a change made
    in the moment between."""
    rig = make_rig()
    [native] = rig.world.seed("calendar", [{"title": "Swim", "start": calendar.MONDAY,
                                            "end": calendar.MONDAY + dt.timedelta(hours=1)}])
    provider = _other(rig, rig.world.calendar_url())
    event = provider.fetch(native)
    rig.habits.after_get = lambda: rig.world.mutate("calendar", native, title="Swim at 8")

    with pytest.raises(TargetChanged):
        if write == "update":
            event.title = "Swim with Ada"
            provider.update(event)
        else:
            provider.delete(event)

    assert provider.fetch(native).title == "Swim at 8"


def test_an_event_the_phone_changed_on_another_day_as_a_move_went_is_not_the_moves_doing(
        make_rig):
    """The check after a write compared the whole calendar, so Friday's
    choir, synced from the phone in the second after Monday's lunch moved,
    read as the move's doing: the user heard that nothing was written, of a
    move that went, and was asked again. It compares the days the event
    spans; on them, another event changed is still reported, as written."""
    rig = make_rig()
    hour = dt.timedelta(hours=1)
    lunch, gym, choir = rig.world.seed("calendar", [
        {"title": "Lunch", "start": calendar.MONDAY + 12 * hour,
         "end": calendar.MONDAY + 13 * hour},
        {"title": "Gym", "start": calendar.MONDAY + 7 * hour, "end": calendar.MONDAY + 8 * hour},
        {"title": "Choir", "start": calendar.MONDAY + dt.timedelta(days=4, hours=19),
         "end": calendar.MONDAY + dt.timedelta(days=4, hours=20)}])
    provider = _other(rig, rig.world.calendar_url())

    def later(event: Event) -> Event:
        event.start, event.end = event.start + hour, event.end + hour
        return event

    rig.habits.after_put = lambda: rig.world.mutate("calendar", choir, title="Choir (Hall B)")
    moved = writes.update(provider, lunch, later, card=provider.fetch(lunch).digest())
    assert moved.start == calendar.MONDAY + 13 * hour

    rig.habits.after_put = lambda: rig.world.mutate("calendar", gym, title="Gym with Ada")
    with pytest.raises(AfterWrite, match="more changed"):
        writes.update(provider, lunch, later, card=provider.fetch(lunch).digest())
    assert provider.fetch(lunch).start == calendar.MONDAY + 14 * hour, "the move went"


def test_a_new_event_never_overwrites_one_already_under_its_uid(make_rig):
    rig = make_rig()
    rig.world.seed("calendar", [{"uid": "rota@else.example", "title": "Rota",
                                 "start": calendar.MONDAY,
                                 "end": calendar.MONDAY + dt.timedelta(hours=1)}])
    provider = _other(rig, rig.world.calendar_url())
    before = rig.world.snapshot("calendar")

    with pytest.raises(TargetChanged):
        provider.create(Event(native_id="", ident="rota@else.example", title="Mine",
                              start=calendar.MONDAY,
                              end=calendar.MONDAY + dt.timedelta(hours=1)))

    assert rig.world.snapshot("calendar") == before


def test_a_new_event_is_told_from_the_others_by_its_uid_on_our_side(make_rig):
    """A server that names the event itself and answers the search for its
    UID with every event: the one with that UID, checked here, is it."""
    rig = make_rig(rigs.OWN_NAMES, ignores_filters=True)
    hour = dt.timedelta(hours=1)
    rig.world.seed("calendar", [
        {"title": "Rota", "start": calendar.MONDAY, "end": calendar.MONDAY + hour},
        {"title": "Choir", "start": calendar.MONDAY, "end": calendar.MONDAY + 2 * hour}])
    provider = _other(rig, rig.world.calendar_url())

    made = provider.create(Event(native_id="", title="Dentist", start=calendar.MONDAY,
                                 end=calendar.MONDAY + dt.timedelta(hours=1)))

    assert made.title == "Dentist" and made.ident.endswith("@steve")
    assert provider.fetch(made.native_id).ident == made.ident


def test_a_wrong_password_is_said_as_one(make_rig):
    rig = make_rig()
    provider = rig.provider(mailrig.module("imap"), preset="other",
                            fields={**OTHER, "url": rig.world.calendar_url()}, password="nope")

    with pytest.raises(WrongPassword):
        provider.events(calendar.MONDAY, calendar.MONDAY + dt.timedelta(days=1), limit=5)


def test_a_change_keeps_what_the_event_held_and_counts_its_sequence(make_rig):
    """Another app's alarm and categories are the user's; a move rewrites
    the times, not the event."""
    rig = make_rig()
    native = rig.world.put_raw("home", "swim.ics", "\r\n".join((
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//phone//EN", "BEGIN:VEVENT",
        "UID:swim@phone.example", "DTSTAMP:20261001T000000Z", "SEQUENCE:2",
        "DTSTART:20261103T070000Z", "DTEND:20261103T080000Z", "SUMMARY:Swim",
        "CATEGORIES:Sport", "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Swim",
        "TRIGGER:-PT15M", "END:VALARM", "END:VEVENT", "END:VCALENDAR", "")))
    provider = _other(rig, rig.world.calendar_url())

    def later(event: Event) -> Event:
        event.start = dt.datetime(2026, 11, 3, 8, tzinfo=dt.UTC)
        event.end = dt.datetime(2026, 11, 3, 9, tzinfo=dt.UTC)
        return event

    writes.update(provider, native, later)

    [event] = rig.world.raw(native).walk("VEVENT")
    assert event.decoded("DTSTART") == dt.datetime(2026, 11, 3, 8, tzinfo=dt.UTC)
    assert str(event.get("CATEGORIES").to_ical(), "utf-8") == "Sport"
    assert [a.get("TRIGGER").to_ical() for a in event.walk("VALARM")] == [b"-PT15M"]
    assert int(event.get("SEQUENCE")) == 3


def test_a_repeating_event_is_read_and_never_changed_here(make_rig):
    """One occurrence's edit is a change to the series on most servers."""
    rig = make_rig()
    rig.world.seed("calendar", [{"title": "Rota", "start": calendar.MONDAY,
                                 "end": calendar.MONDAY + dt.timedelta(hours=1),
                                 "rrule": "FREQ=WEEKLY;COUNT=3"}])
    provider = _other(rig, rig.world.calendar_url())
    [first, *_] = provider.events(calendar.MONDAY, calendar.MONDAY + dt.timedelta(days=21),
                                  limit=10)
    before = rig.world.snapshot("calendar")

    with pytest.raises(Unsupported, match="repeating"):
        writes.update(provider, first.native_id, lambda e: e)
    with pytest.raises(Unsupported, match="repeating"):
        writes.delete(provider, first.native_id, card=first.digest())

    assert rig.world.snapshot("calendar") == before


@pytest.mark.parametrize("preset, host, principal, prefix, address", [
    ("icloud", "caldav.icloud.com", "p01-caldav.icloud.com", "", ICLOUD_MAYA),
    ("fastmail", "caldav.fastmail.com", "", "/dav", "maya.fastmail@mail.example"),
    ("yahoo", "caldav.calendar.yahoo.com", "", "", "maya.yahoo@mail.example"),
])
def test_each_preset_finds_its_calendar(make_rig, preset, host, principal, prefix, address):
    rig = make_rig(host=host, principal_host=principal, prefix=prefix, user=address)
    provider = rig.provider(mailrig.module("imap"), preset=preset, address=address)

    said = provider.test()

    assert [c.name for c in provider.calendars()] == ["Home"]
    assert said.startswith("signed in to 1 calendar(s) (Home)"), said


def test_gmails_calendar_is_its_legacy_address_and_nothing_else(make_rig):
    rig = make_rig(rigs.GOOGLE, host="www.google.com", user=GMAIL_MAYA, calendars=("events",))
    provider = rig.provider(mailrig.module("gmail_password"), address=GMAIL_MAYA)

    [found] = provider.calendars()

    assert found.url == f"https://www.google.com/calendar/dav/{GMAIL_MAYA}/events/"
    assert [(m, p) for _, m, p in rig.habits.requests] == [
        ("PROPFIND", f"/calendar/dav/{GMAIL_MAYA}/events/")]


def test_the_test_server_takes_a_new_event_in_a_store_past_windows_path_limit(tmp_path):
    """Radicale nests 158 characters under its store for a new Gmail event.
    Under a pytest-xdist worker's tmp folder that ran past Windows' 260, and
    a create failed with a 500 that passed alone: a fake that fails by where
    it runs would hide the engine's own failures among its own."""
    folder = tmp_path / ("deep" * 30)
    folder.mkdir()
    rig = rigs.rig(rigs.GOOGLE, host="www.google.com", user=GMAIL_MAYA, calendars=("events",),
                   folder=folder)
    try:
        provider = rig.provider(mailrig.module("gmail_password"), address=GMAIL_MAYA)
        made = provider.create(Event(native_id="", title="Dentist", start=calendar.MONDAY,
                                     end=calendar.MONDAY + dt.timedelta(hours=1)))
        assert provider.fetch(made.native_id).title == "Dentist"
    finally:
        rig.close()


# --- the slot ---------------------------------------------------------------------

def test_a_gmail_account_by_app_password_fills_the_calendar_class(make_rig, monkeypatch):
    """Added as the app adds it, the account's calendar is one of the
    user's: free time is read from it (on our side; CalDAV has no
    free/busy here), and a new event goes on it."""
    from agent import runtime
    from agent.toolset import calendar as tools
    from core import clock
    from core.engines import caldav

    password = "abcdabcdabcdabcd"
    rig = make_rig(rigs.GOOGLE, host="www.google.com", user=GMAIL_MAYA, password=password,
                   calendars=("events",))
    rig.world.seed("calendar", [{"title": "Plot meeting",
                                 "start": dt.datetime(2026, 11, 3, 10, tzinfo=dt.UTC),
                                 "end": dt.datetime(2026, 11, 3, 11, tzinfo=dt.UTC)}])
    monkeypatch.setattr(connectors, "NETWORK", rig.network)
    monkeypatch.setattr(clock, "local_zone", lambda: dt.UTC)
    conn = db.open_store()
    monkeypatch.setattr(runtime, "conn", conn)
    modules.register(conn)
    key = accounts.add(conn, module_id="gmail_password", address=GMAIL_MAYA, name="Gmail",
                       values={"app_password": password},
                       resolve=lambda host, port: [mailrig.PUBLIC_IP])

    [found] = [a for a in connectors.accounts(conn, "calendar") if a.key == key]
    free = calendars.free_slots(conn, "2026-11-03")
    made = asyncio.run(tools._do_create_event(summary="Dentist", start="2026-11-04T14:00",
                                              account="Gmail"))

    assert isinstance(found.provider, caldav.CalDavCalendar)
    assert [(s["start"][11:16], s["end"][11:16]) for s in free["free"]] == [
        ("09:00", "10:00"), ("11:00", "18:00")]
    assert made.get("created") is True, made
    on_server = found.provider.events(dt.datetime(2026, 11, 4, tzinfo=dt.UTC),
                                      dt.datetime(2026, 11, 5, tzinfo=dt.UTC), limit=5)
    assert [(e.title, e.start) for e in on_server] == [
        ("Dentist", dt.datetime(2026, 11, 4, 14, tzinfo=dt.UTC))]
    assert len(rig.world.snapshot("calendar")) == 2


# --- setup ------------------------------------------------------------------------

def _add(conn, *, preset: str, address: str = MAYA, **values):
    modules.register(conn)
    return accounts.add(conn, module_id="imap", address=address, preset=preset,
                        values={"password": "secret-pw", **values},
                        resolve=lambda host, port: [mailrig.PUBLIC_IP])


def test_other_joins_the_calendar_class_only_with_a_calendars_address(tmp_path):
    """A preset's calendar is offered; "Other" has one only where the user
    gives its address, whose server is then pinned as a typed one is."""
    conn = db.open_store(tmp_path / "setup.db")
    servers = {"imap": "imap.mail.example:993", "smtp": "smtp.mail.example:465"}

    mail_only = _add(conn, preset="other", **servers)
    both = _add(conn, preset="other", address="maya@home.example", url=f"https://{HOST}/dav/",
                **servers)

    rows = {a.key: a for a in modules.accounts(conn, live_only=False)}
    assert rows[mail_only].classes == ("mail",)
    assert rows[both].classes == ("mail", "calendar")
    assert rows[both].fields["hosts"][HOST] == "public"


def test_a_calendar_address_typed_where_the_preset_has_one_is_refused(tmp_path):
    conn = db.open_store(tmp_path / "setup.db")

    with pytest.raises(accounts.AccountError, match="already knows its servers"):
        _add(conn, preset="fastmail", url="https://calendar.elsewhere.example/")
