"""Sharing a module: who the user is kept out of an export, and an import admitted as from outside.

Core makes and unpacks the zip (`core/sharing.py`), but it cannot know who
the user is, and that is what an export must not carry. A module Steve
built for the user is written from their requests, and may hold their
address as a default or a friend's name in a test. So the scan's terms are
the user's own: their names and city as they gave them, their addresses,
and the people they write to, by address and by full name. In a source
tree the developer's denylist is added (`devtools/denylist.py`), so a
maintainer's module does not carry the maintainer either.

An import is unpacked into a quarantine folder of its own under the
scratch folder, admitted from there as one from outside, and the folder
deleted whatever happened. Both are the app's alone
(tests/gate/test_only_the_app.py): an export sends the user's code away,
and an import is a card the user asked for.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import sqlite3
from typing import Any

from agent import facts, modadmit
from core import lifecycle, modules, paths, sharing


def terms(conn: sqlite3.Connection) -> list[str]:
    """Who a module's code must not name on its way out."""
    said = facts.identity(conn)
    found = [said["name"], said["preferred_name"], said["home_city"]]
    found += facts.user_addresses(conn)
    # No fallback for an unreadable `people`: it was a guard for a database
    # from before there were people (every one has it since 0001), and it
    # read a locked database as nobody to keep out, so a module's code went
    # out unchecked for the user's people. Unreadable now stops the share.
    rows = conn.execute("SELECT email, name FROM people WHERE is_person = 1").fetchall()
    for email, name in rows:
        found += [email, name or ""]
    return [term for term in found if term]


def _denylist() -> sharing.Finder | None:
    try:
        from devtools import denylist
    except ImportError:  # installed: there is no developer to keep out
        return None
    hashed = denylist.load()
    return (lambda text: denylist.find(text, hashed)) if hashed else None


def scan(conn: sqlite3.Connection) -> sharing.Finder:
    mine = sharing.finder(terms(conn))
    theirs = _denylist()
    return (lambda text: mine(text) + theirs(text)) if theirs else mine


def export_zip(conn: sqlite3.Connection, module_id: str) -> tuple[str, bytes]:
    """The module's running version as a zip, and the name to save it by."""
    held = modules.module(conn, module_id)
    if held is None:
        raise modules.AccountError(f"no module {module_id!r} is installed")
    if held.origin == "builtin":
        raise lifecycle.Refused(f"{module_id} comes with Steve, so there is nothing of it "
                                f"to share")
    return f"{module_id}-{held.version}.zip", sharing.export(module_id, held.version,
                                                            scan(conn))


async def import_zip(conn: sqlite3.Connection, data: bytes, *, replace: bool = False,
                     **admission: Any) -> int:
    """A zip the user chose, up to its card: the card's admission id, or
    Refused saying why not. `admission` is admit's seams, for tests."""
    folder = paths.scratch_dir() / f"import-{secrets.token_hex(6)}"
    try:
        await asyncio.to_thread(sharing.extract, data, folder)
        return await modadmit.admit(conn, folder, origin="imported", replace=replace,
                                    **admission)
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)
