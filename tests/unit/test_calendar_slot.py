"""Every calendar the user has, read as one and changed where the event is.

Until step 5 the calendar tools spoke to Google, and Outlook's events were
read beside them by name. On the slot each calendar is an account: a listing
asks them all, an id goes back to the account that handed it out, and a
change is made only where it can be -- on condition, or on the user's yes.
These hold that through the real tools, cards and write path, against
calendars that are dicts (tests/gate/slotfake.py):

- an event is changed on the calendar it is on, and a new one goes on the
  calendar named, or else the first that can take it;
- a calendar that can only be read refuses, and says where to change it;
- a yes is to the event as its card showed it: moved since, it is not
  changed, and the user is asked again;
- on a calendar that cannot write on condition, every change asks first;
- free time is time no calendar has taken, and an event on two calendars
  is listed once, with the guests the invitation had.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agent import actions, calendars, runtime
from agent import approvals as ap
from agent.toolset import calendar
from agent.turn import Deps
from core import clock, db
from steve_sdk import classes
from steve_sdk.classes import Event, Guest
from tests.gate import decisions, slotfake

EASTERN = ZoneInfo("America/New_York")
EVERYTHING_BUT_CONDITIONAL = frozenset(
    {classes.CREATE, classes.UPDATE, classes.DELETE, classes.GUESTS})


@pytest.fixture(autouse=True)
def _in_pittsburgh(monkeypatch):
    monkeypatch.setattr(clock, "local_zone", lambda: EASTERN)


def _event(native, title, start, end, *guests: str, **extra) -> Event:
    return Event(native_id=native, title=title, start=slotfake.at(start),
                 end=slotfake.at(end), guests=tuple(Guest(email=g) for g in guests), **extra)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A turn's context, the real cards, and `call(tool, **args)`."""
    conn = db.open_store(tmp_path / "calendar.db")
    monkeypatch.setattr(runtime, "conn", conn)
    shown: list[str] = []

    async def notify(text, **_):
        shown.append(text)
        return True

    monkeypatch.setattr(ap, "_notify", notify)
    monkeypatch.setattr(ap, "_execute_tool", actions.perform)
    monkeypatch.setattr(ap, "HOLD_SECONDS", 0)
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
    ctx = SimpleNamespace(deps=Deps(conn=conn, session_id=int(session), channel="signal"))
    tools: dict = {}
    calendar.register(lambda fn: tools.setdefault(fn.__name__, fn))

    async def call(name: str, **args):
        said = await tools[name](ctx, **args)
        await ap.asked()
        return said

    yield SimpleNamespace(conn=conn, shown=shown, call=call)
    conn.close()


async def _approve(world) -> dict:
    [card] = ap.pending(world.conn)
    ap.decide(world.conn, card.id, approve=True, by="signal")
    said = await ap.execute(world.conn, card.id, hold=False)
    await ap.asked()
    return said


# ---------------------------------------------------------------------------
# Where a change goes
# ---------------------------------------------------------------------------


async def test_an_event_on_a_second_calendar_is_changed_on_that_calendar(world, monkeypatch):
    home = slotfake.Calendar(_event("h1", "Dentist", "2026-10-15T15:00", "2026-10-15T15:45"))
    work = slotfake.Calendar(_event("w1", "Dentist", "2026-10-15T15:00", "2026-10-15T15:45"))
    accounts = slotfake.plug(monkeypatch, "calendar", google=home, outlook=work)

    await world.call("update_event", event_id=accounts["outlook"].id("w1"),
                     start="2026-10-15T17:00")

    assert home.writes == [], "the other calendar's event of the same name is untouched"
    [(what, moved)] = work.writes
    assert what == "update" and moved.start == slotfake.at("2026-10-15T17:00")
    assert moved.end == slotfake.at("2026-10-15T17:45"), "it kept its length"


async def test_a_new_event_goes_on_the_calendar_named_or_else_the_first_that_can_take_it(
        world, monkeypatch):
    shared = slotfake.Calendar(capabilities=frozenset())
    home, work = slotfake.Calendar(), slotfake.Calendar()
    slotfake.plug(monkeypatch, "calendar", shared=shared, google=home, outlook=work)

    await world.call("create_event", summary="Haircut", start="2026-10-16T09:00")
    await world.call("create_event", summary="Standup", start="2026-10-16T10:00",
                     account="outlook")

    assert [e.title for _, e in home.writes] == ["Haircut"], "the first that can add events"
    assert [e.title for _, e in work.writes] == ["Standup"]
    assert shared.writes == []


async def test_a_calendar_that_is_named_but_not_connected_is_refused_up_front(
        world, monkeypatch):
    home = slotfake.Calendar()
    slotfake.plug(monkeypatch, "calendar", google=home)

    said = await world.call("create_event", summary="Standup", start="2026-10-16T10:00",
                            account="work")

    assert said["ok"] is False and said["kind"] == "usage"
    assert home.writes == [] and ap.pending(world.conn) == []


async def test_a_calendar_that_can_only_be_read_refuses_and_says_where_to_change_it(
        world, monkeypatch):
    shared = slotfake.Calendar(_event("s1", "Board meeting", "2026-10-15T09:00",
                                      "2026-10-15T10:00"), capabilities=frozenset())
    found = slotfake.plug(monkeypatch, "calendar", shared=shared)["shared"]

    moved = await world.call("update_event", event_id=found.id("s1"), start="2026-10-15T11:00")
    gone = await world.call("delete_event", event_id=found.id("s1"))

    for refused in (moved, gone):
        assert refused["ok"] is False and "shared calendar" in refused["error"]
        assert "change it there" in refused["error"]
    assert shared.writes == [] and ap.pending(world.conn) == []


async def test_an_event_with_guests_on_a_calendar_that_cannot_tell_them_is_refused_before_a_card(
        world, monkeypatch):
    """A calendar by password cannot invite or tell anyone (CalDAV,
    05-connectors.md, section 4). Asked first, the user's yes would only
    meet the account's refusal: a new event with a guest did, until step
    10's calendar scenario (19 October 2026)."""
    home = slotfake.Calendar(
        _event("h1", "Plot rota", "2026-10-15T18:00", "2026-10-15T19:00", "ada@else.example"),
        capabilities=frozenset({classes.CREATE, classes.UPDATE, classes.DELETE,
                                classes.CONDITIONAL}))
    found = slotfake.plug(monkeypatch, "calendar", home=home)["home"]

    moved = await world.call("update_event", event_id=found.id("h1"), start="2026-10-15T20:00")
    gone = await world.call("delete_event", event_id=found.id("h1"))
    made = await world.call("create_event", summary="Plot supper", start="2026-10-16T18:00",
                            attendees=["ada@else.example"], account="home")

    for refused in (moved, gone):
        assert refused["ok"] is False and "cannot tell them" in refused["error"], refused
        assert "home" in refused["error"]
    assert made.get("ok") is False and "cannot invite anyone" in made["error"], made
    assert "home" in made["error"]
    assert home.writes == [] and ap.pending(world.conn) == [] and world.shown == []


# ---------------------------------------------------------------------------
# A yes is to what the card showed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capabilities", [None, EVERYTHING_BUT_CONDITIONAL],
                         ids=["on condition", "unconditional"])
async def test_an_event_moved_since_its_card_is_not_changed_and_is_asked_about_again(
        world, monkeypatch, capabilities):
    """The user said yes to moving the review from 10:00 to 15:00. Its
    organiser moved it to 11:00 meanwhile: the yes was to the 10:00 one."""
    held = slotfake.Calendar(
        _event("r1", "Review", "2026-10-15T10:00", "2026-10-15T11:00", "dana@example.com"),
        capabilities=capabilities)
    found = slotfake.plug(monkeypatch, "calendar", google=held)["google"]

    await world.call("update_event", event_id=found.id("r1"), start="2026-10-15T15:00")
    held.edit("r1", start=slotfake.at("2026-10-15T11:00"), end=slotfake.at("2026-10-15T12:00"))
    said = await _approve(world)

    assert said["ok"] is False and held.writes == []
    assert held.items["r1"].start == slotfake.at("2026-10-15T11:00"), "the organiser's move stands"
    [again] = ap.pending(world.conn)
    assert again.tool == "update_event" and "changed after you said yes" in again.preview


@pytest.mark.parametrize("tool", ["update_event", "delete_event"])
async def test_another_event_in_the_place_of_the_one_carded_is_not_changed(
        world, monkeypatch, tool):
    """The card showed the review, whose UID is review@x. Before the yes,
    its id came to name another event alike to it -- the same title, times
    and guest, so the same digest -- and the write went by the id and the
    digest alone: the write path checks an item's UID only against one it
    is given, and no tool gave the card's. The card binds it now, and the
    event there is asked about as it is."""
    held = slotfake.Calendar(_event("r1", "Review", "2026-10-15T10:00", "2026-10-15T11:00",
                                    "dana@example.com", ident="review@x"))
    found = slotfake.plug(monkeypatch, "calendar", google=held)["google"]
    if tool == "update_event":
        await world.call(tool, event_id=found.id("r1"), start="2026-10-15T15:00")
    else:
        await world.call(tool, event_id=found.id("r1"))
    [card] = ap.pending(world.conn)
    assert card.args["ident"] == "review@x"

    held.edit("r1", ident="other@x")
    said = await _approve(world)

    assert said["ok"] is False and held.writes == []
    [again] = ap.pending(world.conn)
    assert again.tool == tool and again.args["ident"] == "other@x"


class _Synced(slotfake.Calendar):
    """A calendar that lists an event's neighbours, as CalDAV does, and
    whose other event `synced` the user's phone changes as a write lands."""

    def __init__(self, *events: Event, synced: str) -> None:
        super().__init__(*events)
        self.synced = synced

    def neighbours(self, item):
        return {e.ident: e.condition for e in self.items.values()}

    def update(self, event: Event) -> Event:
        written = super().update(event)
        self.edit(self.synced, title="Gym with Ada")
        return written


async def test_a_change_that_went_is_said_as_written_and_never_asked_again(world, monkeypatch):
    """The user said yes to moving the review, and it moved; their phone
    synced the gym hour in the same second, so the check after the write
    found more changed than the one event. They were told nothing was
    written, and asked again: a second yes would have made the change twice."""
    held = _Synced(
        _event("r1", "Review", "2026-10-15T10:00", "2026-10-15T11:00", "dana@example.com",
               ident="review@x"),
        _event("g1", "Gym", "2026-10-15T07:00", "2026-10-15T08:00", ident="gym@x"),
        synced="g1")
    found = slotfake.plug(monkeypatch, "calendar", google=held)["google"]

    await world.call("update_event", event_id=found.id("r1"), start="2026-10-15T15:00")
    said = await _approve(world)

    assert held.items["r1"].start == slotfake.at("2026-10-15T15:00"), "the move went"
    assert said["ok"] is False and "nothing was written" not in said["error"]
    assert "not to be made again" in said["error"]
    assert ap.pending(world.conn) == [], "a yes would move it a second time"


@pytest.mark.parametrize("jev", ["ask", {"go": 0.55, "ask": 0.45}, "unreachable"])
async def test_a_change_on_a_calendar_that_cannot_write_on_condition_asks_unless_jev_is_sure(
        world, monkeypatch, jev):
    """A private event moves at once where the write can be conditional on
    it being as read. Where it cannot, the user's yes to a card showing it
    is that condition (core/consent.yaml, `unconditional`), unless Jev is
    sure asking is not needed (core/lostupdate.py): not sure, or not there
    to ask, it asks."""
    if jev == "unreachable":
        decisions.install(monkeypatch, down=RuntimeError("Jev is down"))
    else:
        decisions.install(monkeypatch, lost_update=jev)
    monkeypatch.setattr("core.secrets.get", lambda name: "test-key")
    held = slotfake.Calendar(_event("p1", "Gym", "2026-10-15T07:00", "2026-10-15T08:00"),
                             capabilities=EVERYTHING_BUT_CONDITIONAL)
    found = slotfake.plug(monkeypatch, "calendar", google=held)["google"]

    await world.call("update_event", event_id=found.id("p1"), start="2026-10-15T18:00")

    assert held.writes == [], "nothing changed before the yes"
    [card] = ap.pending(world.conn)
    assert card.tool == "update_event" and "Gym" in card.preview

    said = await _approve(world)

    assert said["ok"] is True
    [(what, moved)] = held.writes
    assert what == "update" and moved.start == slotfake.at("2026-10-15T18:00")


async def test_a_change_jev_is_sure_of_goes_at_once_on_a_calendar_that_cannot_write_on_condition(
        world, monkeypatch):
    """The user asked to move their gym hour, and nothing suggests anyone
    else is editing it: asking them to confirm what they just asked for,
    for want of an etag, was the card Jev now judges away."""
    jev = decisions.install(monkeypatch, lost_update={"go": 0.9, "ask": 0.1})
    monkeypatch.setattr("core.secrets.get", lambda name: "test-key")
    held = slotfake.Calendar(_event("p1", "Gym", "2026-10-15T07:00", "2026-10-15T08:00"),
                             capabilities=EVERYTHING_BUT_CONDITIONAL)
    found = slotfake.plug(monkeypatch, "calendar", google=held)["google"]

    said = await world.call("update_event", event_id=found.id("p1"), start="2026-10-15T18:00")

    assert said.get("updated") is True and ap.pending(world.conn) == []
    [(what, moved)] = held.writes
    assert what == "update" and moved.start == slotfake.at("2026-10-15T18:00")
    [shown] = jev.inputs
    assert "Gym" in shown and "has not made or changed this item" in shown


# ---------------------------------------------------------------------------
# Read as one
# ---------------------------------------------------------------------------


def test_free_time_is_time_no_calendar_has_taken(monkeypatch):
    slotfake.plug(
        monkeypatch, "calendar",
        google=slotfake.Calendar(_event("g1", "Lecture", "2026-10-15T10:00",
                                        "2026-10-15T11:00")),
        outlook=slotfake.Calendar(
            _event("o1", "Review", "2026-10-15T13:00", "2026-10-15T14:30"),
            _event("o2", "All hands", "2026-10-15T16:00", "2026-10-15T17:00",
                   extra={"my_response": "declined"})))

    said = calendars.free_slots(None, "2026-10-15")

    assert [(s["start"][11:16], s["end"][11:16]) for s in said["free"]] == [
        ("09:00", "10:00"), ("11:00", "13:00"), ("14:30", "18:00")], \
        "both calendars' events taken out; the one they declined is free"


def test_an_invitation_copied_by_hand_onto_another_calendar_is_listed_once(monkeypatch):
    """The invitation is on the work calendar, which can only be read; their
    own copy has no guests. Listed once: as the copy the tools can change,
    with the invitation's guests."""
    soon = dt.datetime.now(EASTERN).replace(second=0, microsecond=0) + dt.timedelta(hours=1)
    later = soon + dt.timedelta(hours=1)
    accounts = slotfake.plug(
        monkeypatch, "calendar",
        work=slotfake.Calendar(Event(native_id="inv", title="Contoso review", start=soon,
                                     end=later, guests=(Guest(email="dana@example.com"),)),
                               capabilities=frozenset()),
        google=slotfake.Calendar(Event(native_id="mine", title="Contoso review",
                                       start=soon, end=later)))

    [listed] = calendars.agenda(None)["events"]

    assert listed["id"] == accounts["google"].id("mine")
    assert listed["also_on"] == "work"
    assert [a["email"] for a in listed["attendees"]] == ["dana@example.com"]
    assert "calendar" not in listed, "it is on their main calendar"


def test_a_room_on_an_event_is_not_listed_among_the_people_on_it(monkeypatch):
    """Who the user will be with is read off this list, by the attention
    loop among others; a room booked with the meeting is not a person to
    prepare for, from whichever account it comes."""
    soon = dt.datetime.now(EASTERN).replace(second=0, microsecond=0) + dt.timedelta(hours=1)
    slotfake.plug(monkeypatch, "calendar", work=slotfake.Calendar(Event(
        native_id="m", title="Planning", start=soon, end=soon + dt.timedelta(hours=1),
        guests=(Guest(email="dana@example.com"),
                Guest(email="larkspur@rooms.example", name="Larkspur 2400", room=True)))))

    [listed] = calendars.agenda(None)["events"]

    assert [a["email"] for a in listed["attendees"]] == ["dana@example.com"]
