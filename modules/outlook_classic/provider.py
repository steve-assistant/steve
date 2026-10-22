"""Outlook on this computer as an account the user adds, providing the mail
and calendar classes.

`outlook.py` and `calendar.py`, beside this, answered in dicts of their
own, with failure as one more dict, and tagged every id `outlook:` so that
`read_email` could tell it from Gmail's. These adapters put the same calls
behind the slot's interfaces (`steve_sdk.classes`), so the tools can be
written once per class (step 5, item 4c); the golden outputs
(tests/unit/test_golden_connectors.py) hold the calls beneath to what they
did before.

Until step 5's item 10 they stood in as an account no one added, behind a
switch, and reached Outlook through a COM object of their own. Now each is
built for the account the user added (`core/engines/code.py`), from its
context, and every call runs as that account's (`outlook.as_account`), so
the functions beneath reach Outlook only through `ctx.com`. The test says
when Outlook cannot be used, in words the user can act on
(`outlook.unusable`), and finds the address of every account in Outlook's
profile, all of which this one account merges (`addresses`).

A message's native id is its EntryID, without the tag: the slot's id names
the account instead. A calendar entry's is the `outlook-` hash of its series
and start, which no call can look up, so this calendar is read-only.

Outlook is one process answering COM, and two walks of its folders at once
only take turns inside it, where a caller cannot give up waiting; so its
providers take turns here instead (`shareable = False`).
"""

from __future__ import annotations

import datetime as dt
import functools
import math
from collections.abc import Callable
from typing import Any

from steve_sdk import classes
from steve_sdk.classes import CalendarProvider, Draft, Event, Guest, MailProvider, Message
from steve_sdk.errors import ConnectorError, NotConnected, Transient, Unsupported

from . import calendar as outlook_calendar
from . import outlook


def _error(said: dict) -> ConnectorError:
    return ConnectorError(str(said.get("error") or ""))


def _answer(said: dict, *, gone: bool = False) -> dict | None:
    """A function's answer, or its error raised; None for an id that finds
    nothing, where `gone` allows it (`read` calls that a usage error)."""
    if not isinstance(said, dict) or not said.get("error"):
        return said
    if gone and said.get("kind") == "usage":
        return None
    raise _error(said)


def _calling(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except ConnectorError:
        raise
    except TimeoutError as exc:
        raise Transient(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - every failure becomes the slot's
        raise (outlook.unusable(exc)
               or ConnectorError(f"{type(exc).__name__}: {exc}"[:300])) from exc


def _days(since: dt.datetime | None, default: int) -> int:
    """`since` as the whole days back today's functions take."""
    if since is None:
        return default
    back = dt.datetime.now(since.tzinfo) - since
    return max(1, math.ceil(back.total_seconds() / 86_400))


class _Outlook:
    """One Outlook account's provider, built from its context. Each public
    method runs as its account's: the only way the functions beneath reach
    Outlook."""

    shareable = False

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name, fn in list(vars(cls).items()):
            if callable(fn) and not name.startswith("_"):
                setattr(cls, name, _as_account(fn))

    def addresses(self) -> list[str]:
        """Every account's in Outlook's profile: this account merges them all,
        so each is held to it (core's one-address rule)."""
        with outlook.as_account(self.ctx):
            return [a for a in _calling(outlook.accounts) if "@" in a]


def _as_account(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def calling(self: _Outlook, *args: Any, **kwargs: Any) -> Any:
        with outlook.as_account(self.ctx):
            return fn(self, *args, **kwargs)

    return calling


# --- mail ------------------------------------------------------------------------

_DIRECTION = {"received": "in", "sent": "out"}

# How many sent items are read to find the few that went to one person.
_SCAN = 150

# How much of a body a read gives: the write path's read of a draft, and
# the same draft's read again as it is sent, cut alike to compare alike.
_READ_CHARS = 20_000


def _mail(found: dict) -> Message:
    tagged = str(found.get("id") or "")
    return Message(
        native_id=outlook.untag(tagged) or tagged, thread=str(found.get("thread_id") or ""),
        sender=str(found.get("from") or ""), to=str(found.get("to") or ""),
        cc=str(found.get("cc") or ""), bcc=str(found.get("bcc") or ""),
        subject=str(found.get("subject") or ""), when=int(found.get("internal_date") or 0),
        snippet=str(found.get("snippet") or ""), body=found.get("body"),
        body_length=int(found.get("body_length") or 0), unread=bool(found.get("unread")),
        folder=str(found.get("folder") or ""),
        direction=_DIRECTION.get(str(found.get("direction") or ""), ""))


class OutlookMail(_Outlook, MailProvider):
    capabilities = frozenset({classes.SEND})

    def test(self) -> str:
        found = _calling(outlook.accounts)
        if not found:
            raise NotConnected("Outlook has no account in it yet: add one in Outlook, "
                               "then test again")
        return f"Outlook answers for {', '.join(found)}"

    def search(self, query: str, *, limit: int, since: dt.datetime | None = None
               ) -> list[Message]:
        days = _days(since, outlook.SEARCH_DAYS)
        said = _answer(_calling(lambda: outlook.search(query, limit, days=days)))
        return [_mail(m) for m in said.get("messages", [])]

    def ignored(self, query: str) -> list[str]:
        return list(outlook.parse_query(query).ignored)

    def arrived(self, *, since: dt.datetime, limit: int,
                seen: Callable[[list[str]], set[str]]) -> list[Message]:
        """A walk of the local cache back to `since`. It walks Sent as well
        as the inbox, so it lists more than it keeps: the sweep this
        replaced handed the user's own outgoing mail to the judge as new."""
        days = _days(since, outlook.SEARCH_DAYS)
        said = _answer(_calling(lambda: outlook.search("", round(limit * 1.6), days=days)))
        received = [_mail(m) for m in said.get("messages", []) if m.get("direction") != "sent"]
        old = seen([m.native_id for m in received])
        return [m for m in received if m.native_id not in old]

    def fetch(self, native_id: str, *, body_chars: int = _READ_CHARS) -> Message | None:
        said = _answer(outlook.read(outlook.tag(native_id), body_chars), gone=True)
        return None if said is None else _mail(said)

    def make_draft(self, draft: Draft) -> Message:
        """The draft as Outlook holds it once saved, read back: Outlook
        resolves the recipients and makes its own text of the body, and a
        send compares the draft read then with its card (`core.writes`).
        Built from the arguments, as it was, its To line was Outlook's
        names where a read gives addresses, so a card made from it matched
        no read of the draft, and the send was refused."""
        if draft.reply_to:
            said = outlook.draft_reply(outlook.tag(draft.reply_to), draft.body,
                                       cc=draft.cc, bcc=draft.bcc)
        else:
            said = outlook.draft_new(draft.to, draft.subject, draft.body,
                                     cc=draft.cc, bcc=draft.bcc)
        said = _answer(said)
        made = self.fetch(outlook.untag(said["draft_id"]) or said["draft_id"])
        if made is None:
            raise ConnectorError("Outlook saved the draft but could not read it back")
        made.draft, made.direction = True, "out"
        made.extra = {"where": str(said.get("where") or "")}
        return made

    def send_draft(self, message: Message) -> Message:
        """The draft goes only if, read again in the call that sends it, it
        is still the one the write path checked against its card: Outlook
        sends an item as it is by then (`outlook.send_draft`)."""
        checked = message.digest()
        said = _answer(outlook.send_draft(
            outlook.tag(message.native_id), body_chars=_READ_CHARS,
            unchanged=lambda now: _mail(now).digest() == checked))
        # Sent, it is a new item in Sent Items, whose EntryID this does not learn.
        return Message(native_id="", to=str(said.get("to") or ""),
                       subject=str(said.get("subject") or ""), direction="out")

    def delete(self, message: Message) -> None:
        # Drafts only: `delete_draft` refuses a sent message itself.
        _answer(outlook.delete_draft(outlook.tag(message.native_id)))

    def sent_items(self, *, since: dt.datetime, limit: int, to: str = "") -> list[Message]:
        """Sent Items, the one folder `sent_with_bodies` reads. Outlook's
        cache has no "to:" filter that returns bodies, so for one recipient
        a longer stretch is read and what went to them kept."""
        said = outlook.sent_with_bodies(_days(since, 1), max(limit, _SCAN) if to else limit)
        if said.get("error"):
            raise _error(said)
        found = [_mail(dict(m, direction="sent")) for m in said.get("messages", [])]
        if to:
            found = [m for m in found if to.lower() in m.to.lower()][:limit]
        return found

    def envelopes(self, *, sent: bool, since: dt.datetime, limit: int) -> list[Message]:
        """A walk of Sent or the inbox in the local cache, without bodies."""
        direction = "sent" if sent else "received"
        said = _answer(_calling(lambda: outlook.headers(direction, _days(since, 1), limit)))
        return [_mail(dict(m, direction=direction)) for m in said.get("messages", [])]

    def headers(self, native_id: str) -> dict[str, str]:
        raise Unsupported("the headers of an Outlook message")


# --- calendar ----------------------------------------------------------------------

def _event(found: dict) -> Event:
    all_day = bool(found.get("all_day"))

    def moment(text: str | None) -> dt.datetime | dt.date | None:
        if not text:
            return None
        return dt.date.fromisoformat(text) if all_day else dt.datetime.fromisoformat(text)

    attendees = found.get("attendees") or []
    mine = next((a for a in attendees if a.get("self")), {})
    extra = {"series": found.get("series") or "",
             "guest_count": str(found.get("attendee_count") or 0),
             "my_response": mine.get("response") or "",
             "organizer_self": "yes" if found.get("organizer_self") else "",
             "updated": found.get("updated") or "", "created": found.get("created") or ""}
    return Event(
        native_id=str(found.get("id") or ""), title=str(found.get("summary") or ""),
        start=moment(found.get("start")), end=moment(found.get("end")), all_day=all_day,
        location=str(found.get("location") or ""),
        description=str(found.get("description") or ""),
        guests=tuple(Guest(email=str(a.get("email") or ""), name=str(a.get("name") or ""),
                           response=str(a.get("response") or ""))
                     for a in attendees if not a.get("self")),
        organizer=str(found.get("organizer") or ""), status=str(found.get("status") or ""),
        recurring=bool(found.get("recurring")), extra={k: v for k, v in extra.items() if v})


class OutlookCalendar(_Outlook, CalendarProvider):
    def test(self) -> str:
        found = _calling(outlook_calendar.calendars)
        return f"Outlook's calendar answers, with {found} calendar{'s' * (found != 1)}"

    def events(self, start: dt.datetime, end: dt.datetime, *, limit: int) -> list[Event]:
        said = _calling(lambda: outlook_calendar.calendar(start, end, limit=limit))
        if said.get("error"):
            raise _error(said)
        return [_event(e) for e in said.get("events", [])]

    def search(self, words: list[str], start: dt.datetime, end: dt.datetime, *,
               limit: int) -> list[Event]:
        def match(item) -> bool:
            # The cheap properties only -- names as Outlook writes them -- so a
            # year of calendar is not a year of address lookups.
            text = " ".join(str(getattr(item, name, "") or "") for name in (
                "Subject", "Location", "Organizer", "RequiredAttendees", "OptionalAttendees",
            )).lower()
            return all(w in text for w in words)

        said = _calling(lambda: outlook_calendar.calendar(start, end, limit=limit, match=match))
        if said.get("error"):
            raise _error(said)
        return [_event(e) for e in said.get("events", [])]

    def fetch(self, native_id: str) -> Event | None:
        found = _answer(_calling(lambda: outlook_calendar.entry(native_id)))["event"]
        return None if found is None else _event(found)

