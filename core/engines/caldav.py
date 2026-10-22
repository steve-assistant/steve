"""Calendars by password: core's CalDAV engine, for every module built on it.

Google's calendar was reached through its own API alone, so a user with a
password account (Gmail by app password, iCloud, Fastmail, a server at
home) had no calendar in Steve at all. CalDAV is the standard each of them
speaks, and on 11 October 2026 a spike's cleanup showed what trusting such
a server costs: Google's CalDAV ignored a UID filter, answered with all 96
events, and the script deleted every one (`steve_sdk/exact.py`). So this
engine fills the calendar class for each module whose manifest names it,
and takes nothing the server says as the target (05-connectors.md,
section 4):

- Every request goes through `ctx.http`: HTTPS, to the manifest's hosts or
  the one the user typed, of the kind pinned at setup. An address the
  server returns on any other host is refused before the password goes
  there, so a hostile server cannot send it elsewhere.
- Calendars are found from the principal, or the address is a calendar
  itself, as Gmail's legacy one is.
- An event's id is the address of its resource on the server, and the
  occurrence's start where it repeats. Repeats are expanded here, not by
  the server: one that does not expand would otherwise show a weekly
  meeting on the week it began and never again.
- Writes are conditional: a new event is PUT at its UID's address with
  `If-None-Match: *` and then found again (Google stores it under the
  quoted UID, wherever it was asked to); a change or a delete reads the
  exact address, checks its UID, and writes with `If-Match` on the etag
  just read.
- No guests: a CalDAV account cannot invite anyone, and a server may email
  an event's guests when it changes, so an event with other people on it
  is read, never written. Google Cloud's account does that, with a card.
- A repeating event is read by its occurrences and never changed here:
  on most servers one occurrence's edit is a change to the whole series.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
import icalendar

from core.engines import ical
from core.modctx import Refused
from steve_sdk import classes
from steve_sdk.classes import CalendarProvider, Event
from steve_sdk.errors import (
    ConnectorError,
    NotConnected,
    TargetChanged,
    Transient,
    Unsupported,
    WrongPassword,
)

TIMEOUT_S = 20.0
_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
_UTC = dt.UTC
_ICS = {"Content-Type": "text/calendar; charset=utf-8"}

_PROPFIND_START = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:resourcetype/><d:current-user-principal/><c:calendar-home-set/></d:prop>
</d:propfind>"""
_PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-home-set/></d:prop>
</d:propfind>"""
_PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:resourcetype/><d:displayname/><c:supported-calendar-component-set/>
    <d:current-user-privilege-set/></d:prop>
</d:propfind>"""
_REPORT_RANGE = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{start}" end="{end}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""
_REPORT_UID = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:prop-filter name="UID"><c:text-match collation="i;octet">{uid}</c:text-match>
    </c:prop-filter>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""


class Calendar:
    """One calendar collection on the server."""

    def __init__(self, url: str, name: str, writable: bool) -> None:
        self.url, self.name, self.writable = url, name, writable


# --- addresses ---------------------------------------------------------------

def _canon(url: str) -> str:
    """One spelling of a resource's address, however a server escapes its
    path: Google escapes the @ of a UID as %40 in what it answers, where it
    was PUT unescaped, and an event's id must be the same from a listing as
    from its creation."""
    parts = urlsplit(url)
    path = quote(unquote(parts.path), safe="/@:+,;=!$&'()*~")
    host = (parts.hostname or "").lower()
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


def _join(base: str, href: str) -> str:
    return _canon(urljoin(base, (href or "").strip()))


def _collection(url: str) -> str:
    return _canon(url).rstrip("/") + "/"


def _parent(href: str) -> str:
    return _canon(href).rsplit("/", 1)[0] + "/"


def _native(href: str, recurrence: str = "") -> str:
    return f"{href}\n{recurrence}" if recurrence else href


def _split(native: str) -> tuple[str, str]:
    href, _, recurrence = (native or "").partition("\n")
    return href, recurrence


# --- XML and iCalendar ---------------------------------------------------------

def _multistatus(body: bytes) -> list[ElementTree.Element]:
    try:
        root = ElementTree.fromstring(body or b"<x/>")
    except ElementTree.ParseError as exc:
        raise ConnectorError(f"the calendar server answered with what is not XML: "
                             f"{exc}"[:300]) from exc
    return root.findall("d:response", _NS)


def _prop(response: ElementTree.Element, path: str) -> ElementTree.Element | None:
    """A property the server found (a propstat with status 200)."""
    for propstat in response.findall("d:propstat", _NS):
        status = propstat.findtext("d:status", default="", namespaces=_NS)
        if status and " 200 " not in f"{status} ":
            continue
        found = propstat.find(f"d:prop/{path}", _NS)
        if found is not None:
            return found
    return None


def _prop_text(response: ElementTree.Element, path: str) -> str:
    found = _prop(response, path)
    return (found.text or "").strip() if found is not None else ""


def _href_of(response: ElementTree.Element) -> str:
    return response.findtext("d:href", default="", namespaces=_NS)


def _parse(text: str) -> Any:
    return ical.parse(text, "an event on the server")


def _utc(moment: dt.datetime | dt.date | None) -> dt.datetime | dt.date | None:
    """A time as written to the server: UTC, so no VTIMEZONE is needed; a
    date stays a date (an all-day event)."""
    if isinstance(moment, dt.datetime):
        return moment.astimezone(_UTC)
    return moment


class CalDavCalendar(CalendarProvider):
    """The calendar class over CalDAV, for one account of one module."""

    def __init__(self, ctx: Any, spec: Any) -> None:
        self.ctx = ctx
        account = ctx.account
        settings = ctx.manifest.settings("calendar", account.preset)
        fields = dict(account.fields or {})
        self.address = (account.address or "").strip()
        # An address the manifest fixes wins (Gmail's, a preset's); one the
        # user typed ("Other") is used only where it leaves it open.
        if settings.get("url"):
            self.start = ctx.url(settings["url"], {"address": self.address})
        else:
            self.start = str(fields.get("url") or "").strip()
        self.discover = bool(settings.get("discover", True))
        self.username = str(fields.get("username") or "").strip() or self.address
        secrets = ctx.manifest.secrets
        self.secret_name = secrets[0] if len(secrets) == 1 else None
        self.capabilities = frozenset({classes.CREATE, classes.UPDATE, classes.DELETE,
                                       classes.CONDITIONAL})
        self.timeout = TIMEOUT_S
        self._calendars: list[Calendar] | None = None

    # --- requests

    def _auth(self) -> str:
        if self.secret_name is None:
            raise NotConnected("its module names no one password for its calendar")
        password = self.ctx.secret(self.secret_name)
        if not password:
            raise NotConnected("no password is kept for it")
        pair = f"{self.username}:{password}".encode()
        return "Basic " + base64.b64encode(pair).decode("ascii")

    def _call(self, method: str, url: str, *, body: str | None = None,
              depth: str | None = None, headers: dict[str, str] | None = None,
              ok: tuple[int, ...] = (200, 207)) -> Any:
        """One request, its failure said as one of the SDK's errors. A
        status in `ok` is the caller's to read."""
        sent = {"Authorization": self._auth(), **(headers or {})}
        if depth is not None:
            sent["Depth"] = depth
        if body is not None:
            sent.setdefault("Content-Type", "application/xml; charset=utf-8")
        try:
            answer = self.ctx.http.request(method, url, headers=sent,
                                           content=body.encode() if body is not None else None,
                                           timeout=self.timeout)
        except Refused as exc:
            raise NotConnected(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise Transient(f"{type(exc).__name__}: {exc}"[:300]) from exc
        if answer.status in ok:
            return answer
        if answer.status == 401:
            raise WrongPassword("the calendar server did not take the password")
        if answer.status == 412:
            raise TargetChanged("the event changed on the server since it was read")
        if answer.status == 429 or answer.status >= 500:
            raise Transient(f"the calendar server answered {answer.status}")
        raise ConnectorError(f"the calendar server refused that ({answer.status})")

    # --- calendars

    def calendars(self) -> list[Calendar]:
        """This account's calendars of events, found once a process."""
        if self._calendars is None:
            self._calendars = self._find()
        return self._calendars

    def _find(self) -> list[Calendar]:
        if not self.start:
            raise NotConnected("no calendar address is set for it")
        start = _canon(self.start)
        answer = self._call("PROPFIND", start, body=_PROPFIND_START, depth="0")
        first = next(iter(_multistatus(answer.content)), None)
        if first is None:
            raise ConnectorError("the calendar server said nothing of that address")
        kind = _prop(first, "d:resourcetype")
        if kind is not None and kind.find("c:calendar", _NS) is not None:
            return [Calendar(_collection(start), self.address or "Calendar", True)]
        if not self.discover:
            raise ConnectorError("that address is not a calendar")
        home, base = _prop_text(first, "c:calendar-home-set/d:href"), start
        if not home:
            principal = _prop_text(first, "d:current-user-principal/d:href")
            if not principal:
                raise ConnectorError("the calendar server named no calendars for this account")
            base = _join(start, principal)
            found = self._call("PROPFIND", base, body=_PROPFIND_HOME, depth="0")
            mine = next(iter(_multistatus(found.content)), None)
            home = _prop_text(mine, "c:calendar-home-set/d:href") if mine is not None else ""
        if not home:
            raise ConnectorError("the calendar server named no calendars for this account")
        home = _collection(_join(base, home))
        listed = self._call("PROPFIND", home, body=_PROPFIND_CALENDARS, depth="1")
        out = []
        for response in _multistatus(listed.content):
            kind = _prop(response, "d:resourcetype")
            if kind is None or kind.find("c:calendar", _NS) is None:
                continue
            comps = _prop(response, "c:supported-calendar-component-set")
            names = {c.get("name", "").upper() for c in comps.findall("c:comp", _NS)} \
                if comps is not None else set()
            if names and "VEVENT" not in names:
                continue  # a list of tasks or notes, not a calendar of events
            privileges = _prop(response, "d:current-user-privilege-set")
            writable = privileges is None or any(
                p.find(f"d:{name}", _NS) is not None
                for p in privileges.findall("d:privilege", _NS)
                for name in ("write", "write-content", "all"))
            url = _collection(_join(home, _href_of(response)))
            name = _prop_text(response, "d:displayname") or url.rstrip("/").rsplit("/", 1)[-1]
            out.append(Calendar(url, unquote(name), writable))
        if not out:
            raise ConnectorError("the calendar server listed no calendars for this account")
        return out

    def _calendar_of(self, href: str) -> Calendar | None:
        parent = _parent(href)
        return next((c for c in self.calendars() if c.url == parent), None)

    def _for_new(self, event: Event) -> Calendar | None:
        """Where a new event goes: the calendar it names, else the first
        this account can write to."""
        return next((c for c in self.calendars() if c.writable and (
            not event.calendar or c.name == event.calendar)), None)

    # --- reading

    def _events(self, calendar: Calendar, href: str, etag: str | None, parsed: Any,
                start: dt.datetime | None = None, end: dt.datetime | None = None
                ) -> list[Event]:
        """The events of one resource: its occurrences in [start, end) when
        given, else each component as stored."""
        components = list(parsed.walk("VEVENT"))
        repeating = ical.repeats(components)
        if start is not None and end is not None:
            components = ical.between(parsed, start, end)
        return [self._event(calendar, href, etag, c, repeating) for c in components]

    def _event(self, calendar: Calendar, href: str, etag: str | None, c: Any,
               repeating: bool) -> Event:
        return ical.event(c, zone=self.ctx.zone,
                          native_id=_native(href, ical.occurrence(c, repeating)),
                          calendar=calendar.name, repeating=repeating, condition=etag,
                          own=self.address)

    def events(self, start: dt.datetime, end: dt.datetime, *, limit: int) -> list[Event]:
        body = _REPORT_RANGE.format(start=ical.stamp(start), end=ical.stamp(end))
        out: list[Event] = []
        for calendar in self.calendars():
            answer = self._call("REPORT", calendar.url, body=body, depth="1")
            for response in _multistatus(answer.content):
                data = _prop_text(response, "c:calendar-data")
                if not data:
                    continue
                href = _join(calendar.url, _href_of(response))
                etag = _prop_text(response, "d:getetag") or None
                # The range is ours to keep: a server may answer with every
                # event it has, as Google's did to a filter.
                out += self._events(calendar, href, etag, _parse(data), start, end)
        return out[:limit]

    def _get(self, href: str) -> tuple[str | None, Any] | None:
        answer = self._call("GET", href, ok=(200, 404))
        if answer.status == 404:
            return None
        return answer.headers.get("etag"), _parse(answer.text)

    def fetch(self, native_id: str) -> Event | None:
        href, recurrence = _split(native_id)
        calendar = self._calendar_of(href)
        if calendar is None:
            return None
        got = self._get(href)
        if got is None:
            return None
        etag, parsed = got
        if not recurrence:
            found = self._events(calendar, href, etag, parsed)
            if len(found) != 1 or found[0].recurring:
                return None
            return found[0]
        # One occurrence: the series expanded around its start.
        repeating = ical.repeats(parsed.walk("VEVENT"))
        around = [self._event(calendar, href, etag, c, repeating)
                  for c in ical.around(parsed, recurrence, self.ctx.zone)]
        return next((e for e in around if e.native_id == native_id), None)

    def _days(self, item: Any) -> tuple[dt.datetime, dt.datetime] | None:
        """The whole days, in the account's zone, that `item` spans; None
        for an item with no time to place it by."""
        first = getattr(item, "start", None)
        last = getattr(item, "end", None) or first
        if first is None:
            return None
        zone = self.ctx.zone

        def day(moment: dt.datetime | dt.date) -> dt.date:
            if not isinstance(moment, dt.datetime):
                return moment
            return (moment if moment.tzinfo else moment.replace(tzinfo=zone)) \
                .astimezone(zone).date()

        start = dt.datetime.combine(day(first), dt.time(), tzinfo=zone)
        end = dt.datetime.combine(day(last), dt.time(), tzinfo=zone) + dt.timedelta(days=1)
        return start, end

    def neighbours(self, item: Any) -> dict[str, str | None] | None:
        """The events of the item's calendar on the days it spans, with
        their etags, by address, and the item itself by its UID: what the
        write path compares before and after, so a write that took or
        changed another event that day would show.

        It was the whole calendar, and a phone's sync of any event in the
        second between the two reads was taken for this write's doing. The
        days are kept here, whatever the server answered for the range."""
        href, _ = _split(getattr(item, "native_id", "") or "")
        calendar = self._calendar_of(href) if href else self._for_new(item)
        days = self._days(item)
        if calendar is None or days is None:
            return None
        start, end = days
        body = _REPORT_RANGE.format(start=ical.stamp(start), end=ical.stamp(end))
        answer = self._call("REPORT", calendar.url, body=body, depth="1")
        out: dict[str, str | None] = {}
        for response in _multistatus(answer.content):
            data = _prop_text(response, "c:calendar-data")
            where = _join(calendar.url, _href_of(response))
            if not data or not ical.between(_parse(data), start, end):
                continue
            key = item.ident if href and where == _canon(href) and item.ident else where
            out[key] = _prop_text(response, "d:getetag") or None
        return out

    def test(self) -> str:
        found = self.calendars()
        now = dt.datetime.now(_UTC)
        week = self.events(now, now + dt.timedelta(days=7), limit=500)
        names = ", ".join(c.name for c in found[:5]) + (" and more" if len(found) > 5 else "")
        return f"signed in to {len(found)} calendar(s) ({names}); {len(week)} events this week"

    # --- writing

    def _writable(self, event: Event) -> None:
        if event.recurring:
            raise Unsupported("changing a repeating event from this calendar; change it in "
                              "the calendar's own app")
        if event.guests:
            raise Unsupported("changing an event with guests from this calendar, which cannot "
                              "tell them; change it in the calendar's own app")

    def create(self, event: Event) -> Event:
        if event.guests:
            raise Unsupported("inviting guests from this calendar")
        calendar = self._for_new(event)
        if calendar is None:
            raise Unsupported(f"adding events to {event.calendar or 'this account'}, "
                              "which can only be read")
        uid = event.ident or f"{uuid.uuid4()}@steve"
        asked = _canon(calendar.url + quote(uid, safe="@") + ".ics")
        self._call("PUT", asked, body=self._new(uid, event),
                   headers={**_ICS, "If-None-Match": "*"}, ok=(200, 201, 204))
        made = self.fetch(self._stored(calendar, asked, uid))
        if made is None or made.ident != uid:
            raise TargetChanged("the server did not keep the new event where it said")
        return made

    def _stored(self, calendar: Calendar, asked: str, uid: str) -> str:
        """Where the server stored the event just made with `uid`: the address
        it was PUT at, or, for a server that chose its own, the one event with
        that UID, checked here, since a server may ignore the filter and
        answer with every event it has."""
        if self._get(asked) is not None:
            return asked
        answer = self._call("REPORT", calendar.url, body=_REPORT_UID.format(uid=uid),
                            depth="1")
        found = []
        for response in _multistatus(answer.content):
            data = _prop_text(response, "c:calendar-data")
            if data and any(ical.text(c, "UID") == uid for c in _parse(data).walk("VEVENT")):
                found.append(_join(calendar.url, _href_of(response)))
        if len(found) != 1:
            raise TargetChanged(f"the server holds {len(found)} events with the new event's UID")
        return found[0]

    def _new(self, uid: str, event: Event) -> str:
        cal = icalendar.Calendar()
        cal.add("prodid", "-//Steve//CalDAV//EN")
        cal.add("version", "2.0")
        component = icalendar.Event()
        component.add("uid", uid)
        cal.add_component(self._fill(component, event))
        return cal.to_ical().decode("utf-8")

    @staticmethod
    def _fill(component: Any, event: Event) -> Any:
        """The event's fields onto a VEVENT, leaving whatever else it holds
        (alarms, categories, another app's properties) as it was."""
        now = dt.datetime.now(_UTC)
        for name in ("DTSTAMP", "LAST-MODIFIED", "SUMMARY", "DTSTART", "DTEND", "DURATION",
                     "LOCATION", "DESCRIPTION"):
            component.pop(name, None)
        component.add("dtstamp", now)
        component.add("last-modified", now)
        component.add("summary", event.title)
        component.add("dtstart", _utc(event.start))
        if event.end is not None:
            component.add("dtend", _utc(event.end))
        if event.location:
            component.add("location", event.location)
        if event.description:
            component.add("description", event.description)
        return component

    def _exact(self, event: Event) -> tuple[str, str, Any]:
        """The event's resource read again, its UID and etag checked here:
        its address, the etag, and the calendar as stored."""
        href, _ = _split(event.native_id)
        got = self._get(href)
        if got is None:
            raise TargetChanged("the event is gone from the server")
        etag, parsed = got
        if not event.condition or etag != event.condition:
            raise TargetChanged("the event changed on the server since it was read")
        uids = {ical.text(c, "UID") for c in parsed.walk("VEVENT")}
        if uids != {event.ident}:
            raise TargetChanged("the server's event at that address is another one")
        return href, etag, parsed

    def update(self, event: Event) -> Event:
        self._writable(event)
        href, etag, parsed = self._exact(event)
        [component] = list(parsed.walk("VEVENT")) or [None]
        if component is None:
            raise TargetChanged("the server's event at that address holds no event")
        sequence = int(component.get("SEQUENCE") or 0) + 1
        self._fill(component, event)
        component.pop("SEQUENCE", None)
        component.add("sequence", sequence)
        self._call("PUT", href, body=parsed.to_ical().decode("utf-8"),
                   headers={**_ICS, "If-Match": etag}, ok=(200, 201, 204))
        return self.fetch(event.native_id) or event

    def delete(self, event: Event) -> None:
        self._writable(event)
        href, etag, _ = self._exact(event)
        self._call("DELETE", href, headers={"If-Match": etag}, ok=(200, 204))


def engine(ctx: Any, spec: Any) -> CalDavCalendar:
    if spec.klass != "calendar":
        raise ValueError(f"the CalDAV engine fills calendars, not {spec.klass}")
    return CalDavCalendar(ctx, spec)
