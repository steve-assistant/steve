"""A Google account's providers of the connector slot's classes.

The tools called `google_gmail`, `google_calendar`, `google_drive` and
`google_tasks` by name, and each answered in its own dicts, with failure as
one more dict carrying a `kind` that each caller read its own way. These
adapters put the same calls behind the slot's interfaces
(`steve_sdk.classes`), answering in the slot's items and raising the slot's
errors, so that the tools can be written once per class (step 5, item 4c)
and an account of any module can stand where Google stood. The calls beneath
are today's, unchanged; the golden outputs (tests/unit/test_golden_connectors.py)
hold them to it. They moved into this module in item 4f.

Item 9 made Google an account like any other. Each provider is built by
core's `code` engine for one account, from its context (`core/engines/
code.py`), and holds that account's sign-in (`auth.Account`). Every public
method runs its calls as that account's (`common.as_account`), so two
Google accounts each read their own mail, and the calls beneath, written
for one account, needed no new argument.

A native id here is the id Google gave, untouched: a message's, or for a
draft the draft's own (Gmail's `r...`), which is what sending or deleting
one takes. Google's clients cannot be shared between threads (httplib2), so
every provider here says `shareable = False` and core calls it one call at a
time.
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import email.policy
import functools
import logging
import math
import re
from collections.abc import Callable
from email.utils import getaddresses
from typing import Any

from steve_sdk import classes
from steve_sdk.classes import (
    CalendarProvider,
    Draft,
    Event,
    FileItem,
    FilesProvider,
    Guest,
    MailProvider,
    Message,
    Task,
    TaskList,
    TasksProvider,
)
from steve_sdk.errors import (
    ConnectorError,
    SignInNeeded,
    TargetChanged,
    Transient,
    Unsupported,
)

from . import auth
from . import calendar as google_calendar
from . import common as google_common
from . import drive as google_drive
from . import gmail as google_gmail
from . import tasks as google_tasks

log = logging.getLogger("worker")

# Setup's two hooks, which core's `code` engine looks for here: the client
# file kept as its id, secret and project, and the browser sign-in.
fields = auth.fields
sign_in = auth.sign_in


def _error(said: dict) -> ConnectorError:
    """The slot's error for one of today's error answers."""
    kind, text = said.get("kind"), str(said.get("error") or "")
    if kind == "auth":
        return SignInNeeded(text)
    if kind == "rate_limit" or (kind == "http" and int(said.get("status") or 0) >= 500):
        return Transient(text)
    if kind == "not_found":
        return TargetChanged(text)
    if kind == "http" and int(said.get("status") or 0) == 412:
        # An If-Match refused: the item changed since it was read.
        return TargetChanged(text)
    if kind == "unsupported":
        return Unsupported(text)
    if kind == "disabled":
        # Fixed on one page of the user's own project: that page, opened.
        account = google_common.current()
        page = f"apis/library/{said['service']}" if said.get("service") else "apis/library"
        return ConnectorError(text, fix=auth.console(page, account.project if account else ""))
    return ConnectorError(text)


def _answer(said: dict, *, gone: bool = False) -> dict | None:
    """A guarded function's answer, or its error raised; None for a missing
    item where `gone` allows it."""
    if not isinstance(said, dict) or not said.get("error"):
        return said
    if gone and said.get("kind") == "not_found":
        return None
    raise _error(said)


def _calling(fn: Callable[[], Any], *, gone: bool = False) -> Any:
    """`fn()`, a call on Google's client, with its failure as the slot's."""
    try:
        return fn()
    except (TimeoutError, ConnectionError) as exc:
        raise Transient(f"{type(exc).__name__}: {exc}"[:300]) from exc
    except Exception as exc:  # noqa: BLE001 - every failure becomes the slot's
        said = google_common.explain(exc)
        if gone and said.get("kind") == "not_found":
            return None
        raise _error(said) from exc


class _Google:
    """One Google account's provider: built from its context, holding its
    sign-in. Each public method a subclass defines runs as its account's."""

    shareable = False

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.account = auth.Account(ctx)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name, fn in list(vars(cls).items()):
            if callable(fn) and not name.startswith("_"):
                setattr(cls, name, _as_account(fn))

    def expires(self) -> dt.datetime | None:
        return self.account.ends()


def _as_account(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def calling(self: _Google, *args: Any, **kwargs: Any) -> Any:
        with google_common.as_account(self.account):
            return fn(self, *args, **kwargs)

    return calling


# --- mail ---------------------------------------------------------------------

def _is_draft(native_id: str) -> bool:
    # A draft's id is "r" and digits; a message's is hexadecimal, never "r".
    return native_id.startswith("r")


# The inbox the attention loop watches, without Gmail's own verdict that
# something is an advert. Not `category:updates`: bills, deliveries and
# account-security alerts live there, and those are some of the most useful
# things it can catch. Not `category:forums` either: group mailing lists --
# club events, class groups -- are exactly what the user asked to hear about.
WATCHED = "in:inbox newer_than:{days}d -category:promotions -category:social"

# What `sent_items` puts after Gmail's `to:`: an address and nothing else.
_BARE_ADDRESS = re.compile(r"[^\s()<>\"{}:]+@[^\s()<>\"{}:]+")

# The mailing headers `google_gmail.summary` reads, kept in a Message's
# `extra` for the attention loop's sender checks (`agent/attention/vet.py`).
_LISTING = ("list_id", "list_unsubscribe", "precedence", "auto_submitted")

# Gmail's own sorting (labelIds on every message, free with the headers
# already fetched), given to the sender checks as the account's: what kind
# of sender, and Gmail's word for it. Updates are bills, receipts and
# notices -- written by a system; Forums are lists a person posts to.
# Personal says nothing. The attention loop knew these labels itself until
# step 5's item 9, which no other account's mail could ever match.
_SORTED = {"CATEGORY_PROMOTIONS": ("automated", "Promotions"),
           "CATEGORY_SOCIAL": ("automated", "Social"),
           "CATEGORY_UPDATES": ("automated", "Updates"),
           "CATEGORY_FORUMS": ("group", "Forums")}


def _mail(found: dict, *, draft_id: str = "") -> Message:
    """One of `google_gmail`'s summaries as a Message."""
    labels = tuple(found.get("labels") or ())
    extra = {"date": str(found["date"])} if found.get("date") else {}
    extra.update({k: str(found[k]) for k in _LISTING if found.get(k)})
    if sorted_as := next((_SORTED[label] for label in labels if label in _SORTED), None):
        extra["sorted_as"], extra["filed_under"] = sorted_as
    if draft_id:
        extra["message_id"] = str(found.get("id") or "")
    return Message(
        native_id=draft_id or str(found.get("id") or ""), ident=str(found.get("rfc_id") or ""),
        thread=str(found.get("thread_id") or ""),
        sender=str(found.get("from") or ""), to=str(found.get("to") or ""),
        cc=str(found.get("cc") or ""), subject=str(found.get("subject") or ""),
        when=int(found.get("internal_date") or 0), snippet=str(found.get("snippet") or ""),
        body=found.get("body"), body_length=int(found.get("body_length") or 0),
        unread=bool(found.get("unread")), labels=labels,
        direction="out" if "SENT" in labels else "in",
        draft=bool(draft_id) or "DRAFT" in labels, extra=extra)


def _went_to(message: Message, address: str) -> bool:
    """Whether `address` is on one of the message's own recipient lines,
    in any case, as the IMAP engine's `sent_items` asks it."""
    lines = [line for line in (message.to, message.cc, message.bcc) if line]
    return address.lower() in {found.strip().lower() for _, found in getaddresses(lines)}


class GmailMail(_Google, MailProvider):
    capabilities = frozenset({classes.LABELS, classes.THREADS, classes.FLAGS,
                              classes.ARCHIVE, classes.SEND})

    def search(self, query: str, *, limit: int, since: dt.datetime | None = None
               ) -> list[Message]:
        if since is not None:
            query = f"{query} after:{int(since.timestamp())}".strip()
        said = _answer(google_gmail.search(query, limit))
        return [_mail(m) for m in said.get("messages", [])]

    def arrived(self, *, since: dt.datetime, limit: int,
                seen: Callable[[list[str]], set[str]]) -> list[Message]:
        """The watched inbox (`WATCHED`): one call lists its ids, and only
        those `seen` does not know are fetched, each one call. One that
        cannot be read is left out, unseen, and tried again next poll: one
        unreadable message is not the mailbox."""
        back = dt.datetime.now(since.tzinfo) - since
        days = max(1, math.ceil(back.total_seconds() / 86_400))
        listed = _answer(google_gmail.message_ids(WATCHED.format(days=days), limit))["ids"]
        old = seen(listed)
        found = []
        for native in listed:
            if native in old:
                continue
            summary = google_gmail.summary(native)
            if summary.get("error"):
                log.info("gmail %s unreadable: %s", native, summary["error"])
                continue
            found.append(_mail(summary))
        return found

    def fetch(self, native_id: str, *, body_chars: int = 20_000) -> Message | None:
        if _is_draft(native_id):
            found = _calling(lambda: google_gmail._gmail().users().drafts().get(
                userId="me", id=native_id, format="full").execute(), gone=True)
            message = (found or {}).get("message")
        else:
            message = _calling(lambda: google_gmail._gmail().users().messages().get(
                userId="me", id=native_id, format="full").execute(), gone=True)
        if not message:
            return None
        summary = google_gmail._summarise(message, include_body=True, body_chars=body_chars)
        payload = message.get("payload", {})
        summary["cc"] = google_gmail._header(payload, "Cc")
        found = _mail(summary, draft_id=native_id if _is_draft(native_id) else "")
        found.ident = google_gmail._header(payload, "Message-ID")
        found.bcc = google_gmail._header(payload, "Bcc")
        return found

    def make_draft(self, draft: Draft) -> Message:
        said = _answer(google_gmail.draft(
            draft.to, draft.subject, draft.body, cc=draft.cc or None, bcc=draft.bcc or None,
            reply_to_message_id=draft.reply_to or None))
        made = Message(native_id=str(said["draft_id"]), thread=str(said.get("thread_id") or ""),
                       to=str(said.get("to") or draft.to), cc=draft.cc, bcc=draft.bcc,
                       subject=str(said.get("subject") or draft.subject), body=draft.body,
                       labels=("DRAFT",), direction="out", draft=True)
        made.extra = {"message_id": str(said.get("message_id") or ""),
                      "url": str(said.get("url") or "")}
        # Its Message-ID as Gmail stored it, read back: the draft is written
        # without one and Gmail gives it its own. Without it a draft had no
        # identity but its id, and the write path could not check, before
        # sending or throwing it away, that the id still names this draft.
        # Only a help: the draft is made, so a failed read does not undo it,
        # or a retry would make a second.
        try:
            stored = self.fetch(made.native_id)
        except ConnectorError:
            stored = None
        made.ident = stored.ident if stored is not None else ""
        return made

    def send_draft(self, message: Message) -> Message:
        """Send the draft as the write path read it, then throw it away.

        Gmail's own send takes a draft's id and sends whatever the draft
        holds by then, so a change in the moment after the check (an
        autosave from the user's browser, a Cc added on the phone) went,
        though no card showed it. A draft holds a message, and Gmail never
        changes a message: saving the draft again makes a new one under a
        new id. So the message read for the check is fetched whole by its
        own id and sent as a mail of its own. Gone, the draft was saved
        again since, and nothing is sent; the user is asked again."""
        checked = message.extra.get("message_id") or ""
        if not checked:
            raise TargetChanged("the draft was not read whole; nothing was sent")
        found = _calling(lambda: google_gmail._gmail().users().messages().get(
            userId="me", id=checked, format="raw").execute(), gone=True)
        if not found:
            raise TargetChanged("the draft changed after it was checked; nothing was sent")
        if "DRAFT" not in (found.get("labelIds") or ()):
            raise TargetChanged("that draft has been sent already; it was not sent again")
        raw = str(found.get("raw") or "")
        outgoing = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
                                            policy=email.policy.default)
        # Gmail gives a mail written without them its own, as it does every
        # new one (`google_gmail._compose`). The draft's Date is when it was
        # last saved; its Message-ID is the draft's, and Gmail keeps one
        # message to a Message-ID (`core/engines/imap.py`'s `send_draft`).
        del outgoing["Message-ID"]
        del outgoing["Date"]
        body = {"raw": base64.urlsafe_b64encode(outgoing.as_bytes()).decode()}
        if found.get("threadId"):
            body["threadId"] = found["threadId"]
        said = _calling(lambda: google_gmail._gmail().users().messages().send(
            userId="me", body=body).execute())
        sent = Message(native_id=str(said.get("id") or ""), thread=str(said.get("threadId") or ""),
                       to=message.to, cc=message.cc, subject=message.subject,
                       labels=("SENT",), direction="out")
        # The mail has gone, so nothing after this raises: an error said it
        # was not sent, and a second yes would send it twice. Its Message-ID
        # is Gmail's, read back, by which the own mailbox knows a mail the
        # agent sent; only a help, as a draft's is (`make_draft`).
        try:
            stored = self.fetch(sent.native_id) if sent.native_id else None
        except Exception:  # noqa: BLE001 - the mail went; this is a help
            stored = None
        sent.ident = stored.ident if stored is not None else ""
        if kept := self._drop_sent(message.native_id, checked):
            sent.extra["draft_kept"] = kept
        return sent

    def _drop_sent(self, draft_id: str, checked: str) -> str:
        """Throw away the draft just sent, if it still holds the message
        sent: one saved again since holds words that were not sent, and
        stays. What was left, and why, is said; never raised."""
        try:
            now = _calling(lambda: google_gmail._gmail().users().drafts().get(
                userId="me", id=draft_id, format="minimal").execute(), gone=True)
            if not now:
                return ""
            if str((now.get("message") or {}).get("id") or "") != checked:
                return ("the draft was saved again after its card; what it says now "
                        "was not sent and is still in Drafts")
            _answer(google_gmail.delete_draft(draft_id))
        except Exception as exc:  # noqa: BLE001 - the mail went; this is tidying
            return (str(exc) if isinstance(exc, ConnectorError)
                    else f"{type(exc).__name__}: {exc}")[:300]
        return ""

    def delete(self, message: Message) -> None:
        if not _is_draft(message.native_id):
            raise Unsupported("Gmail here throws away drafts only")
        _answer(google_gmail.delete_draft(message.native_id))

    def sent_items(self, *, since: dt.datetime, limit: int, to: str = "") -> list[Message]:
        """Asked of the query and checked here, as the IMAP engine checks
        its own: the folder on the labels, the date on Gmail's own time of
        the message, and the recipient on its lines. The query is only a
        filter. Trusted, a Gmail that ignored it, as Google's CalDAV ignored
        a UID filter, gave every sent message as one to `to`, and every one
        since any date. A recipient is one bare address, so
        "jane@northwind.example OR in:inbox" cannot widen it."""
        if to and not _BARE_ADDRESS.fullmatch(to):
            raise ValueError(f"not one address: {to!r}")
        found = self.search(f"in:sent to:{to}" if to else "in:sent", limit=limit, since=since)
        fetched = (self.fetch(f.native_id) for f in found)
        after = since.timestamp() * 1000  # Gmail's time is in milliseconds
        return [m for m in fetched if m is not None and "SENT" in m.labels
                and (not m.when or m.when >= after) and (not to or _went_to(m, to))]

    def envelopes(self, *, sent: bool, since: dt.datetime, limit: int) -> list[Message]:
        """Paged past the search's cap of fifty (`google_gmail.headers`)."""
        back = dt.datetime.now(since.tzinfo) - since
        days = max(1, math.ceil(back.total_seconds() / 86_400))
        query = f"{'in:sent' if sent else 'in:inbox'} newer_than:{days}d"
        return [_mail(m) for m in _answer(google_gmail.headers(query, limit))["messages"]]

    def headers(self, native_id: str) -> dict[str, str]:
        said = _answer(google_gmail.envelope(native_id))
        out: dict[str, str] = {}
        for name, value in said.get("headers", []):
            # The top one of each, which the receiving server wrote.
            out.setdefault(name, value)
        return out

    def thread(self, thread_id: str, *, limit: int) -> list[Message]:
        said = _answer(google_gmail.thread(thread_id, limit))
        return [_mail(m) for m in said.get("messages", [])]

    def modify(self, message: Message, *, read: bool | None = None, archive: bool = False,
               add_labels: tuple[str, ...] = (), remove_labels: tuple[str, ...] = ()
               ) -> Message:
        add, remove = list(add_labels), list(remove_labels)
        if archive:
            remove.append("INBOX")
        if read is not None:
            (remove if read else add).append("UNREAD")
        if not add and not remove:
            raise ConnectorError("nothing to change")
        updated = _calling(lambda: google_gmail._gmail().users().messages().modify(
            userId="me", id=message.native_id,
            body={"addLabelIds": add, "removeLabelIds": remove}).execute())
        labels = tuple(updated.get("labelIds", []))
        message.labels, message.unread = labels, "UNREAD" in labels
        return message

    def test(self) -> str:
        said = _answer(google_gmail.profile())
        return f"Gmail answers for {said.get('email') or 'the account'}"


# --- calendar -------------------------------------------------------------------

def _moment(part: dict) -> dt.datetime | dt.date | None:
    if part.get("date"):
        return dt.date.fromisoformat(part["date"])
    if part.get("dateTime"):
        return dt.datetime.fromisoformat(part["dateTime"])
    return None


def _not_a_person(attendee: dict) -> bool:
    """A room, or a calendar invited as a guest: Google marks a room as a
    resource, and gives both an address of its own calendars'. Marked here,
    where the address is Google's, rather than by every reader of a guest
    list, as the attention loop once did with this same suffix."""
    email = str(attendee.get("email") or "").lower()
    return bool(attendee.get("resource")) or email.endswith("calendar.google.com")


def _event(raw: dict) -> Event:
    """One event as Google's API gives it, as an Event. The user's own entry
    on the guest list is their answer to it, not a guest."""
    start, end = raw.get("start", {}), raw.get("end", {})
    attendees = raw.get("attendees") or []
    mine = next((a for a in attendees if a.get("self")), {})
    organizer = raw.get("organizer") or {}
    extra = {"my_response": mine.get("responseStatus") or "",
             "organizer_self": "yes" if organizer.get("self") else "",
             "updated": raw.get("updated") or "", "created": raw.get("created") or "",
             "visibility": raw.get("visibility") or ""}
    return Event(
        native_id=str(raw.get("id") or ""), ident=str(raw.get("iCalUID") or ""),
        condition=raw.get("etag"), title=str(raw.get("summary") or ""),
        start=_moment(start), end=_moment(end), all_day="date" in start,
        location=str(raw.get("location") or ""), description=str(raw.get("description") or ""),
        guests=tuple(Guest(email=str(a.get("email") or ""), name=str(a.get("displayName") or ""),
                           response=str(a.get("responseStatus") or ""),
                           room=_not_a_person(a))
                     for a in attendees if not a.get("self")),
        organizer=str(organizer.get("email") or ""), calendar="primary",
        link=str(raw.get("htmlLink") or ""), recurring=bool(raw.get("recurringEventId")),
        status=str(raw.get("status") or ""), busy=raw.get("transparency") != "transparent",
        extra={k: v for k, v in extra.items() if v})


def _iso(moment: dt.datetime | dt.date | None) -> str | None:
    return moment.isoformat() if moment is not None else None


class GoogleCalendar(_Google, CalendarProvider):
    capabilities = frozenset({classes.CREATE, classes.UPDATE, classes.DELETE,
                              classes.GUESTS, classes.FREEBUSY, classes.CONDITIONAL})

    def test(self) -> str:
        now = dt.datetime.now(dt.UTC)
        found = self.events(now, now + dt.timedelta(days=7), limit=50)
        return f"Google Calendar answers, with {len(found)} events in the coming week"

    def events(self, start: dt.datetime, end: dt.datetime, *, limit: int) -> list[Event]:
        items = _calling(lambda: google_calendar._events(
            timeMin=start.isoformat(), timeMax=end.isoformat(),
            maxResults=max(1, min(limit, 250)), orderBy="startTime"))
        return [_event(e) for e in items]

    def search(self, words: list[str], start: dt.datetime, end: dt.datetime, *,
               limit: int) -> list[Event]:
        items = _calling(lambda: google_calendar._events(
            timeMin=start.isoformat(), timeMax=end.isoformat(), q=" ".join(words),
            maxResults=max(1, min(limit, 250)), orderBy="startTime"))
        return [_event(e) for e in items]

    def changed(self, since: dt.datetime, start: dt.datetime, end: dt.datetime, *,
                limit: int) -> list[Event]:
        """Google lists what changed itself (`updatedMin`), deletions too."""
        items = _calling(lambda: google_calendar._events(
            updatedMin=since.isoformat(), timeMin=start.isoformat(), timeMax=end.isoformat(),
            maxResults=max(1, min(limit, 250)), showDeleted=True, orderBy="updated"))
        return [_event(e) for e in items]

    def fetch(self, native_id: str) -> Event | None:
        raw = _calling(lambda: google_calendar._calendar().events().get(
            calendarId="primary", eventId=native_id).execute(), gone=True)
        return None if not raw or raw.get("status") == "cancelled" else _event(raw)

    def create(self, event: Event) -> Event:
        said = _answer(google_calendar.create(
            event.title, _iso(event.start) or "", _iso(event.end),
            description=event.description or None, location=event.location or None,
            attendees=[g.email for g in event.guests] or None, all_day=event.all_day,
            private=event.extra.get("visibility") == "private"))
        return self.fetch(str(said["id"])) or event

    def update(self, event: Event) -> Event:
        moved = not event.all_day
        if event.all_day:
            # `google_calendar.update` writes times only as moments, and would
            # make an all-day event a timed one at midnight.
            now = self.fetch(event.native_id)
            if now is not None and (now.start, now.end) != (event.start, event.end):
                raise Unsupported("moving an all-day event")
        _answer(google_calendar.update(
            event.native_id, summary=event.title, start=_iso(event.start) if moved else None,
            end=_iso(event.end) if moved else None, description=event.description,
            location=event.location, etag=event.condition))
        found = self.fetch(event.native_id)
        if found is None:
            raise TargetChanged("the event was gone when read back")
        return found

    def delete(self, event: Event) -> None:
        # Guests hear of the cancellation, as `delete_event` has always told them.
        _answer(google_calendar.delete(event.native_id, notify=bool(event.guests),
                                       etag=event.condition))

    def freebusy(self, start: dt.datetime, end: dt.datetime
                 ) -> list[tuple[dt.datetime, dt.datetime]]:
        said = _calling(lambda: google_calendar._calendar().freebusy().query(body={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(),
            "items": [{"id": "primary"}]}).execute())
        busy = (said.get("calendars", {}).get("primary") or {}).get("busy", [])
        return [(dt.datetime.fromisoformat(b["start"]), dt.datetime.fromisoformat(b["end"]))
                for b in busy]


# --- files ------------------------------------------------------------------------

def _file(found: dict) -> FileItem:
    return FileItem(native_id=str(found.get("id") or ""), name=str(found.get("name") or ""),
                    mime=str(found.get("mime_type") or ""),
                    modified=str(found.get("modified") or ""), link=str(found.get("link") or ""),
                    owners=(str(found["owner"]),) if found.get("owner") else (),
                    extra={"size": str(found["size"])} if found.get("size") is not None else {})


class GoogleDrive(_Google, FilesProvider):
    capabilities = frozenset({classes.DOCUMENTS, classes.SPREADSHEETS})

    def test(self) -> str:
        self.recent(limit=1)
        return "Google Drive answers"

    def search(self, query: str, *, limit: int, mime: str | None = None) -> list[FileItem]:
        said = _answer(google_drive.search(query, limit=limit, mime_type=mime))
        return [_file(f) for f in said.get("files", [])]

    def read(self, native_id: str, *, max_chars: int) -> FileItem:
        said = _answer(google_drive.read(native_id, max_chars=max_chars))
        found = _file(said)
        found.text, found.truncated = said.get("text"), bool(said.get("truncated"))
        return found

    def recent(self, *, limit: int) -> list[FileItem]:
        return [_file(f) for f in _answer(google_drive.recent(limit)).get("files", [])]

    def create_document(self, title: str, text: str) -> FileItem:
        return _file(_answer(google_drive.create_doc(title, text)))

    def create_spreadsheet(self, title: str, rows: list[list[str]]) -> FileItem:
        return _file(_answer(google_drive.create_sheet(title, rows)))

    def read_spreadsheet(self, native_id: str, *, cells: str) -> list[list[str]]:
        return _answer(google_drive.read_sheet(native_id, range_name=cells)).get("values", [])

    def update_spreadsheet(self, native_id: str, *, cells: str, rows: list[list[str]]
                           ) -> FileItem:
        said = _answer(google_drive.update_sheet(native_id, rows, range_name=cells))
        return FileItem(native_id=native_id,
                        extra={"cells": str(said.get("cells") or 0),
                               "range": str(said.get("range") or "")})


# --- tasks ------------------------------------------------------------------------

def _task(raw: dict, list_id: str) -> Task:
    return Task(native_id=str(raw.get("id") or ""), list_id=list_id,
                title=str(raw.get("title") or ""), notes=str(raw.get("notes") or ""),
                due=(raw.get("due") or "")[:10] or None, done=raw.get("status") == "completed",
                condition=raw.get("etag"), link=str(raw.get("webViewLink") or ""))


def _list_id(list_id: str | None) -> str:
    """The list named, else the agent's own, where today's tasks go."""
    found = list_id or _calling(google_tasks._agent_list_id)
    if not found:
        raise Transient("could not reach Google Tasks")
    return found


class GoogleTasks(_Google, TasksProvider):
    def test(self) -> str:
        found = self.lists()
        return f"Google Tasks answers, with {len(found)} list{'s' if len(found) != 1 else ''}"

    def lists(self) -> list[TaskList]:
        return [TaskList(native_id=str(t["id"]), title=str(t.get("title") or ""))
                for t in _answer(google_tasks.lists()).get("lists", [])]

    def tasks(self, list_id: str | None = None, *, open_only: bool = True, limit: int = 50
              ) -> list[Task]:
        where = _list_id(list_id)
        found: list[Task] = []
        page = None
        # Paged: reading the ticks back wants every task on the list, and one
        # page is a hundred at most.
        while len(found) < limit:
            wanted = google_common.clamp(limit - len(found), 25, 100)
            said = _calling(lambda page=page, wanted=wanted: google_tasks._tasks().tasks().list(
                tasklist=where, showCompleted=not open_only, showHidden=not open_only,
                maxResults=wanted, pageToken=page).execute())
            found += [_task(t, where) for t in said.get("items", [])]
            if not (page := said.get("nextPageToken")):
                break
        return found[:limit]

    def fetch(self, native_id: str, list_id: str | None = None) -> Task | None:
        where = _list_id(list_id)
        raw = _calling(lambda: google_tasks._tasks().tasks().get(
            tasklist=where, task=native_id).execute(), gone=True)
        return None if not raw or raw.get("deleted") else _task(raw, where)

    def create(self, task: Task) -> Task:
        # To the agent's own list, whatever `task.list_id` says, as today.
        said = _answer(google_tasks.create(task.title, notes=task.notes, due=task.due))
        return Task(native_id=str(said["id"]), list_id=str(said.get("list_id") or ""),
                    title=str(said.get("title") or task.title), notes=task.notes, due=task.due)

    def update(self, task: Task) -> Task:
        where = _list_id(task.list_id or None)
        body: dict[str, Any] = {"title": task.title[:1024], "notes": task.notes[:8192]}
        if (due := google_tasks._due(task.due)) is not None:
            body["due"] = due
        raw = _calling(lambda: google_tasks._tasks().tasks().patch(
            tasklist=where, task=task.native_id, body=body).execute())
        return _task(raw, where)

    def complete(self, task: Task) -> Task:
        said = _answer(google_tasks.complete(task.native_id, task.list_id or None))
        return Task(native_id=str(said.get("id") or task.native_id), list_id=task.list_id,
                    title=task.title, notes=task.notes, due=task.due,
                    done=said.get("status") == "completed")

