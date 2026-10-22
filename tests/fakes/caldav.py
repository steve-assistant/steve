"""A CalDAV server for the CalDAV engine's tests: Radicale, in the process.

The engine is tested against a real server's protocol, not against what
its author believed a server says: Radicale, run in the test's process and
reached through httpx's WSGI transport, so the engine's requests still go
through `ctx.http` and every rule there (declared hosts, HTTPS, pinned
kinds). A thin layer in front of it adds the habits Radicale lacks and the
engine must survive (05-connectors.md, section 8):

- `google`: the calendar lives at Google's legacy address; a new event is
  stored under its quoted UID, whatever name it was sent to; and a
  calendar-query is answered with every event, its filter ignored, as
  Google's was on 11 October 2026;
- `own_names`: the server names a new event itself, so the engine must
  find it again by its UID;
- `principal_host`: discovery answers with the account's principal on
  another host: one the module declares (iCloud's `p01-caldav.icloud.com`),
  or one it does not, which must never see the password.

- `ignores_filters`: Google's habit on its own, beside another mode;
- `after_get`: a change made on the phone just after the engine's next
  read, before its write, which only the write's condition can catch;
- `after_put`: one just after its next write, before the check after it.

The `World` writes straight to Radicale, behind the provider's back.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx
import icalendar
import radicale
import radicale.config

from core import credstore, modctx
from steve_sdk.context import Account
from steve_sdk.manifest import Manifest
from steve_sdk.testing import World

PUBLIC_IP = "93.184.216.34"
STANDARD, GOOGLE, OWN_NAMES = "standard", "google", "own_names"
GOOGLE_PREFIX = "/calendar/dav"
_ALL = b"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"/></c:filter>
</c:calendar-query>"""


_MKCALENDAR = """<?xml version="1.0" encoding="utf-8"?>
<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:set><d:prop><d:displayname>{name}</d:displayname></d:prop></d:set>
</c:mkcalendar>"""


def _uid_of(body: bytes) -> str:
    found = re.search(rb"^UID:(.+?)\r?$", body, re.MULTILINE)
    return found.group(1).decode().strip() if found else ""


class Habits:
    """The WSGI layer in front of Radicale: a server's habits, by mode."""

    def __init__(self, app: Any, *, mode: str, principal_host: str = "",
                 prefix: str = "", ignores_filters: bool | None = None) -> None:
        self.app = app
        self.mode = mode
        self.principal_host = principal_host
        self.prefix = prefix
        self.ignores_filters = mode == GOOGLE if ignores_filters is None else ignores_filters
        # Run once, after the next GET is answered: a change made on the
        # phone between the engine's read and its write.
        self.after_get: Any = None
        # Run once, after the next PUT is answered: a change synced from the
        # phone between the write and the write path's read after it.
        self.after_put: Any = None
        self.requests: list[tuple[str, str, str]] = []  # host, method, path
        self.lock = threading.Lock()

    def __call__(self, environ: dict, start_response: Any) -> Any:
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "")
        # Radicale gzips what it answers when asked to, and a rewrite of
        # the answer would then match nothing, silently.
        environ.pop("HTTP_ACCEPT_ENCODING", None)
        with self.lock:
            self.requests.append((environ.get("HTTP_HOST", ""), method, path))
        if self.prefix and path.startswith(self.prefix + "/"):
            environ["SCRIPT_NAME"] = self.prefix
            environ["PATH_INFO"] = path = path[len(self.prefix):]
        body = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0) or -1) \
            if method in ("PUT", "REPORT", "PROPFIND", "MKCALENDAR") else b""
        if method == "PUT" and path.endswith(".ics"):
            folder = path.rsplit("/", 1)[0]
            if self.mode == GOOGLE:
                # The quoted UID, as Google names it; decoded here, as
                # PATH_INFO is.
                path = f"{folder}/{_uid_of(body)}.ics"
            elif self.mode == OWN_NAMES and environ.get("HTTP_IF_NONE_MATCH") == "*":
                name = hashlib.sha1(unquote(path).encode()).hexdigest()[:12]
                path = f"{folder}/{name}.ics"
            environ["PATH_INFO"] = path
        if method == "REPORT" and self.ignores_filters and b"calendar-query" in body:
            body = _ALL
        environ["wsgi.input"] = _Body(body)
        environ["CONTENT_LENGTH"] = str(len(body))
        hook = f"after_{method.lower()}"
        if method in ("GET", "PUT") and getattr(self, hook) is not None:
            answer = list(self.app(environ, start_response))
            then = getattr(self, hook)
            setattr(self, hook, None)
            then()
            return answer
        if not (self.principal_host and method == "PROPFIND"):
            return self.app(environ, start_response)
        held: dict[str, Any] = {}

        def hold(status: str, headers: list, exc_info: Any = None) -> Any:
            held.update(status=status, headers=headers)
            return lambda data: None

        answer = b"".join(self.app(environ, hold))
        answer = answer.replace(b"<current-user-principal><href>/",
                                f"<current-user-principal><href>https://"
                                f"{self.principal_host}/".encode())
        headers = [(k, v) for k, v in held["headers"] if k.lower() != "content-length"]
        start_response(held["status"], headers + [("Content-Length", str(len(answer)))])
        return [answer]


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    def read(self, size: int = -1) -> bytes:
        end = len(self.data) if size is None or size < 0 else self.at + size
        out, self.at = self.data[self.at:end], min(end, len(self.data))
        return out


def _long_ok(folder: Path) -> str:
    """Radicale's store as a path Windows lets run past 260 characters.

    Radicale nests a history cache, a temporary folder and the event's own
    name under each calendar: 158 characters below the store for a Gmail
    address and a UUID. Under pytest-xdist the tmp folder gains a worker's
    name, and a create then failed with a 500 that passed alone. Shorter
    folder names would only move the edge to a longer user name."""
    path = str(folder.resolve())
    return "\\\\?\\" + path if os.name == "nt" else path


class CalWorld(World):
    """Radicale's store, as a contract suite drives it."""

    def __init__(self, folder: Path, *, user: str, password: str, base: str,
                 calendars: tuple[str, ...] = ("home",), prefix: str = "") -> None:
        self.user, self.password, self.base, self.prefix = user, password, base, prefix
        (folder / "users").write_text(f"{user}:{password}\n", newline="\n")
        conf = radicale.config.load()
        conf.update({
            "storage": {"filesystem_folder": _long_ok(folder / "collections")},
            "auth": {"type": "htpasswd", "htpasswd_filename": str(folder / "users"),
                     "htpasswd_encryption": "plain"},
            "rights": {"type": "owner_only"},
            "logging": {"level": "error", "mask_passwords": "True"},
        }, "test", privileged=True)
        self.app = radicale.Application(conf)
        self._direct = httpx.Client(transport=httpx.WSGITransport(app=self.app),
                                    base_url="https://radicale.invalid", auth=(user, password))
        self.calendars = calendars
        for name in calendars:
            made = self._direct.request("MKCALENDAR", self.path(name) + "/",
                                        content=_MKCALENDAR.format(name=name.title()))
            assert made.status_code == 201, made.text

    def put_raw(self, calendar: str, name: str, text: str) -> str:
        """An event as another app wrote it, whole; its native id."""
        put = self._direct.put(self.path(calendar, name), content=text.encode(),
                               headers={"Content-Type": "text/calendar"})
        assert put.status_code in (201, 204), put.text
        return self.native(calendar, name)

    def path(self, calendar: str, name: str = "") -> str:
        """A calendar's path on Radicale, or an event's in it."""
        return f"/{quote(self.user, safe='@')}/{calendar}" + (f"/{name}" if name else "")

    def calendar_url(self, calendar: str = "home") -> str:
        return f"{self.base}{self.prefix}{self.path(calendar)}/"

    def native(self, calendar: str, name: str) -> str:
        return f"{self.base}{self.prefix}{self.path(calendar, quote(name, safe='@'))}"

    # --- World

    def seed(self, klass: str, items: list[dict[str, Any]]) -> list[str]:
        assert klass == "calendar", klass
        ids = []
        for item in items:
            uid = item.get("uid") or f"{hashlib.sha1(repr(item).encode()).hexdigest()[:10]}" \
                f"-{len(ids)}@seed.example"
            calendar = item.get("calendar", self.calendars[0])
            cal = icalendar.Calendar()
            cal.add("prodid", "-//fake//EN")
            cal.add("version", "2.0")
            event = icalendar.Event()
            event.add("uid", uid)
            event.add("dtstamp", dt.datetime(2026, 10, 1, tzinfo=dt.UTC))
            event.add("summary", item.get("title", "Event"))
            event.add("dtstart", item["start"])
            if item.get("end") is not None:
                event.add("dtend", item["end"])
            if item.get("rrule"):
                event.add("rrule", icalendar.vRecur.from_ical(item["rrule"]))
            if item.get("organizer"):
                event.add("organizer", f"mailto:{item['organizer']}")
            for guest in item.get("guests", ()):
                event.add("attendee", f"mailto:{guest}")
            cal.add_component(event)
            put = self._direct.put(self.path(calendar, f"{uid}.ics"), content=cal.to_ical(),
                                   headers={"Content-Type": "text/calendar"})
            assert put.status_code in (201, 204), put.text
            ids.append(self.native(calendar, f"{uid}.ics"))
        return ids

    def _radicale_path(self, native_id: str) -> str:
        path = unquote(httpx.URL(native_id.split("\n")[0]).path)
        return path[len(self.prefix):] if self.prefix and path.startswith(self.prefix) else path

    def mutate(self, klass: str, native_id: str, **changes: Any) -> None:
        """Change one event as another device would: `title`, `start`,
        `end`; `gone=True` deletes it."""
        assert klass == "calendar", klass
        path = self._radicale_path(native_id)
        if changes.get("gone"):
            assert self._direct.delete(path).status_code in (200, 204)
            return
        got = self._direct.get(path)
        assert got.status_code == 200, got.text
        cal = icalendar.Calendar.from_ical(got.text)
        [event] = list(cal.walk("VEVENT"))
        for key, prop in (("title", "summary"), ("start", "dtstart"), ("end", "dtend")):
            if key in changes:
                event.pop(prop.upper(), None)
                event.add(prop, changes[key])
        put = self._direct.put(path, content=cal.to_ical(),
                               headers={"Content-Type": "text/calendar"})
        assert put.status_code in (201, 204), put.text

    def snapshot(self, klass: str) -> dict[str, Any]:
        """Every event on every calendar, by UID: where it is, its etag."""
        assert klass == "calendar", klass
        out: dict[str, Any] = {}
        for calendar in self.calendars:
            answer = self._direct.request("REPORT", self.path(calendar) + "/", content=_ALL,
                                          headers={"Depth": "1"})
            for href, etag, data in re.findall(
                    r"<href>([^<]+)</href>.*?<getetag>([^<]+)</getetag>.*?"
                    r"<C:calendar-data>(.*?)</C:calendar-data>", answer.text, re.DOTALL):
                uid = _uid_of(data.encode())
                out[uid] = (unquote(href), etag)
        return out

    def raw(self, native_id: str) -> Any:
        """The event's calendar as stored, for a test to look inside."""
        got = self._direct.get(self._radicale_path(native_id))
        return icalendar.Calendar.from_ical(got.text) if got.status_code == 200 else None


class CalNetwork(modctx.Network):
    """Every name resolves to one public address; each host the rig serves
    is the layer in front of Radicale; any other is recorded and refused."""

    def __init__(self, habits: Habits, served: tuple[str, ...]) -> None:
        self.habits = habits
        self.served = served
        self.reached: list[str] = []

    def resolve(self, host: str, port: int) -> list[str]:
        return [PUBLIC_IP]

    def own_ports(self) -> set[int]:
        return {54321}  # Steve's own, as a run file would name it

    def transport_for(self, host: str) -> httpx.BaseTransport | None:
        self.reached.append(host)
        if host not in self.served:
            raise AssertionError(f"a request reached {host}, which this rig does not serve")
        return httpx.WSGITransport(app=self.habits)


@dataclass
class CalRig:
    world: CalWorld
    habits: Habits
    network: CalNetwork
    folder: Any = None
    made: list[Any] = field(default_factory=list)

    def provider(self, manifest: Manifest, *, address: str | None = None,
                 preset: str | None = None, fields: dict | None = None,
                 password: str | None = None, key: str = "acct0002",
                 module_key: str = "m0dkey0002"):
        from core.engines import caldav

        account = Account(key=key, name="Calendar", module=manifest.id,
                          address=address or self.world.user, preset=preset,
                          fields=fields or {})
        for name in manifest.secrets:
            credstore.write(credstore.target(module_key, key, name),
                            password if password is not None else self.world.password)
        ctx = modctx.ModuleContext(manifest=manifest, module_key=module_key, account=account,
                                   state_dir=None, network=self.network,
                                   zone=lambda: dt.UTC)
        self.made.append(ctx)
        return caldav.engine(ctx, manifest.specs["calendar"])

    def close(self) -> None:
        for ctx in self.made:
            ctx.close()
        if self.folder is not None:
            self.folder.cleanup()


def rig(mode: str = STANDARD, *, host: str = "caldav.mail.example",
        user: str = "maya@mail.example", password: str = "secret-pw",
        calendars: tuple[str, ...] = ("home",), principal_host: str = "",
        ignores_filters: bool | None = None,
        also: tuple[str, ...] = (), prefix: str | None = None,
        folder: Path | None = None) -> CalRig:
    """A rig serving `host` (and `also`), in `mode`, under `prefix` (Google's
    by default in its mode, else the root). Radicale keeps its store
    in `folder`, or in a folder of its own removed on `close`."""
    own = None
    if folder is None:
        from core import paths

        own = tempfile.TemporaryDirectory(dir=paths.scratch_dir())
        folder = Path(own.name)
    if prefix is None:
        prefix = GOOGLE_PREFIX if mode == GOOGLE else ""
    # Events are reached on the host discovery ends on.
    world = CalWorld(folder, user=user, password=password,
                     base=f"https://{principal_host or host}",
                     calendars=calendars, prefix=prefix)
    habits = Habits(world.app, mode=mode, principal_host=principal_host, prefix=prefix,
                    ignores_filters=ignores_filters)
    served = (host, *also, *((principal_host,) if principal_host else ()))
    return CalRig(world, habits, CalNetwork(habits, served), own)
