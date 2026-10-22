"""A sandboxed module's reads of the user's mail, calendar, files and tasks, screened.

A module granted the user's mail asks the broker (`connector.read`), and
the broker is core, which cannot reach the accounts; they are the agent's.
This is the reader it is handed. A module reading the accounts itself
could call a method that writes, hand a calendar id to a mailbox, learn a
provider's own ids and send them on, or read a mail its sender wrote to
steer it. So the reader:

- makes only the reads `READS` names (`steve_sdk/calls.py`), each by one
  call below, with its `DEFAULTS` for what the module left out;
- routes an id back to the account that handed it out, and refuses an id
  of another class or one Steve did not make;
- gives the module Steve's ids and the account's name, never the
  provider's own ids, a write's condition, or its extra headers;
- screens each item as outside text (`agent/screening.py`): an item the
  screen did not pass keeps only what its sender did not write, with
  `screened` saying how and `text` holding what the screen allows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from agent import connectors, screening
from core.modctx import Refused
from core.sandbox.broker import Reader
from steve_sdk import calls, ids
from steve_sdk.errors import ConnectorError, NotConnected

log = logging.getLogger(__name__)

# Each read: the provider, the arguments as the broker typed them with the
# read's defaults filled in, and the provider's own ids that its "id"
# arguments stood for.
_CALLS: dict[tuple[str, str], Callable[[Any, dict, dict], Any]] = {
    ("mail", "search"): lambda p, a, n: p.search(a["query"], limit=a["limit"],
                                                 since=a.get("since")),
    ("mail", "fetch"): lambda p, a, n: p.fetch(n["id"], body_chars=a["body_chars"]),
    ("mail", "thread"): lambda p, a, n: p.thread(n["id"], limit=a["limit"]),
    ("calendar", "events"): lambda p, a, n: p.events(a["start"], a["end"], limit=a["limit"]),
    ("calendar", "search"): lambda p, a, n: p.search(a["words"], a["start"], a["end"],
                                                     limit=a["limit"]),
    ("calendar", "fetch"): lambda p, a, n: p.fetch(n["id"]),
    ("files", "search"): lambda p, a, n: p.search(a["query"], limit=a["limit"],
                                                  mime=a.get("mime")),
    ("files", "recent"): lambda p, a, n: p.recent(limit=a["limit"]),
    ("files", "read"): lambda p, a, n: p.read(n["id"], max_chars=a["max_chars"]),
    ("tasks", "lists"): lambda p, a, n: p.lists(),
    ("tasks", "tasks"): lambda p, a, n: p.tasks(n.get("list"), open_only=a["open_only"],
                                                limit=a["limit"]),
    ("tasks", "fetch"): lambda p, a, n: p.fetch(n["id"]),
}

# What a sender, organiser, owner or other author wrote: what an item the
# screen did not pass loses.
_WRITTEN = {
    "mail": ("sender", "to", "cc", "bcc", "subject", "snippet", "body", "attachments"),
    "calendar": ("title", "location", "description", "organizer", "guests", "link"),
    "files": ("name", "owners", "link", "text"),
    "tasks": ("title", "notes", "link"),
}
_SOURCE = {"mail": "email_body", "calendar": "calendar_event", "files": "drive_doc",
           "tasks": "task"}
def _said(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _origin(klass: str, item: dict) -> str:
    if klass == "mail":
        return item.get("sender") or ""
    if klass == "calendar":
        return item.get("organizer") or ""
    if klass == "files":
        return next(iter(item.get("owners") or []), "")
    return ""


def item(conn: sqlite3.Connection | None, klass: str, found: connectors.Connected,
         got: Any) -> dict:
    """One item as a module is given it: Steve's ids, and screened."""
    out = calls.given(got, account=found.name, id_of=found.id)
    written = [k for k in _WRITTEN.get(klass, ()) if out.get(k)]
    if not written:
        return out
    text = "\n".join(f"{k}: {_said(out[k])}" for k in written)
    seen = screening.shown_here(conn, _SOURCE[klass], out["id"], _origin(klass, out), text)
    if seen.how == "text":
        return out
    kept = {k: v for k, v in out.items() if k not in _WRITTEN[klass]}
    return {**kept, "screened": seen.how, "text": seen.text}


def reader(conn: sqlite3.Connection, **kw: Any) -> Reader:
    """The broker's reader, on a connection the calling thread owns. `kw`
    goes to `connectors.accounts`."""

    def read(klass: str, method: str, args: dict, account: str | None) -> list[dict]:
        call = _CALLS[(klass, method)]
        natives: dict[str, str] = {}
        by: connectors.Connected | None = None
        try:
            for name, kind in calls.READS[klass][method].items():
                if kind.rstrip("!") != "id" or name not in args:
                    continue
                parsed = ids.parse(args[name])
                if parsed is None or parsed.klass != klass:
                    raise Refused(f"{name}: not the id of a {klass} item Steve handed out")
                there, natives[name] = connectors.by_id(conn, args[name], **kw)
                if by is not None and there.key != by.key:
                    raise Refused("ids from two accounts in one read")
                by = there
            targets = [by] if by is not None else connectors.named(conn, klass, account, **kw)
        except connectors.NoAccount as exc:
            raise NotConnected(str(exc)) from None
        typed = {**calls.DEFAULTS.get(f"{klass}.{method}", {}),
                 **{k: v for k, v in args.items() if k not in natives}}
        out: list[dict] = []
        failed: list[ConnectorError] = []
        for found in targets:
            try:
                got = connectors.blocking(found, lambda p: call(p, typed, natives))
            except ConnectorError as exc:
                log.info("a module's %s.%s on %s: %s", klass, method, found.name, exc.kind)
                failed.append(exc)
                continue
            items = got if isinstance(got, list) else [] if got is None else [got]
            out += [item(conn, klass, found, one) for one in items]
        if failed and len(failed) == len(targets):
            raise failed[0]
        return out

    return read
