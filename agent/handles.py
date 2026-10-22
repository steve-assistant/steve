"""Short stand-ins for long ids, so a model never has to copy one.

An Outlook message's id is `outlook:` and 148 characters of hex, and to read the
message a model has to hand the whole thing back exactly. Models do not copy
140 characters of hex exactly. Asked to choose among twenty, they drop or
change a few in the middle, MAPI answers "operation failed", and the turn
goes nowhere. modules/outlook_classic/outlook.py has said so since September, and its answer
was a clearer error and fewer reads. That was not enough: on 4 October 2026
the agent's mailbox read a forwarded Outlook mail, the model passed back an id
with characters missing from the middle, every read failed, and the canary
counted those failures against the three upgrades merged that afternoon and
reverted all three -- for an error none of them had caused.

So a long id never reaches a model. Every tool's result passes through
`shorten`, which swaps an id longer than `LONGER_THAN` for a handle like
`mail#9f3a2c1b7e` and keeps the pair in the `handles` table; every tool's
id arguments pass through `resolve`, which swaps a handle back. Both are
done once, in `agent.toolset.register_tools`, so no tool can forget, and a
tool written next month gets them without knowing they exist. An id the
model got some other way -- pasted by the user, read off a link -- is passed
through untouched: only a value shaped like a handle is looked up.

Two wrong turns this avoids:
- A handle that names the wrong thing is worse than a long id that names
  nothing, because a deleted draft or a sent reply cannot be taken back. So
  a handle is a hash of its id, and when two ids would share one, the
  second gets a longer handle rather than the first one's (`_handle`).
- A handle the table no longer knows is refused with "search again", never
  guessed at.

Where it applies: by name. An argument or result key called `id`, or ending
in `_id` or `_ids`, is an id; nothing else is. Anything else that names an
item -- a link, a title -- is for reading, and is left alone.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import re
import sqlite3
import time
from typing import Any

from core import db, modules

# Longer than this, an id is replaced. Gmail's ids are 16 characters and
# Drive's 33, and both pass through as they are; a Google Sheet's is 44 and
# an Outlook message's 156, and those are the ones models have been seen to
# mangle.
LONGER_THAN = 40

# What a handle looks like: a kind, `#`, and at least ten hex digits of the
# id's hash -- more when ten would have been shared with another id.
HANDLE = re.compile(r"([a-z]+)#([0-9a-f]{10,64})")
_SCHEME = re.compile(r"([a-z]+):")
_LENGTHS = (10, 16, 24, 64)

# A handle unused for this long is forgotten. A conversation is replayed for
# a day (`agent.loop.HISTORY_WINDOW_HOURS`), and a goal's notes can quote a
# handle for weeks; past this, "search again" costs less than keeping it.
KEEP_DAYS = 90
_PRUNE_EVERY_S = 3600.0
_pruned_at: float | None = None


class UnknownHandle(LookupError):
    """A value shaped like a handle that the table does not know."""


def is_id(name: str | None) -> bool:
    """Whether an argument or key of this name holds one id."""
    return bool(name) and (name == "id" or name.endswith("_id"))


def is_ids(name: str | None) -> bool:
    """Whether an argument or key of this name holds a list of ids."""
    return bool(name) and name.endswith("_ids")


def _kind(full: str, key: str | None) -> str:
    """What the handle says the id is: its scheme (`mail`, `calendar`)
    if it has one, else the last word of its key (`thread_id` -> `thread`).
    Only for whoever reads it; the table resolves a handle whatever it says."""
    if scheme := _SCHEME.match(full):
        return scheme.group(1)
    word = (key or "").removesuffix("_ids").removesuffix("_id").rsplit("_", 1)[-1]
    word = re.sub(r"[^a-z]", "", word.lower())
    return word or "id"


def _handle(conn: sqlite3.Connection, full: str, key: str | None) -> str:
    """The handle for `full`, kept in the table.

    The same id always gets the same handle, so a conversation replayed after
    a restart still means what it meant. Ten hex digits of a hash collide
    rarely, but rarely is not never: when the handle is already another id's,
    this one takes a longer one instead of taking the first one's over.
    """
    kind = _kind(full, key)
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
    for length in _LENGTHS:
        handle = f"{kind}#{digest[:length]}"
        row = conn.execute("SELECT full FROM handles WHERE handle = ?", (handle,)).fetchone()
        if row is None:
            with db.transaction(conn):
                conn.execute("INSERT INTO handles (handle, full) VALUES (?, ?)", (handle, full))
            return handle
        if row[0] == full:
            with db.transaction(conn):
                conn.execute("UPDATE handles SET used_at = datetime('now') WHERE handle = ?",
                             (handle,))
            return handle
    # Sixty-four hex digits of SHA-256 shared by two different ids: not in
    # this universe. Handing back the id itself is still right if it happens.
    return full


def _prune(conn: sqlite3.Connection) -> None:
    global _pruned_at
    now = time.monotonic()
    if _pruned_at is not None and now - _pruned_at < _PRUNE_EVERY_S:
        return
    _pruned_at = now
    with db.transaction(conn):
        conn.execute("DELETE FROM handles WHERE used_at < datetime('now', ?)",
                     (f"-{KEEP_DAYS} days",))


def shorten(conn: sqlite3.Connection, value: Any) -> Any:
    """`value` with every long id in it replaced by a handle.

    Dicts and lists are walked to any depth; a new structure is returned and
    `value` itself is left as it was, since a tool's caller may still hold it.
    With nothing to replace, `value` comes back as the very same object.
    """
    made: list[bool] = []

    def walk(item: Any, key: str | None) -> Any:
        if isinstance(item, dict):
            return {name: walk(inner, name) for name, inner in item.items()}
        if isinstance(item, list | tuple):
            # A list under `event_ids` is a list of ids; a list under
            # `messages` is a list of things, each with keys of its own.
            each = key.removesuffix("s") if is_ids(key) else None
            walked = [walk(inner, each) for inner in item]
            return tuple(walked) if isinstance(item, tuple) else walked
        if isinstance(item, str) and is_id(key) and len(item) > LONGER_THAN:
            made.append(True)
            return _handle(conn, item, key)
        return item

    walked = walk(value, None)
    if not made:
        return value
    _prune(conn)
    return walked


def resolve(conn: sqlite3.Connection, value: Any) -> Any:
    """`value` with a handle in it replaced by its id: a single value, or each
    item of a list. Anything not shaped like a handle is returned as it is.

    Raises `UnknownHandle` for a handle the table does not know: one from
    before `KEEP_DAYS`, or a handle the model made up or misremembered.
    """
    if isinstance(value, list | tuple):
        return type(value)(resolve(conn, item) for item in value)
    if not isinstance(value, str):
        return value
    text = value.strip().strip("`'\"").lower()
    if not HANDLE.fullmatch(text):
        return value
    # One of an account that is off is unknown, as it would be had the
    # account never been added.
    row = conn.execute("SELECT full FROM handles WHERE handle = ?"
                       f" AND {modules.live(conn, 'handles')}", (text,)).fetchone()
    if row is None:
        raise UnknownHandle(value)
    return row[0]


def lost(value: str) -> dict:
    """What a tool given an unknown handle says, instead of guessing."""
    return {
        "ok": False, "kind": "usage",
        "error": (f"{value!r} is not an id this agent knows any more. Search again for the "
                  "item and use the id that search returns, exactly as given."),
    }


def wrap(fn):
    """`fn`, a tool, taking handles and handing back handles.

    `functools.wraps` keeps its name, docstring and signature, so the model
    is shown the same tool as before. Every tool here is `async def`; a
    plain function is run in a thread, as pydantic-ai would have run it, so
    the table is only ever touched from the loop's thread and connection
    (docs/protocols.md section 3).
    """
    signature = inspect.signature(fn)
    names = [name for name in signature.parameters if is_id(name) or is_ids(name)]

    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        ctx = next(iter(bound.arguments.values()), None)
        conn = getattr(getattr(ctx, "deps", None), "conn", None)
        if conn is None:
            return await _call(fn, bound)
        try:
            for name in names:
                if name in bound.arguments:
                    bound.arguments[name] = resolve(conn, bound.arguments[name])
        except UnknownHandle as gone:
            return lost(str(gone))
        return shorten(conn, await _call(fn, bound))

    return wrapped


async def _call(fn, bound: inspect.BoundArguments) -> Any:
    if inspect.iscoroutinefunction(fn):
        return await fn(*bound.args, **bound.kwargs)
    import asyncio

    return await asyncio.to_thread(fn, *bound.args, **bound.kwargs)


def shorten_text(conn: sqlite3.Connection | None, name: str, value: Any) -> Any:
    """One named value, shortened if it is a long id: for prompts that list an
    item's ids outside any tool (agent/attention/prepare.describe)."""
    if conn is None:
        return value
    return shorten(conn, {name: value})[name]
