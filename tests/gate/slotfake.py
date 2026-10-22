"""Calendar, files and tasks accounts for tests, each held in a dict, and the
slot showing only them.

The mail tools moved onto the connector slot first, with their fake beside
this one (`tests/gate/mailfake.py`). The calendar, Drive and Tasks tools
followed (step 5, item 4d), and the tests that faked a named service's
client -- `calendar_client`, `google_tasks.create` -- no longer reached the
code that matters: the event read back to see who a change emails, the
etag a change is conditional on, the task found again by its id before it
is ticked. These accounts are that code's far side: providers with a dict
behind them, driven by `core/writes.py` like any other, which a test then
reads to see what was written.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import itertools

from steve_sdk import classes
from steve_sdk.classes import (
    CalendarProvider,
    Event,
    FileItem,
    FilesProvider,
    Task,
    TaskList,
    TasksProvider,
)
from steve_sdk.errors import TargetChanged
from tests.gate import mailfake


def plug(monkeypatch, klass: str, **providers) -> dict:
    """These accounts, by name, as the only `klass` accounts there are, to
    every caller of `connectors.accounts`. Each name's account is returned,
    so a test can make the ids it would have handed out. Plugging another
    class keeps this one's."""
    from agent import connectors

    made = {name: connectors.Connected(
        key=hashlib.sha256(f"{klass}:{name}".encode()).hexdigest()[:12], name=name,
        klass=klass, module="tests", provider=provider,
        address=getattr(provider, "address", ""))
        for name, provider in providers.items()}
    real = connectors.accounts

    def accounts(conn, wanted, **kw):
        return list(made.values()) if wanted == klass else real(conn, wanted, **kw)

    monkeypatch.setattr(connectors, "accounts", accounts)
    mailfake.live(monkeypatch, made)
    return made


# --- calendar ----------------------------------------------------------------------

def at(text: str) -> dt.datetime:
    """An ISO time; with no zone, the user's (`agent/zones.local_zone`)."""
    from agent import zones

    moment = dt.datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=zones.local_zone())


class Calendar(CalendarProvider):
    """One account's events, by native id. Writes are conditional on each
    event's etag, which every change bumps, as a server's would; `writes`
    says what a test did to it, and `edit` changes an event as its owner
    would elsewhere."""

    shareable = True

    def __init__(self, *events: Event, address: str = "",
                 capabilities: frozenset[str] | None = None) -> None:
        self.address = address
        self.capabilities = (frozenset({classes.CREATE, classes.UPDATE, classes.DELETE,
                                        classes.GUESTS, classes.CONDITIONAL})
                             if capabilities is None else capabilities)
        self.items: dict[str, Event] = {}
        self.writes: list[tuple[str, Event]] = []
        self._ids = itertools.count(1)
        for event in events:
            self.items[event.native_id] = dataclasses.replace(
                event, condition=event.condition or self._etag())

    def _etag(self) -> str | None:
        return f'"{next(self._ids)}"' if classes.CONDITIONAL in self.capabilities else None

    def edit(self, native_id: str, **change) -> None:
        self.items[native_id] = dataclasses.replace(
            self.items[native_id], condition=self._etag(), **change)

    def events(self, start: dt.datetime, end: dt.datetime, *, limit: int) -> list[Event]:
        found = sorted(self.items.values(), key=lambda e: str(e.start))
        return [copy.deepcopy(e) for e in found][:limit]

    def fetch(self, native_id: str) -> Event | None:
        return copy.deepcopy(self.items.get(native_id))

    def _held(self, event: Event) -> Event:
        now = self.items.get(event.native_id)
        if now is None:
            raise TargetChanged("no such event")
        if event.condition is not None and event.condition != now.condition:
            raise TargetChanged("412: the event changed since it was read")
        return now

    def create(self, event: Event) -> Event:
        made = dataclasses.replace(event, native_id=f"ev-{next(self._ids)}",
                                   condition=self._etag())
        self.items[made.native_id] = made
        self.writes.append(("create", copy.deepcopy(made)))
        return copy.deepcopy(made)

    def update(self, event: Event) -> Event:
        self._held(event)
        written = dataclasses.replace(event, condition=self._etag())
        self.items[event.native_id] = written
        self.writes.append(("update", copy.deepcopy(written)))
        return copy.deepcopy(written)

    def delete(self, event: Event) -> None:
        self._held(event)
        self.writes.append(("delete", self.items.pop(event.native_id)))


# --- files ---------------------------------------------------------------------------

class Drive(FilesProvider):
    """One account's files, by native id, with each spreadsheet's rows."""

    shareable = True

    def __init__(self, *files: FileItem) -> None:
        self.capabilities = frozenset({classes.DOCUMENTS, classes.SPREADSHEETS})
        self.items: dict[str, FileItem] = {f.native_id: f for f in files}
        self.sheets: dict[str, list[list[str]]] = {}
        self._ids = itertools.count(1)

    def search(self, query: str, *, limit: int, mime: str | None = None) -> list[FileItem]:
        words = query.lower().split()
        found = [f for f in self.items.values()
                 if all(w in f"{f.name} {f.text or ''}".lower() for w in words)]
        return [dataclasses.replace(f, text=None) for f in found[:limit]]

    def read(self, native_id: str, *, max_chars: int) -> FileItem:
        found = copy.deepcopy(self.items[native_id])
        text = found.text or ""
        found.text, found.truncated = text[:max_chars], len(text) > max_chars
        return found

    def _made(self, name: str, mime: str, text: str = "") -> FileItem:
        native = f"file-{next(self._ids)}"
        made = FileItem(native_id=native, name=name, mime=mime, text=text,
                        link=f"https://files.test/{native}")
        self.items[made.native_id] = made
        return dataclasses.replace(made, text=None)

    def create_document(self, title: str, text: str) -> FileItem:
        return self._made(title, "application/vnd.google-apps.document", text)

    def create_spreadsheet(self, title: str, rows: list[list[str]]) -> FileItem:
        made = self._made(title, "application/vnd.google-apps.spreadsheet")
        self.sheets[made.native_id] = [list(r) for r in rows]
        return made

    def read_spreadsheet(self, native_id: str, *, cells: str) -> list[list[str]]:
        return [list(r) for r in self.sheets.get(native_id, [])]

    def update_spreadsheet(self, native_id: str, *, cells: str, rows: list[list[str]]
                           ) -> FileItem:
        self.sheets[native_id] = [list(r) for r in rows]
        return FileItem(native_id=native_id, extra={
            "cells": str(sum(len(r) for r in rows)), "range": f"Sheet1!{cells}"})


# --- tasks ---------------------------------------------------------------------------

class TaskBoard(TasksProvider):
    """One account's task list, by native id. `created` says what was
    added; `tick` and `remove` are the user's thumb on their phone."""

    shareable = True

    def __init__(self, *tasks: Task) -> None:
        self.items: dict[str, Task] = {t.native_id: t for t in tasks}
        self.created: list[Task] = []
        self.completed: list[str] = []
        self._ids = itertools.count(1)

    def tick(self, native_id: str) -> None:
        self.items[native_id].done = True

    def remove(self, native_id: str) -> None:
        self.items.pop(native_id, None)

    def lists(self) -> list[TaskList]:
        return [TaskList(native_id="L", title="Steve")]

    def tasks(self, list_id: str | None = None, *, open_only: bool = True, limit: int = 50
              ) -> list[Task]:
        found = [copy.deepcopy(t) for t in self.items.values() if not (open_only and t.done)]
        return found[:limit]

    def fetch(self, native_id: str, list_id: str | None = None) -> Task | None:
        return copy.deepcopy(self.items.get(native_id))

    def create(self, task: Task) -> Task:
        made = dataclasses.replace(task, native_id=f"task-{next(self._ids)}", list_id="L")
        self.items[made.native_id] = made
        self.created.append(copy.deepcopy(made))
        return copy.deepcopy(made)

    def update(self, task: Task) -> Task:
        self.items[task.native_id] = copy.deepcopy(task)
        return copy.deepcopy(task)

    def complete(self, task: Task) -> Task:
        self.items[task.native_id].done = True
        self.completed.append(task.native_id)
        return copy.deepcopy(self.items[task.native_id])
