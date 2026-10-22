"""Calendar links: a calendar read by its private address, never written.

Many calendars give a user no password and no API, only a link: a course
site's due dates, a team's rota, a calendar shared from Outlook or iCloud.
The personal edition read one such feed, a course site's, under a key and
a parser of its own; this reads any, as an account of the `ics` module
(05-connectors.md, section 5).

- **The link is the credential.** Anyone holding it reads the calendar, so
  it is a secret field, kept in the Credential Manager, and no error, log
  line or health sentence carries it. Only its host is kept with the
  account, pinned at setup as a typed server is (`agent/accounts.py`).
- **HTTPS only.** `webcal://`, which Apple's and Outlook's "subscribe"
  links use, is the same address over HTTPS; anything else is refused
  before a request.
- **Read at most every ten minutes** for each account, however many tools
  and sources ask in a turn; the health check reads it fresh.
- **Zones and repeats** are `core/engines/ical.py`'s: a Windows zone name
  is mapped, and an unknown one is read as the user's own and named in the
  health check rather than raised.
- **Due dates.** An account whose `due_dates` field is on declares
  `DUE_DATES`, and the slot reads its events as things due
  (`agent/calendars.due`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.engines import ical
from core.modctx import Refused
from steve_sdk import classes
from steve_sdk.classes import CalendarProvider, Event
from steve_sdk.errors import ConnectorError, NotConnected, Transient, WrongPassword

TIMEOUT_S = 30.0
CACHE_S = 10 * 60
# Each account's feed as last read: (when, the calendar, its unknown zones),
# by the module's and the account's keys and a digest of the link, so a
# link given again is read again.
_read: dict[tuple[str, str, str], tuple[float, Any, frozenset[str]]] = {}
_clock = time.monotonic


def forget() -> None:
    """Drop every feed read: the next read of each goes to its server."""
    _read.clear()


def https(link: str) -> str:
    """The address to read: `webcal://` as `https://`. Anything but HTTPS
    is refused, saying so without the link."""
    parts = urlsplit(link.strip())
    scheme = parts.scheme.lower()
    if scheme == "webcal":
        scheme = "https"
    if scheme != "https" or not parts.hostname:
        raise ConnectorError("the calendar's link is not an https:// or webcal:// link")
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))


class IcsCalendar(CalendarProvider):
    """One calendar link: read whole, its events in any range, nothing written."""

    def __init__(self, ctx: Any, spec: Any) -> None:
        self.ctx = ctx
        account = ctx.account
        settings = ctx.manifest.settings("calendar", account.preset)
        due = (account.fields or {}).get("due_dates", settings.get("due_dates", False))
        self.capabilities = frozenset({classes.DUE_DATES} if due is True else ())
        self.name = account.name
        self._secret = ctx.manifest.secrets[0] if ctx.manifest.secrets else ""

    # --- the feed

    def _feed(self, *, fresh: bool = False) -> tuple[Any, frozenset[str]]:
        link = (self.ctx.secret(self._secret) if self._secret else None) or ""
        if not link.strip():
            raise NotConnected("the calendar's link is not set; add the calendar again")
        held_at = (self.ctx.module_key, self.ctx.account.key,
                   hashlib.sha256(link.strip().encode()).hexdigest())
        held = _read.get(held_at)
        if held is not None and not fresh and _clock() - held[0] < CACHE_S:
            return held[1], held[2]
        answer = self._get(https(link))
        parsed = ical.parse(answer.text, "the calendar from its link")
        unknown = frozenset(ical.unknown_zones(parsed))
        _read[held_at] = (_clock(), parsed, unknown)
        return parsed, unknown

    def _get(self, url: str) -> Any:
        # No message here holds the link or an exception's own text, which
        # may: the link is the credential. A redirect is followed on the
        # link's own host, each hop checked again; one to another host is
        # refused by the host pinned at setup, and says only that host.
        try:
            answer = self.ctx.http.request("GET", url, headers={"Accept": "text/calendar"},
                                           timeout=TIMEOUT_S, follow=True)
        except Refused as exc:
            raise ConnectorError(f"the calendar's link was refused here: {exc}"[:300]) from None
        except httpx.HTTPError as exc:
            raise Transient(f"the calendar's server did not answer "
                            f"({type(exc).__name__})") from None
        if answer.status == 200:
            return answer
        if answer.status in (401, 403, 404, 410):
            raise WrongPassword(f"the calendar's server no longer takes its link "
                                f"({answer.status}); it may have been reset: copy it again")
        if answer.status == 429 or answer.status >= 500:
            raise Transient(f"the calendar's server is busy or down ({answer.status})")
        raise ConnectorError(f"the calendar's server refused the link ({answer.status})")

    def _event(self, c: Any, repeating: bool) -> Event:
        which = ical.occurrence(c, repeating)
        uid = ical.text(c, "UID")
        return ical.event(c, zone=self.ctx.zone,
                          native_id=f"{uid}\n{which}" if which else uid,
                          calendar=self.name, repeating=repeating)

    def _series(self, parsed: Any) -> dict[str, bool]:
        """Each UID, and whether its event repeats (a moved occurrence is a
        component of its own, under its series' UID)."""
        found: dict[str, list[Any]] = {}
        for c in parsed.walk("VEVENT"):
            found.setdefault(ical.text(c, "UID"), []).append(c)
        return {uid: ical.repeats(cs) for uid, cs in found.items()}

    # --- reading

    def events(self, start: dt.datetime, end: dt.datetime, *, limit: int) -> list[Event]:
        parsed, _ = self._feed()
        series = self._series(parsed)
        return [self._event(c, series.get(ical.text(c, "UID"), False))
                for c in ical.between(parsed, start, end)][:limit]

    def fetch(self, native_id: str) -> Event | None:
        uid, _, which = (native_id or "").partition("\n")
        parsed, _ = self._feed()
        series = self._series(parsed)
        if uid not in series:
            return None
        if not which:
            if series[uid]:
                return None
            found = [c for c in parsed.walk("VEVENT") if ical.text(c, "UID") == uid]
            return self._event(found[0], False) if len(found) == 1 else None
        around = [self._event(c, True) for c in ical.around(parsed, which, self.ctx.zone)]
        return next((e for e in around if e.native_id == native_id), None)

    def test(self) -> str:
        parsed, unknown = self._feed(fresh=True)
        now = dt.datetime.now(ical.UTC)
        soon = ical.between(parsed, now, now + dt.timedelta(days=14))
        said = f"read the calendar: {len(soon)} event(s) in the next two weeks"
        if unknown:
            named = ", ".join(sorted(unknown)[:3])
            said += (f"; some of its times name a time zone Steve does not know ({named}), "
                     "and are read as yours")
        return said


def engine(ctx: Any, spec: Any) -> CalendarProvider:
    if spec.klass != "calendar":
        raise ValueError(f"the ICS engine reads calendars, not {spec.klass}")
    return IcsCalendar(ctx, spec)
