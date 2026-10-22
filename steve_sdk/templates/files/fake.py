"""Example Files' API in memory: the template's fake, and a `World` for the
files contract suite (`steve_sdk.testing.files`).

It answers the routes `provider.py` names, strictly (`steve_sdk.testing.api`):
a write needs the version it was read at, and a search is loose, any word
anywhere, as a service's relevance search is. A copy rewrites it to answer
as its own service does, habits included.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from steve_sdk.testing.api import TOKEN, Answer, Refusal, Request, Service

CELL = re.compile(r"([A-Z]{1,3})([0-9]{1,7})")


def _iso(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _column(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - ord("A") + 1
    return number - 1


def _range(text: str) -> tuple[int, int, int, int]:
    """A1:B2 as first row, first column, last row, last column, from 0."""
    corners = [CELL.fullmatch(part) for part in text.split(":")]
    if not all(corners) or len(corners) > 2:
        raise Refusal(400, f"not a range: {text!r}")
    first, last = corners[0], corners[-1]
    return (int(first[2]) - 1, _column(first[1]), int(last[2]) - 1, _column(last[1]))


class FilesService(Service):
    """The service, holding the files of the account at `user`."""

    def __init__(self, user: str = "maya@mail.example", *, token: str = TOKEN) -> None:
        super().__init__(token=token)
        self.user = user

    def _file(self, name: str, kind: str, *, text: str = "",
              rows: list[list[str]] | None = None, modified: str = "") -> dict[str, Any]:
        item = self.add("file", {"name": name, "kind": kind, "text": text,
                                 "rows": [list(r) for r in rows or []], "owner": self.user,
                                 "modified": modified or _iso(dt.datetime.now(dt.UTC))})
        item["link"] = f"https://files.example.com/{item['id']}"
        return item

    # --- the World the contract suite drives ----------------------------------------

    def seed(self, klass: str, items: list[dict[str, Any]]) -> list[str]:
        assert klass == "files", klass
        return [self._file(str(spec["name"]), spec.get("kind", "document"),
                           text=str(spec.get("text") or ""), rows=spec.get("rows"),
                           modified=_iso(spec["modified"]) if spec.get("modified") else ""
                           )["id"] for spec in items]

    def mutate(self, klass: str, native_id: str, **changes: Any) -> None:
        """As another device would: a new name or text, or `gone`."""
        assert klass == "files", klass
        item = self.one("file", native_id)
        if changes.pop("gone", False):
            del self.items["file"][native_id]
            return
        for key in ("name", "text"):
            if key in changes:
                item[key] = changes[key]
        self.touch(item)

    def snapshot(self, klass: str) -> dict[str, Any]:
        """Every file by id: its name, version and contents."""
        return {item["id"]: (item["name"], item["version"], item["text"],
                             tuple(tuple(r) for r in item["rows"]))
                for item in self.items.get("file", {}).values()}

    # --- the API ----------------------------------------------------------------------

    def route(self, request: Request) -> Answer:
        parts = request.parts
        if parts[:1] != ["v1"]:
            raise Refusal(404, f"no such route: {request.path}")
        body = request.body or {}
        match (request.method, parts[1:]):
            case ("GET", ["files"]):
                return self._listing(request)
            case ("GET", ["files", native]):
                return Answer(200, self._shown(self.one("file", native), text=True))
            case ("POST", ["documents"]):
                made = self._file(str(body.get("name") or ""), "document",
                                  text=str(body.get("text") or ""))
                return Answer(201, self._shown(made))
            case ("POST", ["spreadsheets"]):
                made = self._file(str(body.get("name") or ""), "spreadsheet",
                                  rows=body.get("rows") or [])
                return Answer(201, self._shown(made))
            case ("GET", ["spreadsheets", native, "cells"]):
                return Answer(200, {"rows": self._cells(self._sheet(native),
                                                        request.query.get("range", ""))})
            case ("PUT", ["spreadsheets", native, "cells"]):
                return self._write(request, native)
        raise Refusal(404, f"no such route: {request.method} {request.path}")

    def _shown(self, item: dict[str, Any], *, text: bool = False) -> dict[str, Any]:
        shown = {k: v for k, v in item.items() if k not in ("rows", "text")}
        if text:
            shown["text"] = item["text"] or "\n".join(",".join(r) for r in item["rows"])
        return shown

    def _listing(self, request: Request) -> Answer:
        words = [w for w in (request.query.get("q") or "").lower().split() if w]
        found = [item for item in self.items.get("file", {}).values()
                 if not words or any(w in f"{item['name']} {item['text']}".lower()
                                     for w in words)]
        found.sort(key=lambda i: i["modified"], reverse=True)
        with_text = request.query.get("text") == "1"
        return self.page(found, request, lambda i: self._shown(i, text=with_text))

    def _sheet(self, native: str) -> dict[str, Any]:
        item = self.one("file", native)
        if item["kind"] != "spreadsheet":
            raise Refusal(400, "not a spreadsheet")
        return item

    def _cells(self, item: dict[str, Any], text: str) -> list[list[str]]:
        top, left, bottom, right = _range(text)
        return [[row[c] if c < len(row) else "" for c in range(left, right + 1)]
                for row in item["rows"][top:bottom + 1]]

    def _write(self, request: Request, native: str) -> Answer:
        item = self._sheet(native)
        self.condition(request, item)
        top, left, _, _ = _range(request.query.get("range", ""))
        for r, values in enumerate((request.body or {}).get("rows") or []):
            while len(item["rows"]) <= top + r:
                item["rows"].append([])
            row = item["rows"][top + r]
            for c, value in enumerate(values):
                while len(row) <= left + c:
                    row.append("")
                row[left + c] = str(value)
        item["modified"] = _iso(dt.datetime.now(dt.UTC))
        self.touch(item)
        return Answer(200, {"cells": sum(len(v) for v in request.body.get("rows") or [])})
