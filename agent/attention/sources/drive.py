"""Files someone else changed, in each files account the user connected.

Only other people's: the watcher this replaces reported the user's own edits
back to them as news. A file owned by someone else and recently modified is
usually a share or a teammate's update to a shared deck -- worth a line in a
briefing now and then, almost never a ping, which the judge decides.

Keyed by the file's slot id and modification time, so each edit is one
observation, and the key names its account: one switched off has its
queued observations skipped (`core/modules.TAGGED`). Until 14 October 2026
it read Google Drive alone, by Drive's own ids, which no tool could take
back to the account.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from agent.attention.sources import SourceError
from agent.attention.store import Observation

# How many recently changed files one poll lists, per account.
LIMIT = 15


def _where(account: str) -> str:
    """Which of their files, by the account's name. Until step 5's item 9
    an account named "google" was called "their Drive": a branch on a name,
    and no account added in the app is named that (it is named by its
    address, or by the user)."""
    return f"their files ({account})" if account else "their files"


def shared(conn: sqlite3.Connection) -> list[Observation]:
    from agent import connectors
    from steve_sdk.errors import NotConnected

    mine = connectors.addresses(conn)
    if not mine:
        # Without their address every file looks like someone else's, which
        # is the noise this source exists to leave out. Try next time.
        raise SourceError("could not tell which files are the user's")
    out = []
    for found in connectors.accounts(conn, "files"):
        try:
            files = connectors.blocking(found, lambda p: p.recent(limit=LIMIT), limit_s=None)
        except NotConnected:
            continue
        for item in files:
            # Theirs if they are any of its owners; unknown owners are not news.
            if not item.owners or any(o.lower() in mine for o in item.owners):
                continue
            owner = item.owners[0]
            file_id = found.id(item.native_id)
            out.append(
                Observation(
                    source="drive",
                    key=f"drive:{file_id}:{item.modified}",
                    title=f"{item.name or '(untitled)'} -- changed in {_where(found.name)}",
                    detail=f"owner {owner}; {item.mime or 'file'}",
                    when=item.modified or None,
                    ref={"file_id": file_id, "account": found.name, "link": item.link,
                         "owner": owner},
                )
            )
    return out


def screened(conn: sqlite3.Connection, observation: Observation) -> Observation:
    """A file's name as the judge may read it (agent/screening.py). Anyone
    can share a file with them, and whoever shares it names it: until 11
    October 2026 the name reached the judge unscreened. A name the screen
    did not pass is not shown; the owner's address, which Drive gives, and
    what the screen allows stand in its place."""
    from agent import screening

    seen = screening.shown_here(conn, "drive_file_name", observation.ref.get("file_id"),
                                observation.ref.get("owner"), observation.title)
    if seen.how == "text":
        return observation
    where = _where(str(observation.ref.get("account") or ""))
    return replace(observation, title=f"A file someone else changed in {where}",
                   detail=f"{observation.detail}. {seen.text}")
