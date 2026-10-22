"""The four connector classes: what every provider of each one fills in.

Mail, calendar, files and tasks were each written per provider, with their
own shapes: a Gmail message was Google's JSON summarised one way, an
Outlook one a COM item summarised another, and the password mailbox's a third, so
every caller learned three. And each provider made its own write checks,
or none: the password mailbox's delete was a bare EXPUNGE, and Google's calendar
updates carried no etag.

So each class is a small interface here. A provider fills a few methods
over plain items (`Message`, `Event`, `FileItem`, `Task`); core does the
rest around them -- ids, merging accounts, the write path
(`core/writes.py`), errors and health -- in its own process. A method the
provider cannot do is a capability it does not declare: core refuses it
before calling, naming the account, rather than letting the provider fake
it.

Every item carries its native id, its identifying field (`ident`: the
RFC Message-ID, the iCalendar UID) and its condition for a conditional
write (an etag, UIDVALIDITY with MODSEQ, a change key), or None where the
service offers none. The write path checks the first two on our side and
writes on the third: the 11 October lesson, in the interface.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from steve_sdk.errors import Unsupported

# --- capabilities: what a provider may declare beyond its class's minimum ---

# mail
LABELS = "labels"          # labels added and removed (Gmail's)
THREADS = "threads"        # a whole conversation by its thread id
FLAGS = "flags"            # read, unread, starred
MOVE = "move"              # to another folder
ARCHIVE = "archive"        # out of the inbox, kept
SEND = "send"              # a draft can be sent (classic Outlook's cannot always)
# calendar
CREATE = "create"
UPDATE = "update"
DELETE = "delete"
GUESTS = "guests"          # invitations: events with other people on them
FREEBUSY = "freebusy"      # the server's own free/busy answer
DUE_DATES = "due_dates"    # its events are things due (a course site's feed), not time taken
# files
DOCUMENTS = "documents"    # documents created and edited
SPREADSHEETS = "spreadsheets"
PRESENTATIONS = "presentations"
# every class
CONDITIONAL = "conditional"  # updates and deletes can be made conditional

MAIL_CAPABILITIES = frozenset({LABELS, THREADS, FLAGS, MOVE, ARCHIVE, SEND, CONDITIONAL})
CALENDAR_CAPABILITIES = frozenset({CREATE, UPDATE, DELETE, GUESTS, FREEBUSY, DUE_DATES,
                                   CONDITIONAL})
FILES_CAPABILITIES = frozenset({DOCUMENTS, SPREADSHEETS, PRESENTATIONS})
TASKS_CAPABILITIES = frozenset({CONDITIONAL})


def _digest(parts: dict[str, Any]) -> str:
    text = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _addresses(text: str) -> list[str]:
    return sorted(a.strip().lower() for a in (text or "").split(",") if a.strip())


# --- items ---------------------------------------------------------------------

@dataclass
class Message:
    native_id: str
    ident: str = ""                 # the RFC Message-ID
    condition: str | None = None
    thread: str = ""
    sender: str = ""
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    when: int = 0                   # epoch milliseconds
    snippet: str = ""
    body: str | None = None         # None: not fetched
    body_length: int = 0
    unread: bool = False
    labels: tuple[str, ...] = ()
    folder: str = ""
    attachments: tuple[str, ...] = ()
    direction: str = ""             # "in" or "out", where the provider knows
    draft: bool = False
    extra: dict[str, str] = field(default_factory=dict)  # list headers and the like

    def digest(self) -> str:
        """What a card shows of a message to be sent, bound: every recipient,
        the subject, the attachments and the body. Any change asks again."""
        return _digest({"to": _addresses(self.to), "cc": _addresses(self.cc),
                        "bcc": _addresses(self.bcc), "subject": self.subject.strip(),
                        "attachments": sorted(self.attachments),
                        "body": (self.body or "").strip()})


@dataclass
class Draft:
    """A message to be made: core fills it from a tool's arguments."""

    to: str
    subject: str
    body: str
    cc: str = ""
    bcc: str = ""
    reply_to: str = ""              # the native id of the message answered
    attachments: tuple[str, ...] = ()
    sender: str = ""                # a plus-address of the account's, for the own mailbox


@dataclass
class Guest:
    email: str
    name: str = ""
    response: str = ""
    room: bool = False              # a room or resource, not a person


@dataclass
class Event:
    native_id: str
    ident: str = ""                 # the iCalendar UID
    condition: str | None = None
    title: str = ""
    start: dt.datetime | dt.date | None = None
    end: dt.datetime | dt.date | None = None
    all_day: bool = False
    location: str = ""
    description: str = ""
    guests: tuple[Guest, ...] = ()
    organizer: str = ""
    calendar: str = ""
    link: str = ""                  # the event's page, or its meeting link
    recurring: bool = False
    status: str = ""
    busy: bool = True
    extra: dict[str, str] = field(default_factory=dict)

    def digest(self) -> str:
        """What a card shows of an event, bound: its title, times and guests."""
        return _digest({"title": self.title.strip(), "start": self.start, "end": self.end,
                        "all_day": self.all_day,
                        "guests": sorted(g.email.lower() for g in self.guests)})


@dataclass
class FileItem:
    native_id: str
    ident: str = ""                 # none where the service has no identity beyond the id
    name: str = ""
    mime: str = ""
    modified: str = ""
    link: str = ""
    owners: tuple[str, ...] = ()
    text: str | None = None
    truncated: bool = False
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class TaskList:
    native_id: str
    title: str = ""


@dataclass
class Task:
    native_id: str
    ident: str = ""                 # none where the service has no identity beyond the id
    list_id: str = ""
    title: str = ""
    notes: str = ""
    due: str | None = None          # an ISO date
    done: bool = False
    condition: str | None = None
    link: str = ""


def _searchable(event: Event) -> str:
    """What a calendar search looks in: the event's title, place, organizer
    and guests, lower case."""
    guests = " ".join(f"{g.name} {g.email}" for g in event.guests)
    return f"{event.title} {event.location} {event.organizer} {guests}".lower()


# --- providers ---------------------------------------------------------------------

class Provider(ABC):  # noqa: B024 - each class's interface below adds the abstract methods
    """What every provider of every class has.

    `shareable` is False where the provider's client cannot be used from two
    threads at once (Google's cannot): core then calls that account one
    call at a time. `capabilities` lists what it can do beyond its class's
    minimum; core refuses anything else before calling."""

    klass: ClassVar[str] = ""
    capabilities: frozenset[str] = frozenset()
    shareable: bool = True

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def neighbours(self, item: Any) -> dict[str, str | None] | None:
        """The items next to `item` -- same calendar day, same folder -- as
        ident to condition, for the write path to check that exactly one
        item changed. None where the provider cannot list them cheaply."""
        return None

    def test(self) -> str:
        """Connect and read once, for setup's test and the health check;
        a sentence of what was found. Raises a ConnectorError."""
        raise Unsupported("this provider has no test")

    def expires(self) -> dt.datetime | None:
        """When the account's sign-in is known to end (aware), so that
        core can say so a day before; None when it has no known end."""
        return None

    def addresses(self) -> list[str]:
        """The addresses the account answers at that only the account
        itself knows, asked after a passing test: every account in classic
        Outlook's profile, where the user typed one. Core holds each to one
        account per class. None beyond the one typed or signed in, here."""
        return []


class MailProvider(Provider):
    klass = "mail"

    @abstractmethod
    def search(self, query: str, *, limit: int, since: dt.datetime | None = None
               ) -> list[Message]:
        """Messages matching `query`, in Gmail's search syntax, newest first,
        without bodies."""

    def arrived(self, *, since: dt.datetime, limit: int,
                seen: Callable[[list[str]], set[str]]) -> list[Message]:
        """Mail received since `since`, newest first, without bodies, less
        the native ids `seen` says were observed before: the attention
        loop's poll, every few minutes. A provider that can list ids cheaply
        lists them, asks `seen`, and fetches only the rest, so a poll that
        finds nothing new costs one call; this default asks `search`."""
        found = [m for m in self.search("", limit=limit, since=since)
                 if m.direction != "out" and not m.draft]
        old = seen([m.native_id for m in found])
        return [m for m in found if m.native_id not in old]

    @abstractmethod
    def fetch(self, native_id: str, *, body_chars: int = 20_000) -> Message | None:
        """One message by its native id, with its body; None if it is gone."""

    @abstractmethod
    def make_draft(self, draft: Draft) -> Message:
        """Save a draft; the stored draft, with its native id and Message-ID."""

    @abstractmethod
    def send_draft(self, message: Message) -> Message:
        """Send the one draft the write path fetched and checked."""

    @abstractmethod
    def delete(self, message: Message) -> None:
        """Delete the one message the write path fetched and checked: to
        Trash, or by UID, never by a search."""

    @abstractmethod
    def sent_items(self, *, since: dt.datetime, limit: int, to: str = "") -> list[Message]:
        """What the user sent since `since`, with bodies: the only mail whose
        text reaches learning and style unredacted. With `to`, only what was
        sent to that one address, up to `limit` of those: what a draft is
        shown of how the user writes to that person."""

    @abstractmethod
    def headers(self, native_id: str) -> dict[str, str]:
        """The headers of one message, as the server received it: the
        topmost value of each name, which is the one the receiving server
        wrote, never one further down that the sender could have."""

    def envelopes(self, *, sent: bool, since: dt.datetime, limit: int) -> list[Message]:
        """Who, to whom, when and what subject, for the mail sent (or
        received) since `since`, newest first and without bodies: what the
        people ledger and the thread watcher read, a whole window at a time,
        every night. An account that cannot list a window cheaply leaves it
        unsupported, and the ledger does without its mail."""
        raise Unsupported("listing a window of mail")

    def ignored(self, query: str) -> list[str]:
        """The parts of `query` this account's search drops rather than
        honours (`label:`, say, where there are no labels). Dropping widens
        the answer; a search that said nothing would pass it off as exact."""
        return []

    def thread(self, thread_id: str, *, limit: int) -> list[Message]:
        raise Unsupported("threads")

    def modify(self, message: Message, *, read: bool | None = None, archive: bool = False,
               add_labels: tuple[str, ...] = (), remove_labels: tuple[str, ...] = ()
               ) -> Message:
        raise Unsupported("changing a message")

    def move(self, message: Message, folder: str) -> Message:
        raise Unsupported("moving a message")


class CalendarProvider(Provider):
    klass = "calendar"

    @abstractmethod
    def events(self, start: dt.datetime, end: dt.datetime, *, limit: int) -> list[Event]:
        """Events overlapping [start, end), repeats expanded, in any order:
        core checks the range again on its side."""

    @abstractmethod
    def fetch(self, native_id: str) -> Event | None:
        """One event by its native id; None if it is gone."""

    def create(self, event: Event) -> Event:
        raise Unsupported("adding events")

    def update(self, event: Event) -> Event:
        """Write the one event the write path fetched and checked, with
        `event.condition` as the condition where there is one."""
        raise Unsupported("changing events")

    def delete(self, event: Event) -> None:
        raise Unsupported("deleting events")

    def freebusy(self, start: dt.datetime, end: dt.datetime
                 ) -> list[tuple[dt.datetime, dt.datetime]]:
        raise Unsupported("the server's free/busy")

    def search(self, words: list[str], start: dt.datetime, end: dt.datetime, *,
               limit: int) -> list[Event]:
        """Events in [start, end) whose title, place, organizer or guests hold
        every one of `words` (lower case). By default the range is read and
        searched here; a server that searches for itself should, since a
        year of events read whole is slow and may be cut."""
        found = [event for event in self.events(start, end, limit=max(limit, 2_500))
                 if all(word in _searchable(event) for word in words)]
        return found[:limit]

    def changed(self, since: dt.datetime, start: dt.datetime, end: dt.datetime, *,
                limit: int) -> list[Event]:
        """Events in [start, end) added, moved or cancelled since `since`:
        what the attention loop tells the user is new. Its `extra["updated"]`
        says when, as ISO time. By default the range is read and kept by
        that, a series once (`extra["series"]`): one edit to a series is
        every occurrence. A server that lists changes, deletions included,
        should: a cancellation is the most useful thing this finds, and a
        deleted event is not in `events`."""
        kept: list[Event] = []
        series: set[str] = set()
        for event in self.events(start, end, limit=max(limit, 500)):
            try:
                touched = dt.datetime.fromisoformat(event.extra.get("updated") or "")
            except ValueError:
                continue
            if touched.tzinfo is None:
                touched = touched.replace(tzinfo=since.tzinfo)
            run = event.extra.get("series") or ""
            if touched < since or (run and run in series):
                continue
            series.add(run)
            kept.append(event)
        return kept[:limit]


class FilesProvider(Provider):
    klass = "files"

    @abstractmethod
    def search(self, query: str, *, limit: int, mime: str | None = None) -> list[FileItem]:
        """Files matching `query`, by name and text, newest first."""

    @abstractmethod
    def read(self, native_id: str, *, max_chars: int) -> FileItem:
        """One file as text, cut to `max_chars`."""

    def recent(self, *, limit: int) -> list[FileItem]:
        return self.search("", limit=limit)

    def create_document(self, title: str, text: str) -> FileItem:
        raise Unsupported("documents")

    def create_spreadsheet(self, title: str, rows: list[list[str]]) -> FileItem:
        raise Unsupported("spreadsheets")

    def read_spreadsheet(self, native_id: str, *, cells: str) -> list[list[str]]:
        raise Unsupported("spreadsheets")

    def update_spreadsheet(self, native_id: str, *, cells: str, rows: list[list[str]]
                           ) -> FileItem:
        raise Unsupported("spreadsheets")


class TasksProvider(Provider):
    klass = "tasks"

    @abstractmethod
    def lists(self) -> list[TaskList]:
        ...

    @abstractmethod
    def tasks(self, list_id: str | None = None, *, open_only: bool = True, limit: int = 50
              ) -> list[Task]:
        ...

    @abstractmethod
    def fetch(self, native_id: str, list_id: str | None = None) -> Task | None:
        ...

    @abstractmethod
    def create(self, task: Task) -> Task:
        ...

    @abstractmethod
    def update(self, task: Task) -> Task:
        ...

    @abstractmethod
    def complete(self, task: Task) -> Task:
        ...


INTERFACES: dict[str, type[Provider]] = {
    "mail": MailProvider, "calendar": CalendarProvider,
    "files": FilesProvider, "tasks": TasksProvider,
}
CAPABILITIES: dict[str, frozenset[str]] = {
    "mail": MAIL_CAPABILITIES, "calendar": CALENDAR_CAPABILITIES,
    "files": FILES_CAPABILITIES, "tasks": TASKS_CAPABILITIES,
}
