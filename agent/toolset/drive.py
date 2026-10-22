"""Their files: documents and spreadsheets, searched, read and made through
the connector slot (`agent/connectors.py`)."""

from __future__ import annotations

import asyncio
import logging

from pydantic_ai import RunContext

from agent import connectors, screening
from agent.actions import act, executable
from agent.approvals import one_line
from agent.connectors import Connected, NoAccount
from agent.toolset import oncard
from agent.turn import Deps
from steve_sdk import classes
from steve_sdk.classes import FileItem
from steve_sdk.errors import ConnectorError, NotConnected

log = logging.getLogger("toolset.drive")


def listed(found: Connected, item: FileItem) -> dict:
    """A file as a search lists it: an id `read_drive_file` takes back to its
    account, its name, kind, when it last changed, its owner and its page."""
    return {"id": found.id(item.native_id), "name": item.name or None,
            "mime_type": item.mime or None, "modified": item.modified or None,
            "size": item.extra.get("size"), "link": item.link or None,
            "owner": item.owners[0] if item.owners else None}


async def screened(conn, files: list[dict]) -> list[dict]:
    """Files as a search lists them, each one's name and owner as the
    injection screen allows (agent/screening.py): whoever shares a file with
    the user names it. One the screen did not pass keeps its id, kind, dates
    and page, and loses its name and owner to what the screen allows. Names
    reached the model unscreened, and left the conversation free to open any
    address, until step 11's audit."""
    seen = await asyncio.gather(*(screening.shown(
        conn, "drive_file_name", f["id"], f.get("owner"),
        f"{f.get('name') or ''}\nOwner: {f.get('owner') or ''}") for f in files))
    for listing, shown in zip(files, seen, strict=True):
        if shown.how != "text":
            listing.pop("name", None)
            listing.pop("owner", None)
            listing.update(screened=shown.how, text=shown.text)
    return files


def maker(conn, capability: str) -> Connected:
    """The first files account that can make this kind of file. Raises
    NoAccount, or Unsupported naming the account."""
    chosen = connectors.accounts(conn, "files")
    if not chosen:
        raise NoAccount("no files account is connected")
    able = [found for found in chosen if found.provider.has(capability)]
    if not able:
        connectors.require(chosen[0], capability)  # raises, naming it
    return able[0]


@executable("update_spreadsheet")
async def _do_update_spreadsheet(
    *, spreadsheet_id: str, rows: list[list[str]], range_name: str = "A1"
) -> dict:
    try:
        found, native = oncard.routed(spreadsheet_id)
    except NoAccount as exc:
        return {"ok": False, "error": str(exc)}
    try:
        connectors.require(found, classes.SPREADSHEETS)
        done = await connectors.call(found, lambda p: p.update_spreadsheet(
            native, cells=range_name, rows=rows))
    except ConnectorError as exc:
        return oncard.failed(found, exc)
    return {"updated": True, "id": spreadsheet_id,
            "cells": int(done.extra.get("cells") or 0),
            "range": done.extra.get("range") or range_name}


def register(tool) -> None:
    """Attach the drive tools. `tool` is `register_tools`'s filter."""

    @tool
    async def search_drive(ctx: RunContext[Deps], query: str, limit: int = 10) -> dict:
        """Find files in Drive by name or contents."""
        files, problems = [], []
        accounts = connectors.accounts(ctx.deps.conn, "files")
        for answer in await connectors.ask_all(accounts, lambda p: p.search(query, limit=limit)):
            if answer.error is not None:
                if not isinstance(answer.error, NotConnected):
                    problems.append(connectors.say(answer.error, answer.account.name))
                continue
            files += [listed(answer.account, item) for item in answer.result]
        if problems and not files:
            return {"ok": False, "error": " ".join(problems)}
        files = await screened(ctx.deps.conn, files)
        out = {"query": query, "count": len(files), "files": files}
        if problems:
            out["problems"] = problems
        return out

    @tool
    async def read_drive_file(ctx: RunContext[Deps], file_id: str) -> dict:
        """Read a file as text. A file in its account's own format is exported."""
        try:
            found, native = connectors.by_id(ctx.deps.conn, file_id)
            item = await connectors.call(found, lambda p: p.read(native, max_chars=20_000))
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        except ConnectorError as exc:
            return oncard.failed(found, exc)
        # A shared document is someone else's words, its title included
        # (agent/screening.py).
        seen = await screening.shown(
            ctx.deps.conn, "drive_doc", file_id, item.name,
            f"Title: {item.name or ''}\n\n{item.text or ''}")
        if seen.how != "text":
            return {"id": file_id, "mime_type": item.mime or None,
                    "truncated": item.truncated, "screened": seen.how, "text": seen.text}
        return {"id": file_id, "name": item.name or None, "mime_type": item.mime or None,
                "text": item.text or "", "truncated": item.truncated}

    @tool
    async def create_document(
        ctx: RunContext[Deps], title: str, content: str
    ) -> dict:
        """Create a document. `content` may use markdown headings and bullets."""
        found = None
        try:
            found = maker(ctx.deps.conn, classes.DOCUMENTS)
            made = await connectors.call(found, lambda p: p.create_document(title, content))
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        except ConnectorError as exc:
            return oncard.failed(found, exc)
        return {"created": True, **listed(found, made)}

    @tool
    async def create_spreadsheet(
        ctx: RunContext[Deps], title: str, rows: list[list[str]]
    ) -> dict:
        """Create a spreadsheet. First row is the header.

        Cells are entered as if typed, so "=SUM(A1:A9)" becomes a real formula
        and "50%" becomes a percentage.
        """
        found = None
        try:
            found = maker(ctx.deps.conn, classes.SPREADSHEETS)
            made = await connectors.call(found, lambda p: p.create_spreadsheet(title, rows))
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        except ConnectorError as exc:
            return oncard.failed(found, exc)
        return {"created": True, **listed(found, made), "rows_written": len(rows)}

    @tool
    async def read_spreadsheet(
        ctx: RunContext[Deps], spreadsheet_id: str, range_name: str = "A1:Z1000"
    ) -> dict:
        """Read cells out of a Sheet."""
        try:
            found, native = connectors.by_id(ctx.deps.conn, spreadsheet_id)
            connectors.require(found, classes.SPREADSHEETS)
            values = await connectors.call(
                found, lambda p: p.read_spreadsheet(native, cells=range_name))
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        except ConnectorError as exc:
            return oncard.failed(found, exc)
        # The range asked for, not the one the service names: that one
        # carries the tab's name, which the sheet's author wrote.
        out = {"id": spreadsheet_id, "range": range_name, "rows": len(values),
               "values": values}
        if values:
            # A shared sheet's cells are someone else's words, as a
            # document's are.
            seen = await screening.shown(
                ctx.deps.conn, "drive_sheet", spreadsheet_id, range_name,
                "\n".join("\t".join(map(str, row)) for row in values))
            if seen.how != "text":
                return {"id": spreadsheet_id, "range": range_name, "rows": len(values),
                        "screened": seen.how, "text": seen.text}
        return out

    @tool
    async def update_spreadsheet(
        ctx: RunContext[Deps],
        spreadsheet_id: str,
        rows: list[list[str]],
        range_name: str = "A1",
    ) -> dict:
        """Write cells into a Sheet, overwriting what is there.

        There is no undo the agent can call -- Drive keeps version history,
        but only a human can restore it -- so read the range first if you are
        not certain what you are replacing.
        """
        try:
            connectors.by_id(ctx.deps.conn, spreadsheet_id)
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        args = {
            "spreadsheet_id": spreadsheet_id, "rows": rows, "range_name": range_name,
        }
        return await act(
            ctx, "update_spreadsheet", args,
            preview=f"Overwrite {one_line(range_name)} in sheet {one_line(spreadsheet_id)} "
                    f"with {len(rows)} row(s)",
        )
