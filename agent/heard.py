"""What the user said: their own messages, read back from the database.

Four readers want their words -- the judge's `lately`, the checks before a
ping (`vet.user_words`), the night's evidence and its learning -- and each
had its own copy of the query and of the channels it ran on. With the
copies in `closing` and `context`, that was six spellings of the same four
names, and a copy is where the next channel goes missing.
`core.call_policy.PRESENT_CHANNELS` is the one list of the sessions where the user
is the one talking. A 'user' row anywhere else is the agent talking to
itself -- the night's research prompts, the attention loop's initiative,
queued jobs -- and none of those is their hand.
"""

from __future__ import annotations

import sqlite3

from core.call_policy import PRESENT_CHANNELS


def user_messages(conn: sqlite3.Connection, *, hours: float | None = None,
                 since: str | None = None, until: str | None = None,
                 latest: int | None = None) -> list[sqlite3.Row]:
    """Their messages, oldest first, as rows of `id, session_id, channel,
    content, created_at`: from the last `hours` by SQLite's clock and from
    `since`, up to `until` (both SQLite UTC); `latest` keeps only the last
    so many. Blocking (the database)."""
    where = ["m.role = 'user'", f"s.channel IN ({', '.join('?' * len(PRESENT_CHANNELS))})"]
    args: list = sorted(PRESENT_CHANNELS)
    for clause, value in (("m.created_at >= datetime('now', ?)",
                           None if hours is None else f"-{hours} hours"),
                          ("m.created_at >= ?", since), ("m.created_at <= ?", until)):
        if value is not None:
            where.append(clause)
            args.append(value)
    limit = ""
    if latest is not None:
        limit = " LIMIT ?"
        args.append(latest)
    rows = conn.execute(
        "SELECT m.id, m.session_id, s.channel, m.content, m.created_at FROM messages m"
        f" JOIN sessions s ON s.id = m.session_id WHERE {' AND '.join(where)}"
        f" ORDER BY m.id DESC{limit}", args).fetchall()
    return rows[::-1]
