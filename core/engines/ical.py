"""Calendar data as both calendar engines read it: a CalDAV server's events
and a calendar link's feed are the same iCalendar text.

The personal edition's course-feed reader parsed that text by hand, and an
event whose time named a Windows zone ("Eastern Standard Time", as Outlook
and Exchange publish it) broke it, so a whole feed read as nothing. Here
`icalendar` reads the text and maps Windows' zone names to the IANA ones,
and `recurring-ical-events` expands repeats with their exceptions and moved
occurrences. A zone neither knows is read as the user's own and reported
by `unknown_zones` for the health check to name, never raised: one odd
event must not hide the rest. One reading for both engines, so a fix to
one is a fix to both.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

import icalendar
import recurring_ical_events

from steve_sdk.classes import Event, Guest
from steve_sdk.errors import ConnectorError

UTC = dt.UTC
# How far around an occurrence's start a series is expanded to find that
# occurrence again: a moved one may sit a day or so from where it was.
AROUND = dt.timedelta(days=2)


def parse(text: str, what: str) -> Any:
    """A VCALENDAR, or ConnectorError saying `what` could not be read."""
    try:
        parsed = icalendar.Calendar.from_ical(text)
    except ValueError as exc:
        raise ConnectorError(f"{what} could not be read: {exc}"[:300]) from exc
    if getattr(parsed, "name", "") != "VCALENDAR":
        raise ConnectorError(f"{what} is not a calendar")
    return parsed


def stamp(moment: dt.datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def text(component: Any, name: str) -> str:
    value = component.get(name)
    return str(value).strip() if value is not None else ""


def email(value: Any) -> str:
    found = str(value or "").strip()
    return found[7:] if found.lower().startswith("mailto:") else found


def moment(value: Any, zone: dt.tzinfo) -> dt.datetime | dt.date | None:
    """A DTSTART or DTEND as Python's: a date for an all-day event, an
    aware time otherwise. A floating time is the user's own zone, and so is
    one in a zone nobody knows (`unknown_zones` names those)."""
    found = getattr(value, "dt", value)
    if isinstance(found, dt.datetime):
        return found if found.tzinfo is not None else found.replace(tzinfo=zone)
    if isinstance(found, dt.date):
        return found
    return None


def unknown_zones(parsed: Any) -> set[str]:
    """The zones the calendar's times name that neither `icalendar` nor its
    own VTIMEZONEs define: those times come back floating, with the name
    still on them."""
    found = set()
    for component in parsed.walk("VEVENT"):
        for name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            value = component.get(name)
            zone = getattr(value, "params", {}).get("TZID") if value is not None else None
            when = getattr(value, "dt", None)
            if zone and isinstance(when, dt.datetime) and when.tzinfo is None:
                found.add(str(zone))
    return found


def repeats(components: Iterable[Any]) -> bool:
    return any(c.get("RRULE") is not None or c.get("RDATE") is not None
               or c.get("RECURRENCE-ID") is not None for c in components)


def occurrence(component: Any, repeating: bool) -> str:
    """Which occurrence of a series a component is, as text (a UTC stamp,
    or a date for an all-day series); "" for an event that does not repeat."""
    if not repeating:
        return ""
    found = getattr(component.get("RECURRENCE-ID"), "dt", None)
    if isinstance(found, dt.datetime):
        return stamp(found if found.tzinfo else found.replace(tzinfo=UTC))
    return found.isoformat() if isinstance(found, dt.date) else ""


def between(parsed: Any, start: dt.datetime, end: dt.datetime) -> list[Any]:
    """Every occurrence overlapping [start, end), repeats expanded."""
    try:
        return list(recurring_ical_events.of(parsed).between(start, end))
    except (ValueError, TypeError) as exc:
        raise ConnectorError(f"an event's repeats could not be read: {exc}"[:300]) from exc


def around(parsed: Any, which: str, zone: dt.tzinfo) -> list[Any]:
    """The occurrences near the one `which` names (`occurrence`'s text):
    what finding one occurrence of a series again reads. None if `which`
    is not such a text."""
    try:
        at = (dt.datetime.strptime(which, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
              if "T" in which else
              dt.datetime.combine(dt.date.fromisoformat(which), dt.time(), tzinfo=zone))
    except ValueError:
        return []
    return between(parsed, at - AROUND, at + AROUND)


def event(c: Any, *, zone: dt.tzinfo, native_id: str, calendar: str, repeating: bool,
          condition: str | None = None, own: str = "") -> Event:
    """One VEVENT, or one occurrence of it, as the SDK's Event. `own` is
    the account's address, left out of the guests."""
    begins = moment(c.get("DTSTART"), zone)
    ends = moment(c.get("DTEND"), zone)
    if ends is None and begins is not None and c.get("DURATION") is not None:
        ends = begins + c.get("DURATION").dt
    attendees = c.get("ATTENDEE")
    attendees = attendees if isinstance(attendees, list) else (
        [attendees] if attendees is not None else [])
    mine = own.lower()
    guests = tuple(
        Guest(email=email(a), name=str(a.params.get("CN", "")),
              response=str(a.params.get("PARTSTAT", "")).lower(),
              room=str(a.params.get("CUTYPE", "")).upper() in ("ROOM", "RESOURCE"))
        for a in attendees if not mine or email(a).lower() != mine)
    touched = c.get("LAST-MODIFIED") or c.get("DTSTAMP")
    uid = text(c, "UID")
    return Event(
        native_id=native_id, ident=uid, condition=condition,
        title=text(c, "SUMMARY"), start=begins, end=ends,
        all_day=begins is not None and not isinstance(begins, dt.datetime),
        location=text(c, "LOCATION"), description=text(c, "DESCRIPTION"),
        guests=guests, organizer=email(c.get("ORGANIZER")), calendar=calendar,
        link=text(c, "URL"), recurring=repeating, status=text(c, "STATUS").lower(),
        busy=text(c, "TRANSP").upper() != "TRANSPARENT",
        extra={**({"updated": touched.dt.isoformat()} if touched is not None else {}),
               **({"series": uid} if repeating else {})})
