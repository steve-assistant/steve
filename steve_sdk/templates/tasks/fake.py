"""Example Tasks' API in memory: the template's fake, and a `World` for the
tasks contract suite (`steve_sdk.testing.tasks`).

It answers the routes `provider.py` names, strictly (`steve_sdk.testing.api`):
a write needs the version it was read at. The account's own list is
"Tasks"; a task seeded with another `list` makes that list. A copy rewrites
it to answer as its own service does, habits included.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from steve_sdk.testing.api import TOKEN, Answer, Refusal, Request, Service

OWN = "Tasks"


def _iso(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TasksService(Service):
    """The service, holding the task lists of the account at `user`."""

    def __init__(self, user: str = "maya@mail.example", *, token: str = TOKEN) -> None:
        super().__init__(token=token)
        self.user = user
        self._list(OWN, own=True)

    def _list(self, title: str, *, own: bool = False) -> str:
        for found in self.items.get("list", {}).values():
            if found["title"] == title:
                return found["id"]
        return self.add("list", {"title": title, "own": own})["id"]

    def _task(self, list_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        item = self.add("task", {"list": list_id, "title": str(fields.get("title") or ""),
                                 "notes": str(fields.get("notes") or ""),
                                 "due": fields.get("due") or None,
                                 "done": bool(fields.get("done")),
                                 "updated": _iso(dt.datetime.now(dt.UTC))})
        item["link"] = f"https://tasks.example.com/{item['id']}"
        return item

    # --- the World the contract suite drives ----------------------------------------

    def seed(self, klass: str, items: list[dict[str, Any]]) -> list[str]:
        assert klass == "tasks", klass
        return [self._task(self._list(spec.get("list") or OWN), spec)["id"] for spec in items]

    def mutate(self, klass: str, native_id: str, **changes: Any) -> None:
        """As the phone would: a new title, notes or due date, ticked, or `gone`."""
        assert klass == "tasks", klass
        item = self.one("task", native_id)
        if changes.pop("gone", False):
            del self.items["task"][native_id]
            return
        for key in ("title", "notes", "due", "done"):
            if key in changes:
                item[key] = changes[key]
        self.touch(item)

    def snapshot(self, klass: str) -> dict[str, Any]:
        """Every task by id: its list, title, notes, due date, state and version."""
        return {item["id"]: (item["list"], item["title"], item["notes"], item["due"],
                             item["done"], item["version"])
                for item in self.items.get("task", {}).values()}

    # --- the API ----------------------------------------------------------------------

    def route(self, request: Request) -> Answer:
        parts = request.parts
        if parts[:1] != ["v1"]:
            raise Refusal(404, f"no such route: {request.path}")
        match (request.method, parts[1:]):
            case ("GET", ["lists"]):
                return self.page(list(self.items.get("list", {}).values()), request)
            case ("GET", ["lists", list_id, "tasks"]):
                self.one("list", list_id)
                every = request.query.get("state") == "all"
                found = [t for t in self.items.get("task", {}).values()
                         if t["list"] == list_id and (every or not t["done"])]
                return self.page(found, request)
            case ("GET", ["tasks", native]):
                return Answer(200, dict(self.one("task", native)))
            case ("POST", ["lists", list_id, "tasks"]):
                self.one("list", list_id)
                return Answer(201, dict(self._task(list_id, request.body or {})))
            case ("PATCH", ["tasks", native]):
                item = self.one("task", native)
                self.condition(request, item)
                for key in ("title", "notes", "due", "done"):
                    if key in (request.body or {}):
                        item[key] = request.body[key]
                item["updated"] = _iso(dt.datetime.now(dt.UTC))
                self.touch(item)
                return Answer(200, dict(item))
        raise Refusal(404, f"no such route: {request.method} {request.path}")
