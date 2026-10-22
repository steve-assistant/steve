"""A web service in memory: what every template's fake has in common.

Each template (`steve_sdk/templates`) reaches its service through
`ctx.http`, and its fake answers as the service would, from memory, so the
class's contract suite runs against it with no network. A fake that was
kinder than its service would let a module pass a suite the service would
fail: a write sent with no condition looks the same as one sent with it,
until another device changes the item in between. So what any such service
does is here once, and strict:

- a bearer token checked on every request (401 without it);
- an id for every item, and a version that changes with every change;
- a write without `If-Match` refused (428), and one whose `If-Match` is not
  the item's version refused (412), changing nothing;
- an id it does not hold answered 404;
- a listing a page at a time, with the cursor for the next.

Each template's fake adds only its class's routes and listings, and how
`seed`, `mutate` and `snapshot` see its items. A copy of a template
rewrites its fake to answer as the real service does, habits included.
"""

from __future__ import annotations

import itertools
import json
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from steve_sdk.testing import World

TOKEN = "token-for-the-fake"


@dataclass
class Request:
    method: str
    path: str                                               # "/v1/messages/m3"
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None                                        # the JSON sent
    headers: dict[str, str] = field(default_factory=dict)   # names lower case

    @property
    def parts(self) -> list[str]:
        return [part for part in self.path.split("/") if part]


@dataclass
class Answer:
    status: int
    body: Any = None

    @property
    def content(self) -> bytes:
        return b"" if self.body is None else json.dumps(self.body).encode("utf-8")


class Refusal(Exception):
    """An answer other than success, raised from a route."""

    def __init__(self, status: int, said: str) -> None:
        super().__init__(said)
        self.status = status


class Service(World):
    """The store and the rules above; a subclass routes (`route`)."""

    def __init__(self, *, token: str = TOKEN) -> None:
        self.token = token
        self.items: dict[str, dict[str, dict[str, Any]]] = {}   # kind -> id -> item
        self.requests: list[Request] = []
        self._ids = itertools.count(1)
        self._versions = itertools.count(1)

    def answer(self, request: Request) -> Answer:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return Answer(401, {"error": "the token is missing or wrong"})
        try:
            return self.route(request)
        except Refusal as no:
            return Answer(no.status, {"error": str(no)})

    @abstractmethod
    def route(self, request: Request) -> Answer:
        """The answer to one request whose token was right."""

    # --- what a fake's routes use ---------------------------------------------

    def new_id(self, kind: str) -> str:
        return f"{kind}-{next(self._ids)}"

    def add(self, kind: str, item: dict[str, Any]) -> dict[str, Any]:
        """Store `item` under a new id, at its first version."""
        item["id"] = self.new_id(kind)
        self.touch(item)
        self.items.setdefault(kind, {})[item["id"]] = item
        return item

    def touch(self, item: dict[str, Any]) -> None:
        """A new version: any change, whoever made it."""
        item["version"] = f"v{next(self._versions)}"

    def one(self, kind: str, item_id: str) -> dict[str, Any]:
        found = self.items.get(kind, {}).get(item_id)
        if found is None:
            raise Refusal(404, f"no {kind} has the id {item_id}")
        return found

    def condition(self, request: Request, item: dict[str, Any]) -> None:
        """A write goes only on the version it was read at."""
        wanted = request.headers.get("if-match")
        if wanted is None:
            raise Refusal(428, "a write here needs If-Match")
        if wanted != item["version"]:
            raise Refusal(412, "the item changed since that version was read")

    def page(self, items: list[dict[str, Any]], request: Request,
             shown: Any = None) -> Answer:
        """A listing, `limit` at a time (at most 100), and `next`, the
        cursor for the rest. `shown` is what the listing shows of each."""
        limit = min(int(request.query.get("limit") or 50), 100)
        start = int(request.query.get("cursor") or 0)
        chunk = items[start:start + limit]
        more = str(start + limit) if start + limit < len(items) else None
        return Answer(200, {"items": [shown(i) if shown else dict(i) for i in chunk],
                            "next": more})
