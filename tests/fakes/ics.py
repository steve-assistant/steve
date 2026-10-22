"""Calendar links for tests: feeds served at private addresses, as a course
site, Outlook or a shared calendar serve them.

The ICS engine reads a feed through `ctx.http`, so the fake is a network:
every host resolves to one public address and the hosts it serves answer
from here, through `httpx.MockTransport`; any other host is recorded and
refused, so no test reaches a real calendar. A `FeedWorld` is one feed:
events seeded as the contract suite seeds them, plus raw VEVENTs written
as real servers write them. The fixture feeds hold what broke the
personal edition's hand-written parser, or would: Windows zone names, a
zone nobody knows, a custom VTIMEZONE as Outlook publishes, a weekly
series with an exception and a moved occurrence, an all-day date with its
parameter given twice, and due dates written as moments.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import icalendar

from core import credstore, modctx
from steve_sdk.context import Account
from steve_sdk.manifest import Manifest
from steve_sdk.testing import World

PUBLIC_IP = "93.184.216.34"
HOST = "feeds.school.example"
PATH = "/calendar/feeds/user_7d1f0c.ics"
TOKEN = "tk-91c3e5a0"
LINK = f"https://{HOST}{PATH}?token={TOKEN}"

# Times in zones as Outlook and Exchange name them, and one nobody does.
WORK = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Fake Exchange//EN
BEGIN:VTIMEZONE
TZID:Customized Time Zone
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:standup@work.example
SUMMARY:Stand-up
DTSTART;TZID=W. Europe Standard Time:20261102T090000
DTEND;TZID=W. Europe Standard Time:20261102T091500
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=W. Europe Standard Time:20261109T090000
END:VEVENT
BEGIN:VEVENT
UID:standup@work.example
RECURRENCE-ID;TZID=W. Europe Standard Time:20261116T090000
SUMMARY:Stand-up (moved)
DTSTART;TZID=W. Europe Standard Time:20261116T100000
DTEND;TZID=W. Europe Standard Time:20261116T101500
END:VEVENT
BEGIN:VEVENT
UID:review@work.example
SUMMARY:Design review
DTSTART;TZID=Eastern Standard Time:20261103T090000
DTEND;TZID=Eastern Standard Time:20261103T100000
END:VEVENT
BEGIN:VEVENT
UID:offsite@work.example
SUMMARY:Offsite
DTSTART;TZID=Customized Time Zone:20261104T130000
DTEND;TZID=Customized Time Zone:20261104T150000
END:VEVENT
BEGIN:VEVENT
UID:mars@work.example
SUMMARY:Call with the rover team
DTSTART;TZID=Olympus Mons Time:20261105T090000
DTEND;TZID=Olympus Mons Time:20261105T093000
END:VEVENT
BEGIN:VEVENT
UID:holiday@work.example
SUMMARY:Office closed
DTSTART;VALUE=DATE:20261106
DTEND;VALUE=DATE:20261107
END:VEVENT
END:VCALENDAR
"""

# A course site's feed: due dates as moments, one as a bare date with its
# parameter twice, and a class session with a length.
COURSE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Fake Course Site//EN
X-WR-CALNAME:Maya Lindqvist Calendar
BEGIN:VEVENT
UID:due-essay@school.example
SUMMARY:Essay on glazes [CER 101]
DESCRIPTION:Two pages\\, with photos of three test tiles.
URL:https://school.example/courses/101/assignments/7
DTSTART:20261103T220000Z
DTEND:20261103T220000Z
END:VEVENT
BEGIN:VEVENT
UID:due-reading@school.example
SUMMARY:Reading response [CER 101]
DTSTART;VALUE=DATE;VALUE=DATE:20261105
END:VEVENT
BEGIN:VEVENT
UID:session@school.example
SUMMARY:Studio session [CER 101]
DTSTART:20261104T140000Z
DTEND:20261104T160000Z
END:VEVENT
BEGIN:VEVENT
UID:due-later@school.example
SUMMARY:Final portfolio [CER 101]
DTSTART:20261201T220000Z
DTEND:20261201T220000Z
END:VEVENT
END:VCALENDAR
"""


def crlf(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


class FeedWorld(World):
    """One feed: the `raw` text as a server wrote it, or else what `seed`
    put in, as one VCALENDAR."""

    def __init__(self, raw: str = "") -> None:
        self.raw = raw
        self.items: dict[str, dict[str, Any]] = {}

    def seed(self, klass: str, items: list[dict[str, Any]]) -> list[str]:
        assert klass == "calendar", klass
        ids = []
        for item in items:
            uid = item.get("uid") or (f"{hashlib.sha1(repr(item).encode()).hexdigest()[:10]}"
                                      f"-{len(self.items)}@seed.example")
            self.items[uid] = dict(item)
            ids.append(uid)
        return ids

    def mutate(self, klass: str, native_id: str, **changes: Any) -> None:
        uid = native_id.partition("\n")[0]
        if changes.pop("gone", False):
            del self.items[uid]
            return
        self.items[uid].update(changes)

    def snapshot(self, klass: str) -> dict[str, Any]:
        return {uid: repr(sorted(item.items())) for uid, item in self.items.items()}

    def text(self) -> bytes:
        cal = icalendar.Calendar()
        cal.add("prodid", "-//fake feed//EN")
        cal.add("version", "2.0")
        for uid, item in self.items.items():
            event = icalendar.Event()
            event.add("uid", uid)
            event.add("dtstamp", dt.datetime(2026, 10, 1, tzinfo=dt.UTC))
            event.add("summary", item.get("title", "Event"))
            event.add("dtstart", item["start"])
            if item.get("end") is not None:
                event.add("dtend", item["end"])
            if item.get("rrule"):
                event.add("rrule", icalendar.vRecur.from_ical(item["rrule"]))
            cal.add_component(event)
        return crlf(self.raw) if self.raw and not self.items else cal.to_ical()


@dataclass
class Served:
    """What a path answers: the world's feed, or a fixed status and body."""
    world: FeedWorld | None = None
    status: int = 200
    body: bytes = b""
    content_type: str = "text/calendar; charset=utf-8"
    location: str = ""  # where a redirect sends the reader


class FeedNetwork(modctx.Network):
    """Every name resolves to one public address; the hosts served answer
    from `paths`; any other is recorded and refused."""

    def __init__(self) -> None:
        self.paths: dict[tuple[str, str], Served] = {}
        self.reached: list[str] = []
        self.requests: list[str] = []

    def resolve(self, host: str, port: int) -> list[str]:
        return [PUBLIC_IP]

    def own_ports(self) -> set[int]:
        return {54321}  # Steve's own, as a run file would name it

    def transport_for(self, host: str) -> httpx.BaseTransport | None:
        self.reached.append(host)
        if host not in {h for h, _ in self.paths}:
            raise AssertionError(f"a request reached {host}, which no feed is served on")
        return httpx.MockTransport(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.host}{request.url.path}")
        served = self.paths.get((request.url.host, request.url.path))
        if served is None:
            return httpx.Response(404, text="no such feed")
        body = served.world.text() if served.world is not None else served.body
        headers = {"Content-Type": served.content_type}
        if served.location:
            headers["Location"] = served.location
        return httpx.Response(served.status, content=body, headers=headers)


@dataclass
class FeedRig:
    network: FeedNetwork = field(default_factory=FeedNetwork)
    made: list[Any] = field(default_factory=list)

    def serve(self, link: str = LINK, *, world: FeedWorld | None = None, raw: str = "",
              status: int = 200, body: bytes = b"", location: str = "",
              content_type: str = "text/calendar; charset=utf-8") -> FeedWorld:
        """Serve a feed at `link`'s host and path (its query is ignored, as
        the token in it is the server's to check); the world it serves."""
        parts = urlsplit(link.replace("webcal://", "https://", 1))
        world = world if world is not None else FeedWorld(raw)
        self.network.paths[(parts.hostname or "", parts.path)] = Served(
            world=world if not body and status == 200 else None, status=status, body=body,
            content_type=content_type, location=location)
        return world

    def provider(self, manifest: Manifest, *, link: str = LINK, due_dates: bool | None = None,
                 name: str = "Course site", key: str = "acct0003",
                 module_key: str = "m0dkey0003", zone: dt.tzinfo = dt.UTC):
        """The engine's provider for an account set up with `link`, its host
        pinned as setup pins it."""
        from core.engines import ics

        host = (urlsplit(link.replace("webcal://", "https://", 1)).hostname or "").lower()
        fields: dict[str, Any] = {"hosts": {host: "public"}}
        if due_dates is not None:
            fields["due_dates"] = due_dates
        account = Account(key=key, name=name, module=manifest.id, address="", preset=None,
                          fields=fields)
        credstore.write(credstore.target(module_key, key, manifest.secrets[0]), link)
        ctx = modctx.ModuleContext(manifest=manifest, module_key=module_key, account=account,
                                   state_dir=None, network=self.network, zone=lambda: zone)
        self.made.append(ctx)
        return ics.engine(ctx, manifest.specs["calendar"])

    def close(self) -> None:
        """Close each context, and drop the engine's feeds as read: they are
        kept by the account's keys, which the next test may use again."""
        from core.engines import ics

        for ctx in self.made:
            ctx.close()
        ics.forget()
