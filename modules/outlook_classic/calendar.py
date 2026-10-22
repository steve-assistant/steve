"""Their Outlook calendar, read locally through the Outlook that is already signed in.

Where invitations from their organisation land, with their guest lists --
which the copies the user makes on their Google calendar never carry.
`agent/calendars.py` reads every calendar as one.

Its own module because it had grown to a quarter of `outlook.py` and
shares nothing with mail but the way in: `outlook._namespace()`, through the
account's context. The same credential rule holds here as there, and the
same gate test reads both files (`tests/gate/test_sent_mail_only.py`).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from datetime import UTC, datetime, timedelta

from steve_sdk.cut import keeping_links

from . import outlook

log = logging.getLogger("outlook")

# As modules/google_cloud/calendar.py: how much of a description a model is shown,
# with the links past that kept.
DESCRIPTION_CHARS = 1_000

FOLDER_CALENDAR = 9

# `olMeetingStatus`: their own appointment, a meeting the user sent, one they received,
# and the two cancelled states.
MEETING_NONE, MEETING_ORGANIZED, MEETING_RECEIVED = 0, 1, 3
MEETING_CANCELLED = frozenset({5, 7})

# A guest's answer (`olResponseStatus`), spelled as Google spells it.
_RESPONSE = {
    0: "needsAction", 1: "accepted", 2: "tentative", 3: "accepted", 4: "declined",
    5: "needsAction",
}
RECIPIENT_RESOURCE = 3  # a room

# Resolving a guest's address is an Exchange lookup each. The school's
# all-student invitations carry 352 guests -- seconds of COM for a list
# nobody prepares for -- so past this many none are read, only counted.
MAX_GUESTS_READ = 25

# `IncludeRecurrences` makes the collection endless (a series with no last
# date recurs forever), so the walk stops at the first entry past the window
# -- and here, whatever happens. The whole calendar was 24 entries to the end
# of this week in September 2026.
CALENDAR_SCAN = 5000

# The start in an entry's id, in UTC.
_STAMP = "%Y%m%dT%H%M%SZ"


def native_id(entry_id: str, start: datetime) -> str:
    """The id an entry is read again by (`entry`): its EntryID, which a
    series shares with each of its occurrences, and its start.

    It was a hash of the series and the start: it told the occurrences
    apart, but nothing could be found from it, so no entry could be read
    again by its id, as a calendar link's checks ask."""
    return f"{entry_id}@{start.astimezone(UTC).strftime(_STAMP)}"


def _moment(value, tz):
    """A MAPI `StartUTC`/`EndUTC` as an aware local time.

    The `UTC` properties, not `Start` and `End`: those come back as the local
    wall-clock time labelled UTC, which is four hours wrong once converted.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz)


def _stamp(value, tz) -> str | None:
    """A MAPI modification or creation time, local, via `outlook._epoch_ms`."""
    try:
        moment = datetime.fromtimestamp(outlook._epoch_ms(value) / 1000, tz)
        return moment.isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        return None


def _entry_smtp(entry) -> str:
    """An AddressEntry's real address. `outlook._smtp` is for a Recipient,
    which wraps one."""
    for resolve in (
        lambda: entry.Address,
        lambda: entry.GetExchangeUser().PrimarySmtpAddress,
        lambda: entry.PropertyAccessor.GetProperty(outlook.PR_SMTP),
    ):
        try:
            found = (resolve() or "").strip()
            if "@" in found:
                return found.lower()
        except Exception:  # noqa: BLE001
            continue
    return ""


def _person_name(display: str) -> str:
    """"Brook, Ada" as "Ada Brook", the way Google and mail write it."""
    display = (display or "").strip().strip('"')
    last, comma, first = display.partition(",")
    if comma and first.strip() and "," not in first and "@" not in display:
        return f"{first.strip()} {last.strip()}"
    return display


def _guests(item, mine: set[str]) -> tuple[list[dict], int]:
    """The guest list, as Google shapes it, and how many there are."""
    count = 0
    with contextlib.suppress(Exception):
        count = int(item.Recipients.Count)
    if count > MAX_GUESTS_READ:
        return [], count
    found = []
    for i in range(1, count + 1):
        try:
            entry = item.Recipients.Item(i)
            if int(getattr(entry, "Type", 1) or 1) == RECIPIENT_RESOURCE:
                continue
            email = outlook._smtp(entry)
            # The organiser can be on the list twice, as sender and guest.
            if not email or any(g["email"] == email for g in found):
                continue
            found.append({
                "email": email,
                "name": _person_name(getattr(entry, "Name", "")) or None,
                "response": _RESPONSE.get(int(getattr(entry, "MeetingResponseStatus", 0) or 0)),
                "self": email in mine,
            })
        except Exception:  # noqa: BLE001 - one odd guest is not the meeting
            continue
    return found, count


def _appointment(item, *, tz, mine: set[str]) -> dict | None:
    """One Outlook calendar entry, in the shape `google_calendar._format_event` returns.

    The description is kept only for entries the user made themselves. An invitation's
    body is an email someone else wrote, and received Outlook mail reaches a
    model as headers only (`tests/gate/test_sent_mail_only.py`); what a
    meeting is, who is in it, where and when, is the header here.
    """
    try:
        start = _moment(item.StartUTC, tz)
        end = _moment(item.EndUTC, tz)
        all_day = bool(item.AllDayEvent)
        status = int(getattr(item, "MeetingStatus", 0) or 0)
        subject = str(item.Subject or "") or "(no title)"
        entry_id = str(item.EntryID)
        series = str(getattr(item, "GlobalAppointmentID", "") or entry_id)
    except Exception as exc:  # noqa: BLE001 - one odd entry is not the calendar
        log.debug("skipped a calendar entry: %s", exc)
        return None

    organizer = ""
    with contextlib.suppress(Exception):
        organizer = _entry_smtp(item.GetOrganizer())
    their_own = status in (MEETING_NONE, MEETING_ORGANIZED) or (organizer and organizer in mine)
    guests, count = _guests(item, mine)
    description = ""
    if their_own:
        with contextlib.suppress(Exception):
            description = keeping_links(str(item.Body or ""), DESCRIPTION_CHARS)
    location = ""
    with contextlib.suppress(Exception):
        location = str(item.Location or "")

    return {
        "id": native_id(entry_id, start),
        "series": hashlib.sha1(series.encode()).hexdigest()[:16],
        "summary": subject,
        "start": start.date().isoformat() if all_day else start.isoformat(),
        "end": end.date().isoformat() if all_day else end.isoformat(),
        "timezone": str(tz),
        "all_day": all_day,
        "location": location or None,
        "description": description,
        "attendees": guests,
        "attendee_count": count,
        "organizer": organizer or None,
        "organizer_self": bool(their_own),
        "link": None,
        "status": "cancelled" if status in MEETING_CANCELLED else "confirmed",
        "recurring": bool(getattr(item, "IsRecurring", False)),
        "updated": _stamp(getattr(item, "LastModificationTime", None), tz),
        "created": _stamp(getattr(item, "CreationTime", None), tz),
        "calendar": "outlook",
    }


def _walk_calendar(folder, start: datetime, end: datetime, *, tz, mine: set[str],
                   match=None) -> list[dict]:
    """Entries overlapping [start, end), in start order.

    Iterated rather than `Restrict`ed, for the reason `outlook._walk_one`
    gives: the Jet date syntax is locale-dependent and a wrong one returns
    nothing, not an error. Sorted by start with `IncludeRecurrences`, so a weekly class is
    its occurrences rather than the one master item dated when it began.
    `match`, given an entry, says whether to build it at all -- cheap checks
    first, because building one resolves every guest's address.
    """
    items = folder.Items
    items.Sort("[Start]")
    items.IncludeRecurrences = True
    found: list[dict] = []
    scanned = 0
    item = items.GetFirst()
    while item is not None and scanned < CALENDAR_SCAN:
        scanned += 1
        try:
            item_start = _moment(item.StartUTC, tz)
            item_end = _moment(item.EndUTC, tz)
        except Exception:  # noqa: BLE001 - not an appointment
            item = items.GetNext()
            continue
        if item_start >= end:
            break
        if item_end > start and (match is None or match(item)):
            event = _appointment(item, tz=tz, mine=mine)
            if event is not None:
                found.append(event)
        item = items.GetNext()
    if scanned >= CALENDAR_SCAN:
        log.warning("calendar walk stopped at %d entries; results may be incomplete", scanned)
    return found


def calendars() -> int:
    """How many calendars Outlook has, one at most in each store: the test."""
    return len(outlook._roots(outlook._namespace(), FOLDER_CALENDAR))


def _mine(mapi) -> set[str]:
    """The addresses of the accounts Outlook holds: the user's own."""
    with contextlib.suppress(Exception):
        return {
            str(mapi.Accounts.Item(i).SmtpAddress).lower()
            for i in range(1, mapi.Accounts.Count + 1)
        }
    return set()


@outlook._safe
def calendar(start: datetime, end: datetime, *, limit: int = 100, match=None) -> dict:
    """Their Outlook calendar between two aware times, soonest first. Blocking."""
    # The user's zone, the account's context's: this module borrowed
    # Google's helper until 4f, and core's until item 10.
    tz = outlook._local_zone()
    mapi = outlook._namespace()
    mine = _mine(mapi)
    found: list[dict] = []
    for root in outlook._roots(mapi, FOLDER_CALENDAR):
        found += _walk_calendar(root, start, end, tz=tz, mine=mine, match=match)
    found.sort(key=lambda e: e["start"])
    return {"count": min(len(found), limit), "events": found[:limit]}


@outlook._safe
def entry(native: str) -> dict:
    """The one entry `native_id` named, as a read listed it, or none.

    `GetItemFromID` finds the entry. A series is one item for all its
    occurrences, so its occurrence is found as a read of that moment finds
    it: by walking the calendar there for the series' EntryID at the start
    the id holds. What a read listed is then what is found again, an
    occurrence moved in Outlook among them. Blocking."""
    entry_id, _, stamp = native.partition("@")
    tz = outlook._local_zone()
    mapi = outlook._namespace()
    try:
        item = mapi.GetItemFromID(entry_id)
    except Exception:  # noqa: BLE001 - deleted, or no Outlook id: no entry either way
        return {"event": None}
    mine = _mine(mapi)
    if not bool(getattr(item, "IsRecurring", False)):
        return {"event": _appointment(item, tz=tz, mine=mine)}
    try:
        start = datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return {"event": None}

    def match(found) -> bool:
        return str(found.EntryID) == entry_id and _moment(found.StartUTC, tz) == start

    # A second either side, so that an entry ending as it starts is in the walk.
    edge = timedelta(seconds=1)
    for root in outlook._roots(mapi, FOLDER_CALENDAR):
        for event in _walk_calendar(root, start - edge, start + edge, tz=tz, mine=mine,
                                    match=match):
            return {"event": event}
    return {"event": None}
