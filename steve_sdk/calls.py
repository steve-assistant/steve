"""What a module may ask of Steve -- reads of the user's items, and
proposals -- with what each argument may be and the shape an item is given
in: one table for core's broker, core's reader and a module's own tests
(`steve_sdk/testing/context.py`).

In the self-upgrade trial (19 October 2026) all three builds of a tool for
"who are my unread emails from" asked the mail search for 500 items, and
gave their tests a ctx of their own that took any number and answered in
shapes the reader never gives. The tests passed, the code reviewer
approved, and every real call failed: the broker takes 1 to 100. The table
lived in core, where a module's tests never met it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
from collections.abc import Callable
from typing import Any

# The reads a module may make, by class and method, with the kinds of their
# arguments. "id" is an id Steve handed out for an item of that class; "time"
# is ISO 8601 with its offset; "words" a short list of text. A kind ending in
# "!" must be given. `agent/modreads.py` makes the calls.
READS: dict[str, dict[str, dict[str, str]]] = {
    "mail": {
        "search": {"query": "text", "limit": "limit", "since": "time"},
        "fetch": {"id": "id!", "body_chars": "chars"},
        "thread": {"id": "id!", "limit": "limit"},
    },
    "calendar": {
        "events": {"start": "time!", "end": "time!", "limit": "limit"},
        "search": {"words": "words", "start": "time!", "end": "time!", "limit": "limit"},
        "fetch": {"id": "id!"},
    },
    "files": {
        "search": {"query": "text", "limit": "limit", "mime": "text"},
        "recent": {"limit": "limit"},
        "read": {"id": "id!", "max_chars": "chars"},
    },
    "tasks": {
        "lists": {},
        "tasks": {"list": "id", "open_only": "bool", "limit": "limit"},
        "fetch": {"id": "id!"},
    },
}
# What a read uses for an argument its module leaves out.
DEFAULTS: dict[str, dict[str, Any]] = {
    "mail.search": {"query": "", "limit": 20},
    "mail.fetch": {"body_chars": 20_000},
    "mail.thread": {"limit": 20},
    "calendar.events": {"limit": 50},
    "calendar.search": {"words": [], "limit": 50},
    "files.search": {"query": "", "limit": 20},
    "files.recent": {"limit": 20},
    "files.read": {"max_chars": 20_000},
    "tasks.tasks": {"open_only": True, "limit": 50},
}
MAX_LIMIT = 100             # items one read returns
MAX_CHARS = 100_000         # characters of one item's text
MAX_TEXT = 2000             # characters in one argument of a read
MAX_WORDS = 20              # words in a "words" argument, each of 1 to 200 characters

ACTION = re.compile(r"[a-z][a-z0-9_]{0,39}")
MAX_ACTS = 5                # actions one call may propose, each a card
MAX_ACT = 16 << 10          # bytes of one action's arguments, as JSON
# What `ctx.act` answers: a card, not a change.
PROPOSED = {"status": "proposed",
            "note": "the user is asked once this call ends; nothing is done yet"}

# An item as a module is given it: the provider's own ids, a write's
# condition and extra headers are never given; these are given as Steve's ids.
DROPPED = ("native_id", "ident", "condition", "extra")
NATIVE = ("thread", "list_id")


def arguments(read: str, kinds: dict[str, str], args: dict[str, Any]) -> dict[str, Any]:
    """`args` as Steve passes them on, or ValueError saying what is wrong.
    `read` names it ("mail.search"); `kinds` is its entry in `READS`."""
    if extra := sorted(set(args) - set(kinds)):
        raise ValueError(f"no parameter {extra[0]!r} here")
    typed = {}
    for name, kind in kinds.items():
        if args.get(name) is None:
            if kind.endswith("!"):
                raise ValueError(f"{read} needs {name}")
            continue
        typed[name] = argument(name, kind.rstrip("!"), args[name])
    return typed


def argument(name: str, kind: str, value: Any) -> Any:
    """One argument of a read, checked as its kind says."""
    if kind in ("text", "id"):
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            raise ValueError(f"{name}: text of at most {MAX_TEXT:,} characters")
        return value
    if kind in ("limit", "chars"):
        top = MAX_LIMIT if kind == "limit" else MAX_CHARS
        if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= top:
            raise ValueError(f"{name}: a whole number from 1 to {top:,}")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{name}: true or false")
        return value
    if kind == "words":
        if (not isinstance(value, list) or len(value) > MAX_WORDS
                or not all(isinstance(w, str) and 0 < len(w) <= 200 for w in value)):
            raise ValueError(f"{name}: a list of at most {MAX_WORDS} words")
        return [w.lower() for w in value]
    if kind == "time":
        try:
            moment = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            moment = None
        if moment is None or moment.tzinfo is None:
            raise ValueError(f"{name}: an ISO 8601 time with its offset")
        return moment
    raise AssertionError(kind)


def proposal(action: str, args: Any) -> None:
    """ValueError if Steve would refuse this proposal's form (`ctx.act`)."""
    if not isinstance(action, str) or not ACTION.fullmatch(action):
        raise ValueError("action: [a-z][a-z0-9_]{0,39}")
    if not isinstance(args, dict):
        raise ValueError("args: an object")
    if len(json.dumps(args, ensure_ascii=False)) > MAX_ACT:
        raise ValueError(f"args: over {MAX_ACT >> 10} KB")


def given(item: Any, *, account: str, id_of: Callable[[str], str]) -> dict[str, Any]:
    """An item (`steve_sdk.classes`) as a module is given it, before the
    injection screen: a plain dict, with Steve's ids for the provider's
    (`id_of`), times as ISO 8601 text, and the name of the account it came
    from. An item the screen did not pass then loses what its sender wrote
    and gains `screened` and `text` (agent/modreads.py)."""
    raw = dataclasses.asdict(item)
    out: dict[str, Any] = {"id": id_of(raw["native_id"]), "account": account}
    for key, value in raw.items():
        if key in DROPPED:
            continue
        if key in NATIVE:
            value = id_of(value) if value else ""
        out[key] = plain(value)
    return out


def plain(value: Any) -> Any:
    """`value` as JSON carries it: times as ISO 8601 text, tuples as lists."""
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value
