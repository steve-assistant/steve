"""Google Calendar: read the schedule, create and change events.

Times are the whole difficulty here, in two ways.

Google returns either `dateTime` (a moment) or `date` (an all-day event), and
mixing them up turns "your flight is at 06:00" into "your flight is some time
on Tuesday". Both shapes are normalised on the way out and the distinction is
kept explicit.

More importantly, everything is resolved in the **machine's** timezone, not
the calendar's. Those are not the same thing and on this account they differ:
the Google Calendar timezone setting is UTC while the user is in
America/New_York, so a class stored as 14:05Z actually happens at 10:05 local.
Reporting the calendar's setting would be four hours wrong on every time, and
"schedule lunch at 1pm" would land at 08:00. The calendar's timezone is a
display preference in Google's web UI; where the user is sitting is a fact,
and the agent runs on their machine.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from steve_sdk.cut import keeping_links
from steve_sdk.errors import NotConnected

from .common import current, guard, service

# How much of an event's description a model is shown (with the links past
# that kept: `steve_sdk.cut.keeping_links`).
DESCRIPTION_CHARS = 1_000


def _calendar():
    return service("calendar", "v3")


def _tz() -> dt.tzinfo:
    """The timezone the user is actually in, from the account's context
    (`ctx.zone`), by its IANA name ("America/New_York") rather than a fixed
    offset, which matters for events on the far side of a DST transition: a
    fixed -04:00 would put every meeting after November an hour out."""
    account = current()
    if account is None:
        raise NotConnected("no Google account is in use here")
    return account.ctx.zone


def _local(value: str | None, tz: dt.tzinfo) -> str | None:
    """Render a Google timestamp in the user's timezone."""
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        return value
    return moment.astimezone(tz).isoformat()


def _format_event(event: dict, tz: dt.tzinfo | None = None) -> dict:
    tz = tz or _tz()
    start, end = event.get("start", {}), event.get("end", {})
    all_day = "date" in start
    return {
        "id": event.get("id"),
        "summary": event.get("summary", "(no title)"),
        # All-day events carry a plain date and must not be shifted; a moment
        # is converted so the user reads their own clock.
        "start": start.get("date") if all_day else _local(start.get("dateTime"), tz),
        "end": end.get("date") if all_day else _local(end.get("dateTime"), tz),
        "timezone": str(tz),
        # Kept explicit: an all-day event has no meaningful time-of-day, and
        # rendering one as midnight is how "your flight is at 06:00" becomes
        # "your flight is some time on Tuesday".
        "all_day": all_day,
        "location": event.get("location"),
        # Cut with its links kept (agent/cut.py): a join link sits at the
        # bottom of a long description.
        "description": keeping_links(event.get("description"), DESCRIPTION_CHARS),
        # `self` is Google's mark on their own entry, kept so a reader can
        # leave them out of "who is in the room" without knowing which of their
        # addresses the invitation went to.
        "attendees": [
            {"email": a.get("email"), "name": a.get("displayName"),
             "response": a.get("responseStatus"), "self": bool(a.get("self"))}
            for a in event.get("attendees", [])
        ],
        "organizer": (event.get("organizer") or {}).get("email"),
        "organizer_self": bool((event.get("organizer") or {}).get("self")),
        "link": event.get("htmlLink"),
        "status": event.get("status"),
        "recurring": bool(event.get("recurringEventId")),
    }


def _to_rfc3339(value: str | dt.datetime, tz: dt.tzinfo) -> str:
    # A bare "2026-09-21T14:00" means local time, not UTC.
    moment = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value)
    return (moment if moment.tzinfo else moment.replace(tzinfo=tz)).isoformat()


def _events(calendar_id: str = "primary", **query) -> list[dict]:
    """The events a listing finds, as the API gives them. Recurring events
    come as their occurrences (`singleEvents`), as every caller here wants;
    five of them spelled the call out in full."""
    return _calendar().events().list(
        calendarId=calendar_id, singleEvents=True, **query).execute().get("items", [])


# The decorator slid off this function once: two helpers were inserted
# between `@guard` and `def create`, so the first was guarded
# and `create` was not. When Google's sign-in expired on 26 September 2026,
# adding an event raised instead of reporting, and the whole turn died --
# "add Game Theory to my calendar" came back as "my laptop is offline".
# tests/unit/test_google_common.py now checks every public function here.
@guard
def create(
    summary: str,
    start: str,
    end: str | None = None,
    *,
    duration_minutes: int = 60,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    all_day: bool = False,
    private: bool = False,
    calendar_id: str = "primary",
) -> dict:
    """Create an event.

    `end` is optional: most of the time the user says "lunch at 1 for an
    hour", not "lunch from 1 to 2", so a duration is the more natural input
    and the end time is derived.
    """
    tz = _tz()

    if all_day:
        start_date = dt.datetime.fromisoformat(start).date()
        end_date = (
            dt.datetime.fromisoformat(end).date()
            if end
            else start_date + dt.timedelta(days=1)
        )
        body: dict[str, Any] = {
            "start": {"date": start_date.isoformat()},
            # Google treats the all-day end date as exclusive.
            "end": {"date": end_date.isoformat()},
        }
    else:
        start_iso = _to_rfc3339(start, tz)
        if end:
            end_iso = _to_rfc3339(end, tz)
        else:
            end_iso = (
                dt.datetime.fromisoformat(start_iso)
                + dt.timedelta(minutes=duration_minutes)
            ).isoformat()
        body = {
            "start": {"dateTime": start_iso, "timeZone": str(tz)},
            "end": {"dateTime": end_iso, "timeZone": str(tz)},
        }

    body["summary"] = summary
    if private:
        body["visibility"] = "private"
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees]

    created = (
        _calendar().events()
        .insert(
            calendarId=calendar_id,
            body=body,
            # Only notify when there is somebody to notify; an invite email
            # for an event with no guests is just noise to the user.
            sendUpdates="all" if attendees else "none",
        )
        .execute()
    )
    return {"created": True, **_format_event(created, tz)}


@guard
def update(
    event_id: str,
    *,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    calendar_id: str = "primary",
    etag: str | None = None,
) -> dict:
    """Change an existing event. Only the fields given are touched. With
    `etag`, only if the event is still as it was read (`_if_match`)."""
    tz = _tz()
    patch: dict[str, Any] = {}
    if summary is not None:
        patch["summary"] = summary
    if description is not None:
        patch["description"] = description
    if location is not None:
        patch["location"] = location
    if start:
        patch["start"] = {"dateTime": _to_rfc3339(start, tz), "timeZone": str(tz)}
    if end:
        patch["end"] = {"dateTime": _to_rfc3339(end, tz), "timeZone": str(tz)}
    if not patch:
        return {"error": "nothing to update", "kind": "usage"}

    updated = _if_match(
        _calendar().events().patch(calendarId=calendar_id, eventId=event_id, body=patch),
        etag).execute()
    return {"updated": True, **_format_event(updated, tz)}


@guard
def delete(event_id: str, *, calendar_id: str = "primary", notify: bool = False,
           etag: str | None = None) -> dict:
    """Remove an event. `notify` sends its guests the cancellation -- without
    it they keep a meeting the user has quietly dropped. With `etag`, only if
    the event is still as it was read (`_if_match`)."""
    _if_match(_calendar().events().delete(
        calendarId=calendar_id, eventId=event_id, sendUpdates="all" if notify else "none"
    ), etag).execute()
    return {"deleted": True, "id": event_id}


def _if_match(request, etag: str | None):
    """`request`, made conditional on the event's `etag`: Google refuses it
    (412) if the event changed since it was read. Until step 5 a change and a
    delete went by id alone, and landed on whatever the event had become."""
    if etag:
        request.headers["If-Match"] = etag
    return request

