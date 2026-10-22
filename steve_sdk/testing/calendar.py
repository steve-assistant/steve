"""The calendar class's contract: what every calendar provider must do, against its fake.

One suite for every provider, every server's habits and every template
(05-connectors.md, section 8), run through core's write path, which the
caller passes in (`core.writes`). Each check takes a `CalendarCase` and
raises AssertionError on a broken contract; a test file parametrizes over
`CHECKS`.

They are the cases that went wrong before, or would on a calendar: a
server that answers with every event whatever the range or the filter
asked, a weekly meeting shown once, a new event whose id from a listing is
not the one it was made with, a change or a delete aimed at one of two
events alike, and an event changed on the phone between the read and the
write. A provider that cannot invite guests must not write an event that
has them, since a server may email them about it.
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from steve_sdk import classes
from steve_sdk.classes import CalendarProvider, Event, Guest
from steve_sdk.errors import NeedsCard, TargetChanged, Unsupported
from steve_sdk.testing import World, changed

# A Monday in the suite's own week, so no check depends on today's date.
MONDAY = dt.datetime(2026, 11, 2, tzinfo=dt.UTC)


@dataclass
class CalendarCase:
    provider: CalendarProvider
    world: World
    writes: Any                       # core.writes, the one write path
    address: str                      # the account's own address
    other: str = "ada@else.example"   # someone with whom it shares events


def _at(day: int, hour: int, minutes: int = 0) -> dt.datetime:
    return MONDAY + dt.timedelta(days=day, hours=hour, minutes=minutes)


def _seed(case: CalendarCase, *items: dict) -> list[str]:
    return case.world.seed("calendar", list(items))


def _refused(what: str, write: Callable[[], Any], *errors: type[Exception]) -> None:
    try:
        write()
    except errors:
        return
    raise AssertionError(f"{what} went ahead")


def reads_the_range_and_only_the_range(case: CalendarCase) -> None:
    _seed(case,
          {"title": "Plot meeting", "start": _at(0, 10), "end": _at(0, 11)},
          {"title": "Choir", "start": _at(1, 15), "end": _at(1, 16)},
          {"title": "Next week", "start": _at(7, 10), "end": _at(7, 11)})
    found = case.provider.events(_at(0, 0), _at(2, 0), limit=50)
    assert sorted(e.title for e in found) == ["Choir", "Plot meeting"], found
    for event in found:
        again = case.provider.fetch(event.native_id)
        assert again is not None and again.ident == event.ident and again.title == event.title


def repeats_are_read_as_their_occurrences(case: CalendarCase) -> None:
    _seed(case, {"title": "Rota", "start": _at(0, 9), "end": _at(0, 10),
                 "rrule": "FREQ=WEEKLY;COUNT=4"})
    found = case.provider.events(_at(6, 0), _at(16, 0), limit=50)
    assert [e.start for e in sorted(found, key=lambda e: e.start)] == [_at(7, 9), _at(14, 9)], \
        found
    assert all(e.recurring for e in found) and len({e.ident for e in found}) == 1
    assert len({e.native_id for e in found}) == 2, "each occurrence has its own id"
    one = case.provider.fetch(found[0].native_id)
    assert one is not None and one.start == found[0].start


def a_new_event_is_found_again_by_its_own_id(case: CalendarCase) -> None:
    before = case.world.snapshot("calendar")
    made = case.writes.create(case.provider, Event(native_id="", title="Dentist",
                                                   start=_at(2, 14), end=_at(2, 15)))
    after = case.world.snapshot("calendar")
    assert made.ident and changed(before, after) == {made.ident}, changed(before, after)
    listed = [e for e in case.provider.events(_at(2, 0), _at(3, 0), limit=50)
              if e.ident == made.ident]
    assert [e.native_id for e in listed] == [made.native_id], \
        "a listing names the new event by the id it was made with"
    assert listed[0].start == _at(2, 14) and listed[0].title == "Dentist"


def of_two_events_alike_only_the_one_named_changes(case: CalendarCase) -> None:
    ids = _seed(case, *[{"title": "Lunch", "start": _at(3, 12), "end": _at(3, 13)}] * 2)
    one, two = (case.provider.fetch(i) for i in ids)
    before = case.world.snapshot("calendar")

    def later(event: Event) -> Event:
        event.start, event.end = _at(3, 13), _at(3, 14)
        return event

    # Unconditional, the change goes on the user's yes to its card; a
    # conditional one needs none.
    card = None if case.writes.conditional(case.provider, one) else one.digest()
    case.writes.update(case.provider, one.native_id, later, card=card)
    assert changed(before, case.world.snapshot("calendar")) == {one.ident}
    assert case.provider.fetch(one.native_id).start == _at(3, 13)
    assert case.provider.fetch(two.native_id).start == _at(3, 12)


def of_two_events_alike_only_the_one_named_is_deleted(case: CalendarCase) -> None:
    ids = _seed(case, *[{"title": "Lunch", "start": _at(3, 12), "end": _at(3, 13)}] * 2)
    one, two = (case.provider.fetch(i) for i in ids)
    before = case.world.snapshot("calendar")
    case.writes.delete(case.provider, one.native_id, card=one.digest())
    assert changed(before, case.world.snapshot("calendar")) == {one.ident}
    assert case.provider.fetch(one.native_id) is None
    assert case.provider.fetch(two.native_id) is not None


def a_change_made_elsewhere_since_the_read_is_not_overwritten(case: CalendarCase) -> None:
    if not case.provider.has(classes.CONDITIONAL):
        return
    [native] = _seed(case, {"title": "Swim", "start": _at(4, 7), "end": _at(4, 8)})
    read = case.provider.fetch(native)
    case.world.mutate("calendar", native, title="Swim, moved on the phone")
    mine = copy.deepcopy(read)
    mine.title = "Swim with Ada"
    before = case.world.snapshot("calendar")
    _refused("a change over one made elsewhere", lambda: case.provider.update(mine),
             TargetChanged)
    _refused("a delete of an event changed elsewhere", lambda: case.provider.delete(read),
             TargetChanged)
    assert case.world.snapshot("calendar") == before
    assert case.provider.fetch(native).title == "Swim, moved on the phone"


def without_a_condition_a_change_asks_first(case: CalendarCase) -> None:
    [native] = _seed(case, {"title": "Swim", "start": _at(4, 7), "end": _at(4, 8)})
    if case.writes.conditional(case.provider, case.provider.fetch(native)):
        return
    before = case.world.snapshot("calendar")
    _refused("an unconditional change without a card",
             lambda: case.writes.update(case.provider, native, lambda e: e), NeedsCard)
    assert case.world.snapshot("calendar") == before


def an_event_with_guests_is_not_written_where_they_cannot_be_told(case: CalendarCase) -> None:
    if case.provider.has(classes.GUESTS):
        return
    [native] = _seed(case, {"title": "Plot rota", "start": _at(4, 18), "end": _at(4, 19),
                            "organizer": case.other, "guests": [case.other, case.address]})
    event = case.provider.fetch(native)
    assert [g.email for g in event.guests] == [case.other], "the user is not their own guest"
    before = case.world.snapshot("calendar")

    def renamed(e: Event) -> Event:
        e.title = "Plot rota (moved)"
        return e

    _refused("a change to an event with guests",
             lambda: case.writes.update(case.provider, native, renamed,
                                        card=event.digest()), Unsupported)
    _refused("a delete of an event with guests",
             lambda: case.writes.delete(case.provider, native, card=event.digest()),
             Unsupported)
    _refused("an invitation", lambda: case.writes.create(case.provider, Event(
        native_id="", title="Tea", start=_at(5, 16), end=_at(5, 17),
        guests=(Guest(email=case.other),))), Unsupported)
    assert case.world.snapshot("calendar") == before


def a_calendar_read_only_here_takes_no_write(case: CalendarCase) -> None:
    """A calendar link, or any provider that declares no write: each write
    is refused by the provider itself, even with the user's yes."""
    if case.provider.capabilities & {classes.CREATE, classes.UPDATE, classes.DELETE}:
        return
    [native] = _seed(case, {"title": "Rota", "start": _at(0, 9), "end": _at(0, 10)})
    before = case.world.snapshot("calendar")
    event = case.provider.fetch(native)
    assert event is not None
    _refused("adding an event", lambda: case.writes.create(case.provider, Event(
        native_id="", title="Dentist", start=_at(1, 9), end=_at(1, 10))), Unsupported)
    _refused("a change", lambda: case.writes.update(
        case.provider, native, lambda e: e,
        card=event.digest()), Unsupported)
    _refused("a delete", lambda: case.writes.delete(case.provider, native,
                                                    card=event.digest()), Unsupported)
    assert case.world.snapshot("calendar") == before


# What a calendar that is only read must do: a calendar link runs these.
READS: tuple[Callable[[CalendarCase], None], ...] = (
    reads_the_range_and_only_the_range,
    repeats_are_read_as_their_occurrences,
    a_calendar_read_only_here_takes_no_write,
)

CHECKS: tuple[Callable[[CalendarCase], None], ...] = (
    reads_the_range_and_only_the_range,
    repeats_are_read_as_their_occurrences,
    a_new_event_is_found_again_by_its_own_id,
    of_two_events_alike_only_the_one_named_changes,
    of_two_events_alike_only_the_one_named_is_deleted,
    a_change_made_elsewhere_since_the_read_is_not_overwritten,
    without_a_condition_a_change_asks_first,
    an_event_with_guests_is_not_written_where_they_cannot_be_told,
)
