"""A `ctx` for a module's own tests that refuses what Steve refuses and
answers as Steve answers.

A module's tests used to pass a small object of their own as `ctx`. In the
self-upgrade trial (19 October 2026) those objects took any read and
answered in shapes of the test's own making, so three builds of one tool
passed their tests and failed every real call (`steve_sdk/calls.py` says
how), and another made its upload optional to fit a ctx with no `act`.
This one is built on the table and item shape core uses:

    from steve_sdk.classes import Message
    from steve_sdk.testing.context import FakeContext

    ctx = FakeContext(mail=[Message("m1", sender="iris@example.com", unread=True)])
    assert senders(ctx) == {"iris@example.com": 1}
    assert ctx.asked == [("mail", "search", {"query": "is:unread", "limit": 100})]
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
from typing import Any

from steve_sdk import calls, ids
from steve_sdk.context import Account, Response
from steve_sdk.errors import NotConnected

KEY = "fake"  # the account key in every id this ctx hands out


class _Refuses:
    """What a test did not give the module: any use is a mistake in the test."""

    def __init__(self, what: str) -> None:
        self._what = what

    def request(self, method: str, url: str, **kw: Any) -> Response:
        raise PermissionError(f"this test gave the module no {self._what}")

    def open(self, progid: str) -> Any:
        raise PermissionError(f"this test gave the module no {self._what}")


class FakeContext:
    """`ctx` as a module in its sandbox has it, with the user's items given
    as `steve_sdk.classes` objects.

    - `read` refuses a class not in `reads` and a read Steve does not make,
      checks every argument as Steve does, and answers as Steve does: a
      list of plain dicts, each with Steve's `id` for it and the account's
      name, at most the read's limit (`calls.DEFAULTS` when none is given).
      It does not search or look at times: a search, a range or a list
      gives the items of its class as the test gave them, a mail search
      without bodies; a thread, a task list's tasks (open ones unless
      `open_only=False`) or a `fetch` keep to what the id names.
    - `act` checks a proposal as Steve does, five at most, and answers as
      Steve does; the proposals are in `proposed`.
    - `secret` raises, as in the sandbox: a module's secret is bound to a
      header (`{{secret:NAME}}`), never handed to its code.
    - `asked` keeps each read with its arguments as the module gave them;
      `logged` each line of `log`.
    - `id(klass, native_id)` is Steve's id for an item the test gave, to
      pass to a `fetch` or compare with what came back.
    """

    def __init__(self, *, mail: list | tuple = (), calendar: list | tuple = (),
                 files: list | tuple = (), tasks: list | tuple = (),
                 reads: tuple[str, ...] = ids.CLASSES, account_name: str = "Personal",
                 http: Any = None, com: Any = None, zone: dt.tzinfo = dt.UTC,
                 state_dir: Path | None = None, account: Account | None = None) -> None:
        self.items = {"mail": list(mail), "calendar": list(calendar), "files": list(files),
                      "tasks": list(tasks)}
        self.reads = set(reads)
        self.account_name = account_name
        self.http = http if http is not None else _Refuses("http")
        self.com = com if com is not None else _Refuses("com")
        self._zone = zone
        self.state_dir = state_dir
        self.account = account or Account(key="", name="", module="")
        self.asked: list[tuple[str, str, dict[str, Any]]] = []
        self.proposed: list[tuple[str, dict]] = []
        self.logged: list[str] = []

    @property
    def zone(self) -> dt.tzinfo:
        return self._zone

    def secret(self, name: str) -> str | None:
        raise PermissionError("in the sandbox a module never holds a secret: put "
                              f"{{{{secret:{name}}}}} in the header it is bound to")

    def log(self, text: str) -> None:
        self.logged.append(str(text))

    def read(self, klass: str, method: str, *, account: str | None = None,
             **args: Any) -> list[dict[str, Any]]:
        if klass not in self.reads:
            raise PermissionError(f"this module may not read {klass}")
        kinds = calls.READS.get(klass, {}).get(method)
        if kinds is None:
            raise PermissionError(f"{klass}.{method} is not a read a module may make")
        sent = {k: v.isoformat() if isinstance(v, dt.datetime) else v for k, v in args.items()}
        typed = {**calls.DEFAULTS.get(f"{klass}.{method}", {}),
                 **calls.arguments(f"{klass}.{method}", kinds, sent)}
        self.asked.append((klass, method, sent))
        if account is not None and account != self.account_name:
            raise NotConnected(f"no {klass} account is named {account}")
        found = self.items[klass]
        for name, kind in kinds.items():
            if kind.rstrip("!") == "id" and name in typed:
                native = self._native(klass, name, typed[name])
                found = [item for item in found if self._named(klass, method, item) == native]
        if klass == "mail" and method == "search":
            found = [dataclasses.replace(m, body=None) for m in found]
        if klass == "tasks" and method == "tasks" and typed["open_only"]:
            found = [t for t in found if not t.done]
        return [calls.given(item, account=self.account_name,
                            id_of=lambda native: self.id(klass, native))
                for item in found[:typed.get("limit")]]

    def act(self, action: str, args: dict[str, Any]) -> dict:
        calls.proposal(action, args)
        if len(self.proposed) >= calls.MAX_ACTS:
            raise PermissionError(f"this module proposed {calls.MAX_ACTS} actions in one "
                                  "call already")
        self.proposed.append((action, args))
        return dict(calls.PROPOSED)

    @staticmethod
    def id(klass: str, native: str) -> str:
        """Steve's id for the item of `klass` whose `native_id` is `native`."""
        return ids.make(klass, KEY, native)

    @staticmethod
    def _native(klass: str, name: str, given: str) -> str:
        parsed = ids.parse(given)
        if parsed is None or parsed.klass != klass or parsed.account != KEY:
            raise PermissionError(f"{name}: not the id of a {klass} item Steve handed out")
        return parsed.native

    @staticmethod
    def _named(klass: str, method: str, item: Any) -> str:
        # A thread's id is its messages' `thread`; a task list's, its tasks'
        # `list_id`; any other id is the item's own.
        if (klass, method) == ("mail", "thread"):
            return item.thread
        if (klass, method) == ("tasks", "tasks"):
            return item.list_id
        return item.native_id
