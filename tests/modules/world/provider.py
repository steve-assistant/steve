"""The evals' world as a module: one scenario's mail, calendar, files and
tasks, answered through the slot's own interfaces.

Until step 5's item 4f the yardstick (`agent/autonomy_eval.py`) patched
Google's functions by their dotted names, some thirty of them, and the
persona eval patched four more. Each name was a door a rename could leave
open, and each answered in Google's dicts. One went stale unseen: after 4d
moved the calendar onto the slot, three of the patches pointed at world
methods that no longer existed, and a session that asked for the agenda
would have raised. Now the world is an account like any other: plugged in
where accounts are (`agent/connectors`), and read through the same
interfaces a real account answers, by the same code.

It imports only the standard library and `steve_sdk`, as a module must, and
lives under tests/ so that it never ships. Its writes are refused: the evals
replace every executor with a recording fake, and a write reaching the
world would mean one got past it. The one exception is a draft, which a
fake executor saves here so that the card sending it can read whom it is to,
as the slot reads every draft.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import re
from collections.abc import Callable

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

DOCUMENT = "application/vnd.google-apps.document"


class World:
    """One scenario's items, dated from `now`. Texts come already dated
    (the eval writes "{today}" and the like out before this is built).
    `shown` hears the native id of every item handed over whole -- a
    message read, an event listed, a file opened -- which the yardstick
    scores against what was planted."""

    def __init__(self, world: dict, *, now: dt.datetime, me: str,
                 shown: Callable[[str], None] = lambda native: None):
        self.now, self.me, self._shown = now, me, shown
        self.mail = [dict(m) for m in world.get("mail", [])]
        self.events = [self._event(e) for e in world.get("events", [])]
        self.files = [dict(f) for f in world.get("files", [])]
        self.drafts: dict[str, Message] = {}
        self._ids = itertools.count(1)

    def shown(self, native: str) -> None:
        self._shown(native)

    # -- mail ------------------------------------------------------------------

    def when(self, m: dict) -> dt.datetime:
        return self.now - dt.timedelta(days=float(m.get("days_ago", 1)))

    def message(self, m: dict, *, body_chars: int | None = None) -> Message:
        """A world message as the slot's item: unread in the inbox, with a
        mailing list's headers where the scenario says it came through one."""
        extra = {}
        if m.get("list"):
            domain = m["from"].rsplit("@", 1)[-1].rstrip(">")
            extra = {"list_id": f"<news.{domain}>",
                     "list_unsubscribe": f"<mailto:leave@{domain}>"}
        body = m.get("body", "")
        return Message(
            native_id=m["id"], thread=m.get("thread") or m["id"], sender=m["from"],
            to=m.get("to", self.me), subject=m.get("subject", ""),
            when=int(self.when(m).timestamp() * 1000), snippet=" ".join(body.split())[:200],
            body=None if body_chars is None else body[:body_chars], body_length=len(body),
            unread=True, labels=("INBOX",), direction="in", extra=extra)

    # -- calendar --------------------------------------------------------------

    def _event(self, e: dict) -> Event:
        """`in_days` at `at` for `minutes`; or a meeting that ended
        `ended_minutes_ago`, for the attention loop's follow-ups."""
        if "ended_minutes_ago" in e:
            end = self.now - dt.timedelta(minutes=int(e["ended_minutes_ago"]))
            start = end - dt.timedelta(minutes=int(e.get("minutes", 60)))
        else:
            h, mins = (int(x) for x in str(e.get("at", "09:00")).split(":"))
            start = (self.now + dt.timedelta(days=int(e.get("in_days", 1)))).replace(
                hour=h, minute=mins, second=0, microsecond=0)
            end = start + dt.timedelta(minutes=int(e.get("minutes", 60)))
        return Event(native_id=e["id"], title=e["summary"], start=start, end=end,
                     location=e.get("location") or "", description=e.get("description") or "",
                     guests=tuple(Guest(email=a) for a in e.get("attendees", [])),
                     recurring=bool(e.get("recurring")))


class WorldMail(MailProvider):
    """The world's inbox: searched as Gmail's syntax reads, the words of
    `from:`, `to:` and `subject:` kept and the rest of the operators let go."""

    capabilities = frozenset({classes.THREADS, classes.SEND})

    def __init__(self, world: World):
        self.world = world

    def search(self, query: str, *, limit: int, since: dt.datetime | None = None
               ) -> list[Message]:
        words = []
        for token in (query or "").lower().split():
            key, _, value = token.partition(":")
            if not value:
                words.append(key)
            elif key in ("from", "to", "subject"):
                words.append(value)
        found = [m for m in self.world.mail
                 if all(w.strip('"') in json.dumps(m).lower() for w in words)
                 and (since is None or self.world.when(m) >= since)]
        found.sort(key=self.world.when, reverse=True)
        return [self.world.message(m) for m in found[:limit]]

    def fetch(self, native_id: str, *, body_chars: int = 20_000) -> Message | None:
        if (made := self.world.drafts.get(native_id)) is not None:
            return made
        for m in self.world.mail:
            if m["id"] == native_id:
                self.world.shown(native_id)
                return self.world.message(m, body_chars=body_chars)
        return None

    def thread(self, thread_id: str, *, limit: int) -> list[Message]:
        found = [m for m in self.world.mail if (m.get("thread") or m["id"]) == thread_id]
        for m in found[:limit]:
            self.world.shown(m["id"])
        return [self.world.message(m, body_chars=4_000) for m in found[:limit]]

    def headers(self, native_id: str) -> dict[str, str]:
        found = self.fetch(native_id, body_chars=0)
        return dict(found.extra) if found else {}

    def sent_items(self, *, since: dt.datetime, limit: int, to: str = "") -> list[Message]:
        return []

    def envelopes(self, *, sent: bool, since: dt.datetime, limit: int) -> list[Message]:
        return []

    def make_draft(self, draft: Draft) -> Message:
        native = f"world-draft-{next(self.world._ids)}"
        made = Message(native_id=native, to=draft.to, cc=draft.cc, subject=draft.subject,
                       body=draft.body, body_length=len(draft.body), draft=True)
        self.world.drafts[native] = made
        return made

    def send_draft(self, message: Message) -> Message:
        raise _refused("a send")

    def delete(self, message: Message) -> None:
        raise _refused("a delete")


class WorldCalendar(CalendarProvider):
    capabilities = frozenset({classes.CREATE, classes.UPDATE, classes.DELETE,
                              classes.GUESTS, classes.CONDITIONAL})

    def __init__(self, world: World):
        self.world = world

    def events(self, start: dt.datetime, end: dt.datetime, *, limit: int) -> list[Event]:
        found = sorted((e for e in self.world.events if e.start < end and e.end > start),
                       key=lambda e: e.start)[:limit]
        for e in found:
            self.world.shown(e.native_id)
        return found

    def changed(self, since: dt.datetime, start: dt.datetime, end: dt.datetime, *,
                limit: int) -> list[Event]:
        """Nothing in this world changes while it runs."""
        return []

    def search(self, words: list[str], start: dt.datetime, end: dt.datetime, *,
               limit: int) -> list[Event]:
        """As the default reads a range, but only what matched is shown."""
        found = [e for e in self.world.events if start <= e.start < end and all(
            w in " ".join([e.title, e.location, e.description, *(g.email for g in e.guests)]
                          ).lower() for w in words)][:limit]
        for e in found:
            self.world.shown(e.native_id)
        return found

    def fetch(self, native_id: str) -> Event | None:
        found = next((e for e in self.world.events if e.native_id == native_id), None)
        # Read before a change, as the slot reads one: a condition to hold.
        return None if found is None else _replace(found, condition="world")

    def create(self, event: Event) -> Event:
        raise _refused("an event made")

    def update(self, event: Event) -> Event:
        raise _refused("an event changed")

    def delete(self, event: Event) -> None:
        raise _refused("an event deleted")


class WorldFiles(FilesProvider):
    capabilities = frozenset({classes.DOCUMENTS})

    def __init__(self, world: World):
        self.world = world

    def _item(self, f: dict, *, max_chars: int | None = None) -> FileItem:
        text = f.get("text", "")
        return FileItem(native_id=f["id"], name=f["name"], mime=DOCUMENT,
                        link=f"https://docs.example/{f['id']}",
                        text=None if max_chars is None else text[:max_chars],
                        truncated=max_chars is not None and len(text) > max_chars)

    def search(self, query: str, *, limit: int, mime: str | None = None) -> list[FileItem]:
        words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower())
                 if w not in ("name", "contains", "and", "or", "fulltext")]
        found = [f for f in self.world.files
                 if not words or any(w in (f["name"] + " " + f.get("text", "")).lower()
                                     for w in words)]
        return [self._item(f) for f in found[:limit]]

    def read(self, native_id: str, *, max_chars: int) -> FileItem:
        from steve_sdk.errors import ConnectorError

        for f in self.world.files:
            if f["id"] == native_id:
                self.world.shown(native_id)
                return self._item(f, max_chars=max_chars)
        raise ConnectorError("no file has that id")

    def recent(self, *, limit: int) -> list[FileItem]:
        """Files others changed lately: none are shared in this world."""
        return []


class WorldTasks(TasksProvider):
    """An empty task list: no scenario has tasks yet."""

    def __init__(self, world: World):
        self.world = world

    def lists(self) -> list[TaskList]:
        return [TaskList(native_id="world", title="Tasks")]

    def tasks(self, list_id: str | None = None, *, open_only: bool = True, limit: int = 50
              ) -> list[Task]:
        return []

    def fetch(self, native_id: str, list_id: str | None = None) -> Task | None:
        return None

    def create(self, task: Task) -> Task:
        raise _refused("a task made")

    def update(self, task: Task) -> Task:
        raise _refused("a task changed")

    def complete(self, task: Task) -> Task:
        raise _refused("a task ticked")


PROVIDERS: dict[str, type] = {"mail": WorldMail, "calendar": WorldCalendar,
                              "files": WorldFiles, "tasks": WorldTasks}

# The kinds of account a day is played as (05-connectors.md, item 16), by
# module: what each one's providers can do, by class; a class not named is
# one the kind lacks. The evals play a kind with what both it and the world
# can do (`agent.autonomy_eval.World`), so an Outlook.com day asks a card for
# a change Google's calendar makes on condition. Here and not in the evals
# because a module's name belongs to modules/ and the tests
# (`tests/gate/test_no_provider_names.py`); `tests/unit/test_persona_eval.py`
# holds each set to what the provider itself declares, built on its fake.
_C = classes
KINDS: dict[str, dict[str, frozenset[str]]] = {
    "google_cloud": {
        "mail": frozenset({_C.LABELS, _C.THREADS, _C.FLAGS, _C.ARCHIVE, _C.SEND}),
        "calendar": frozenset({_C.CREATE, _C.UPDATE, _C.DELETE, _C.GUESTS, _C.FREEBUSY,
                               _C.CONDITIONAL}),
        "files": frozenset({_C.DOCUMENTS, _C.SPREADSHEETS}),
        "tasks": frozenset()},
    "gmail_password": {
        "mail": frozenset({_C.LABELS, _C.THREADS, _C.FLAGS, _C.MOVE, _C.ARCHIVE, _C.SEND}),
        "calendar": frozenset({_C.CREATE, _C.UPDATE, _C.DELETE, _C.CONDITIONAL})},
    "outlook_mcp": {
        "mail": frozenset({_C.THREADS, _C.SEND}),
        "calendar": frozenset({_C.CREATE, _C.UPDATE, _C.DELETE})},
    "outlook_classic": {
        "mail": frozenset({_C.SEND}),
        "calendar": frozenset()},
}
# The kind whose day the others' cards are measured against: Gmail's.
BASELINE = "google_cloud"


def _replace(event: Event, **changes) -> Event:
    import dataclasses

    return dataclasses.replace(event, **changes)


def _refused(what: str) -> RuntimeError:
    return RuntimeError(f"{what} reached the world: the evals fake every executor, "
                        "so one got past them")
