"""Example Files: the template for a files provider with a web API of its own.

To make a module of it, copy this folder to `modules/<id>/`, fill each
line of `manifest.toml` marked FILL, and rewrite each call below for the
service's API. Keep what each method returns and refuses: core's tools
rely on it, and the files contract suite (`steve_sdk.testing.files`)
checks it, against `fake.py` once that answers as the service does
(`steve_sdk/templates/__init__.py` says how).

Example's API, which this speaks (`API`):

- `GET /files?q=&limit=&cursor=`: files newest first, without their text;
  `q` is words, searched loosely in names and text.
- `GET /files/{id}`: one file, with its text; 404 once it is gone.
- `POST /documents`, `POST /spreadsheets`: a file made, with its text or
  its rows.
- `GET /spreadsheets/{id}/cells?range=A1:B2`: the rows of a range.
  `PUT /spreadsheets/{id}/cells?range=`: those cells written.
- Every file has a `version`. A write sends it as `If-Match`, and a 412
  says the file changed since.

What every copy keeps, beyond the mail template's rules (only `ctx.http`,
exactly the item named, a condition where the service has one, the
server's answer checked here, errors core can say):

- **Text cut where asked, and said to be.** A file read for a model is
  cut to `max_chars`, and `truncated` says so: a model that reads half a
  document as the whole of it answers wrongly, and confidently.
- **The user's own files only, made and changed as asked.** Core makes a
  document or changes cells without a card, so a change goes to exactly
  the file named, read first, on its version.
"""

from __future__ import annotations

import re
from typing import Any

from steve_sdk import classes
from steve_sdk.classes import FileItem, FilesProvider
from steve_sdk.context import Context
from steve_sdk.cut import cut
from steve_sdk.errors import (
    ConnectorError,
    NotConnected,
    TargetChanged,
    Transient,
    WrongPassword,
)

API = "https://api.example.com/v1"   # FILL: the service's API, on a host the manifest names
TIMEOUT_S = 30.0
PAGES = 10                           # a listing reads at most this many pages
ID = re.compile(r"[A-Za-z0-9._~-]{1,200}")
RANGE = re.compile(r"[A-Z]{1,3}[0-9]{1,7}(:[A-Z]{1,3}[0-9]{1,7})?")
# FILL: the service's own types for its documents and spreadsheets.
DOCUMENT = "application/vnd.example.document"
SPREADSHEET = "application/vnd.example.spreadsheet"
KINDS = {"document": DOCUMENT, "spreadsheet": SPREADSHEET}


# --- the service --------------------------------------------------------------------

def call(ctx: Context, method: str, path: str, *, params: dict[str, str] | None = None,
         body: Any = None, version: str | None = None) -> Any:
    """One request: the JSON answered ({} for none), or None for a 404."""
    headers = {"Authorization": "{{secret:token}}", "Accept": "application/json"}
    if version is not None:
        headers["If-Match"] = version
    try:
        answer = ctx.http.request(method, f"{API}{path}", headers=headers, params=params,
                                  json=body, timeout=TIMEOUT_S)
    except PermissionError as exc:
        # The host rules refused it: the manifest or the account is wrong,
        # which trying again will not mend.
        raise NotConnected(str(exc)) from exc
    if answer.ok:
        return answer.json() if answer.content else {}
    if answer.status == 404:
        return None
    if answer.status in (401, 403):
        raise WrongPassword("the service did not take the token")
    if answer.status == 412:
        raise TargetChanged("the file changed on the service since it was read")
    if answer.status == 429 or answer.status >= 500:
        raise Transient(f"the service answered {answer.status}")
    raise ConnectorError(f"the service answered {answer.status}")


def listing(ctx: Context, path: str, params: dict[str, str], *, limit: int
            ) -> tuple[list[dict[str, Any]], bool]:
    """Up to `limit` items of a listing, page by page; and whether there
    were more than were read."""
    found: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(PAGES):
        asked = {**params, "limit": str(min(max(limit - len(found), 1), 100))}
        if cursor:
            asked["cursor"] = cursor
        page = call(ctx, "GET", path, params=asked) or {}
        found += page.get("items") or []
        cursor = page.get("next") or ""
        if not cursor or len(found) >= limit:
            break
    return found[:limit], bool(cursor) or len(found) > limit


def path_id(native_id: str) -> str:
    """An id as it goes into a path: never one that could name another path."""
    if not ID.fullmatch(native_id or ""):
        raise TargetChanged(f"{native_id!r} is not an id this service gives")
    return native_id


def _cells(cells: str) -> str:
    if not RANGE.fullmatch(cells or ""):
        raise ValueError(f"not a range of cells, such as A1:B2: {cells!r}")
    return cells


def _file(raw: dict[str, Any]) -> FileItem:
    return FileItem(native_id=str(raw["id"]), name=str(raw.get("name") or ""),
                    mime=KINDS.get(str(raw.get("kind") or ""), ""),
                    modified=str(raw.get("modified") or ""), link=str(raw.get("link") or ""),
                    owners=(str(raw["owner"]),) if raw.get("owner") else ())


# --- the provider -------------------------------------------------------------------

class ExampleFiles(FilesProvider):
    # FILL: what the service can make and change. Each one declared is
    # checked by its part of the contract suite.
    capabilities = frozenset({classes.DOCUMENTS, classes.SPREADSHEETS})

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def test(self) -> str:
        """Setup's test and the health check: one read that needs the token."""
        self.recent(limit=1)
        return f"Example's files answer for {self.ctx.account.address}"

    def search(self, query: str, *, limit: int, mime: str | None = None) -> list[FileItem]:
        """Files whose name or text holds every word of `query`, newest
        first, without their text. The service searches loosely, so its
        answer is checked here: by name, or by the text it read for it."""
        words = [w for w in re.findall(r"\w+", (query or "").lower())]
        raw, _ = listing(self.ctx, "/files", {"q": " ".join(words), "text": "1"},
                         limit=max(limit, 50))
        kept = [f for f in raw
                if all(w in f"{f.get('name') or ''} {f.get('text') or ''}".lower()
                       for w in words)]
        found = [_file(f) for f in kept]
        if mime:
            found = [f for f in found if f.mime == mime]
        found.sort(key=lambda f: f.modified, reverse=True)
        return found[:limit]

    def read(self, native_id: str, *, max_chars: int) -> FileItem:
        """One file as text, cut to `max_chars`, and `truncated` if it was."""
        raw = call(self.ctx, "GET", f"/files/{path_id(native_id)}")
        if raw is None:
            raise ConnectorError("no file has that id")
        found = _file(raw)
        text = str(raw.get("text") or "")
        found.text = cut(text, max_chars)
        found.truncated = len(text) > max_chars
        return found

    def _made(self, raw: Any) -> FileItem:
        if not raw or not raw.get("id"):
            raise ConnectorError("the service kept no file")
        return _file(raw)

    def create_document(self, title: str, text: str) -> FileItem:
        return self._made(call(self.ctx, "POST", "/documents",
                               body={"name": title, "text": text}))

    def create_spreadsheet(self, title: str, rows: list[list[str]]) -> FileItem:
        return self._made(call(self.ctx, "POST", "/spreadsheets",
                               body={"name": title, "rows": rows}))

    def read_spreadsheet(self, native_id: str, *, cells: str) -> list[list[str]]:
        said = call(self.ctx, "GET", f"/spreadsheets/{path_id(native_id)}/cells",
                    params={"range": _cells(cells)})
        if said is None:
            raise ConnectorError("no spreadsheet has that id")
        return [[str(c) for c in row] for row in said.get("rows") or []]

    def update_spreadsheet(self, native_id: str, *, cells: str, rows: list[list[str]]
                           ) -> FileItem:
        """Write `rows` into exactly the spreadsheet named, read first, on
        the version read."""
        found = call(self.ctx, "GET", f"/files/{path_id(native_id)}")
        if found is None or found.get("kind") != "spreadsheet":
            raise TargetChanged("no spreadsheet has that id")
        said = call(self.ctx, "PUT", f"/spreadsheets/{path_id(native_id)}/cells",
                    params={"range": _cells(cells)}, body={"rows": rows},
                    version=found.get("version"))
        if said is None:
            raise TargetChanged("the spreadsheet was gone when it was to be changed")
        return FileItem(native_id=native_id, name=str(found.get("name") or ""),
                        extra={"cells": str(sum(len(r) for r in rows)), "range": cells})
