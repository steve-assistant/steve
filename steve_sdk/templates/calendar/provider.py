"""Example Calendar: the template for a calendar provider with a web API of its own.

To make a module of it, copy this folder to `modules/<id>/`, fill each
line of `manifest.toml` marked FILL, and rewrite each call below for the
service's API. Keep what each method returns and refuses: core's write
path (`core/writes.py`) relies on it, and the calendar contract suite
(`steve_sdk.testing.calendar`) checks it, against `fake.py` once that
answers as the service does (`steve_sdk/templates/__init__.py` says how).

Example's API, which this speaks (`API`):

- `GET /events?start=&end=&limit=&cursor=`: the events overlapping
  [start, end), each occurrence of a repeating event its own item, with its
  own id, the series' `uid`, and `series` naming the event it repeats.
  Without `start` and `end`, every event as stored, a repeating one once.
- `GET /events/{id}`: one event or occurrence; 404 once it is gone.
- `POST /events`: an event made. `PATCH /events/{id}`: one changed.
  `DELETE /events/{id}`: one deleted.
- Every event has a `version`. A write sends it as `If-Match`, and a 412
  says the event changed since. Times are UTC (`2026-11-02T10:00:00Z`); an
  all-day event's are dates, its end the day after.

What every copy keeps, beyond the mail template's rules (only `ctx.http`,
exactly the item named, a condition where the service has one, the
server's answer checked here, errors core can say):

- **No one is told anything by surprise.** An event with guests is not
  changed or deleted here, and none is made with them: a service may email
  each guest about it, and this provider does not declare GUESTS. A copy
  that does must be one whose service lets it say whether guests are told.
- **A repeating event is not changed here,** nor one of its occurrences:
  a change to the series moves every week, and some services change the
  series when asked to change one occurrence.
- **The range is checked again here,** on what came back: a service that
  drops its range filter answers with the whole calendar.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from steve_sdk import classes
from steve_sdk.classes import CalendarProvider, Event, Guest
from steve_sdk.context import Context
from steve_sdk.errors import (
    ConnectorError,
    NotConnected,
    TargetChanged,
    Transient,
    Unsupported,
    WrongPassword,
)

API = "https://api.example.com/v1"   # FILL: the service's API, on a host the manifest names
TIMEOUT_S = 30.0
PAGES = 10                           # a listing reads at most this many pages
ID = re.compile(r"[A-Za-z0-9._~-]{1,200}")


# --- the service --------------------------------------------------------------------

def call(ctx: Context, method: str, path: str, *, params: dict[str, str] | None = None,
         body: Any = None, version: str | None = None) -> Any:
    """One request: the JSON answered ({} for none), or None for a 404."""
    headers = {"Authorization": "{{secret:token}}", "Accept": "application/json"}
    if version is not None:
        headers["If-Match"] = version
    try:
        answer = ctx.http.request(method, f"{API}{path}", headers=headers, params=params,
                                  json=body, timeout=TIMEOUT_S)
    except PermissionError as exc:
        # The host rules refused it: the manifest or the account is wrong,
        # which trying again will not mend.
        raise NotConnected(str(exc)) from exc
    if answer.ok:
        return answer.json() if answer.content else {}
    if answer.status == 404:
        return None
    if answer.status in (401, 403):
        raise WrongPassword("the service did not take the token")
    if answer.status == 412:
        raise TargetChanged("the event changed on the service since it was read")
    if answer.status == 429 or answer.status >= 500:
        raise Transient(f"the service answered {answer.status}")
    raise ConnectorError(f"the service answered {answer.status}")


def listing(ctx: Context, path: str, params: dict[str, str], *, limit: int
            ) -> tuple[list[dict[str, Any]], bool]:
    """Up to `limit` items of a listing, page by page; and whether there
    were more than were read."""
    found: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(PAGES):
        asked = {**params, "limit": str(min(max(limit - len(found), 1), 100))}
        if cursor:
            asked["cursor"] = cursor
        page = call(ctx, "GET", path, params=asked) or {}
        found += page.get("items") or []
        cursor = page.get("next") or ""
        if not cursor or len(found) >= limit:
            break
    return found[:limit], bool(cursor) or len(found) > limit


def path_id(native_id: str) -> str:
    """An id as it goes into a path: never one that could name another path."""
    if not ID.fullmatch(native_id or ""):
        raise TargetChanged(f"{native_id!r} is not an id this service gives")
    return native_id


def iso(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the provider -------------------------------------------------------------------

class ExampleCalendar(CalendarProvider):
    # FILL: what the service can do beyond the class's minimum. Each one
    # declared is checked by its part of the contract suite.
    capabilities = frozenset({classes.CREATE, classes.UPDATE, classes.DELETE,
                              classes.CONDITIONAL})

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def _aware(self, when: dt.datetime | dt.date) -> dt.datetime:
        """A moment, in the user's zone where it has none: an all-day
        event's date is that day's midnight where the user is."""
        if not isinstance(when, dt.datetime):
            return dt.datetime.combine(when, dt.time(), tzinfo=self.ctx.zone)
        return when if when.tzinfo else when.replace(tzinfo=self.ctx.zone)

    def _event(self, raw: dict[str, Any]) -> Event:
        all_day = bool(raw.get("all_day"))
        if all_day:
            start: dt.datetime | dt.date = dt.date.fromisoformat(str(raw["start"]))
            end: dt.datetime | dt.date = dt.date.fromisoformat(str(raw["end"]))
        else:
            start = dt.datetime.fromisoformat(str(raw["start"]))
            end = dt.datetime.fromisoformat(str(raw["end"]))
        # The user is not their own guest.
        me = self.ctx.account.address.lower()
        guests = tuple(Guest(email=str(g["email"]), name=str(g.get("name") or ""),
                             response=str(g.get("response") or ""))
                       for g in raw.get("attendees") or []
                       if str(g.get("email") or "").lower() not in ("", me))
        series = str(raw.get("series") or "")
        return Event(
            native_id=str(raw["id"]), ident=str(raw.get("uid") or ""),
            condition=raw.get("version"), title=str(raw.get("title") or ""),
            start=start, end=end, all_day=all_day, location=str(raw.get("location") or ""),
            description=str(raw.get("description") or ""), guests=guests,
            organizer=str(raw.get("organizer") or ""),
            recurring=bool(series or raw.get("recurrence")),
            status=str(raw.get("status") or ""), busy=bool(raw.get("busy", True)),
            extra={"updated": str(raw.get("updated") or ""), "series": series})

    def _body(self, event: Event) -> dict[str, Any]:
        if event.start is None or event.end is None:
            raise ValueError("an event needs its start and its end")
        if event.all_day:
            times = {"start": _day(event.start).isoformat(), "end": _day(event.end).isoformat()}
        else:
            times = {"start": iso(self._aware(event.start)), "end": iso(self._aware(event.end))}
        return {"title": event.title, **times, "all_day": event.all_day,
                "location": event.location, "description": event.description,
                "busy": event.busy}

    def test(self) -> str:
        """Setup's test and the health check: one read that needs the token."""
        now = dt.datetime.now(dt.UTC)
        found = self.events(now, now + dt.timedelta(days=7), limit=10)
        return f"Example's calendar answers for {self.ctx.account.address}, " \
               f"with {len(found)} event{'s' if len(found) != 1 else ''} this week"

    def events(self, start: dt.datetime, end: dt.datetime, *, limit: int) -> list[Event]:
        raw, _ = listing(self.ctx, "/events", {"start": iso(start), "end": iso(end)},
                         limit=limit)
        found = [self._event(item) for item in raw]
        return [e for e in found if e.start is not None and e.end is not None
                and self._aware(e.start) < end and self._aware(e.end) > start]

    def fetch(self, native_id: str) -> Event | None:
        raw = call(self.ctx, "GET", f"/events/{path_id(native_id)}")
        return None if raw is None else self._event(raw)

    def neighbours(self, item: Event) -> dict[str, str | None] | None:
        """Every event as stored, by UID, with its version: core checks
        after a write that only the one meant changed. None where the
        calendar is too big to read whole."""
        raw, more = listing(self.ctx, "/events", {}, limit=1_000)
        if more:
            return None
        return {str(e.get("uid") or ""): e.get("version") for e in raw}

    def _writable(self, event: Event, what: str) -> None:
        if event.guests:
            raise Unsupported(f"{what} an event with guests, who would not be told")
        if event.recurring:
            raise Unsupported(f"{what} a repeating event")

    def create(self, event: Event) -> Event:
        if event.guests:
            raise Unsupported("inviting guests from this calendar")
        made = call(self.ctx, "POST", "/events", body=self._body(event))
        if not made or not made.get("id"):
            raise ConnectorError("the service kept no event")
        return self._event(made)

    def update(self, event: Event) -> Event:
        """Write the one event core's write path fetched and checked, on
        its version: one changed on another device since is not overwritten."""
        self._writable(event, "changing")
        changed = call(self.ctx, "PATCH", f"/events/{path_id(event.native_id)}",
                       body=self._body(event), version=event.condition)
        if changed is None:
            raise TargetChanged("the event was gone when it was to be changed")
        return self._event(changed)

    def delete(self, event: Event) -> None:
        self._writable(event, "deleting")
        if call(self.ctx, "DELETE", f"/events/{path_id(event.native_id)}",
                version=event.condition) is None:
            raise TargetChanged("the event was gone when it was to be deleted")


def _day(when: dt.datetime | dt.date) -> dt.date:
    return when.date() if isinstance(when, dt.datetime) else when
