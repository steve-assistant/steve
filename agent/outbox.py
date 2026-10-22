"""Every message to the user, written down before anything sends it.

Everything that reached the user unprompted went through `notify_user`,
which tried Signal once and otherwise logged "dropped" and returned False.
Five callers retried on their own, each its own way: a dict of reminders in
memory, a routine's held report, a goal's owed report, a sweep of finished
jobs, a card asked twice ten seconds apart. The rest dropped silently: a
model switch put back, a goal started from the user's mail, where the notes
of a call went. With no relay to hold them, a laptop whose Signal was down
had nowhere to put any of it.

So a message is a row first (`add`). The app's inbox shows every row, and
the paired remote channel is sent every row, tried again on a schedule
(`RETRY_AFTER`) until it goes or its time is up (`until`: a briefing's
lateness, otherwise `KEEP_TRYING`). A row that stopped trying says why, and
the inbox shows it. Nothing is dropped, and no caller retries.

**Told means told.** A row joins the conversation the user's reply will land
in, and its lines in `told` get their `said_at`, only once something has
confirmed the user has it: the remote channel took it (`sent`), or the app
showed it (`seen`). Each joins the conversation of the place it reached, so a
reply on the phone and a reply in the app each find what it answers.

The sending is the worker's (`agent/worker/links.send_due`); this module is
the table, and every moment in it is SQLite's UTC text (`agent/sqltime.py`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent import sqltime
from core import db

KINDS = ("reminder", "briefing", "card", "notice", "job", "reply")

# How long after each failed send the next try waits; hourly after the last.
RETRY_AFTER = (timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=5),
               timedelta(minutes=15), timedelta(minutes=30))
RETRY_THEN = timedelta(hours=1)

# How long a row is tried for, unless its caller says otherwise: a day covers
# a night asleep or a channel down; older is news nobody waits for.
KEEP_TRYING = timedelta(hours=24)

# Why a row stopped trying once its time ran out.
TOO_LATE = "its time ran out before it could be sent"


@dataclass(frozen=True)
class Message:
    id: int
    kind: str
    text: str
    noted: str | None
    action_id: int | None
    remote: str | None
    created_at: str
    until: str
    seen_at: str | None
    sent_at: str | None
    attempts: int
    next_try_at: str | None
    stopped: str | None
    reminder_id: int | None = None  # a reminder's: what "Later" books again

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Message:
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})

    @property
    def recorded(self) -> str:
        """What a conversation records of it."""
        return self.noted or self.text

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now()


def add(conn: sqlite3.Connection, text: str, *, kind: str, noted: str | None = None,
        remote: str | None = None, action_id: int | None = None,
        until: datetime | None = None, now: datetime | None = None,
        reminder_id: int | None = None) -> int:
    """Write one message; returns its id. With a `remote` channel it is due a
    send at once; without one it is the app's alone."""
    if kind not in KINDS:
        raise ValueError(f"not a kind of message: {kind!r}")
    now = _now(now)
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO outbox (kind, text, noted, action_id, remote, created_at, until,"
            " next_try_at, reminder_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, text, noted, action_id, remote, sqltime.stamp(now),
             sqltime.stamp(until or now + KEEP_TRYING),
             sqltime.stamp(now) if remote else None, reminder_id))
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, message_id: int) -> Message | None:
    row = conn.execute("SELECT * FROM outbox WHERE id = ?", (int(message_id),)).fetchone()
    return Message.from_row(row) if row else None


def next_due(conn: sqlite3.Connection, now: datetime | None = None) -> Message | None:
    """The oldest row due a try. One past its time is stopped first, not
    sent: a morning briefing that arrives at lunch is worse than none."""
    stamp = sqltime.stamp(_now(now))
    with db.transaction(conn):
        conn.execute("UPDATE outbox SET next_try_at = NULL, stopped = ?"
                     " WHERE next_try_at IS NOT NULL AND until < ?", (TOO_LATE, stamp))
    row = conn.execute("SELECT * FROM outbox WHERE next_try_at <= ? ORDER BY id LIMIT 1",
                       (stamp,)).fetchone()
    return Message.from_row(row) if row else None


def sent(conn: sqlite3.Connection, message_id: int, now: datetime | None = None) -> None:
    """The remote channel took it. Whatever else waits for that channel is
    tried now, rather than at the end of its back-off: the channel is back."""
    stamp = sqltime.stamp(_now(now))
    with db.transaction(conn):
        row = conn.execute("UPDATE outbox SET sent_at = ?, next_try_at = NULL WHERE id = ?"
                           " RETURNING remote", (stamp, int(message_id))).fetchone()
        if row is not None:
            conn.execute("UPDATE outbox SET next_try_at = ? WHERE remote = ?"
                         " AND next_try_at > ?", (stamp, row["remote"], stamp))
        _settle_told(conn, [int(message_id)], stamp)


def failed(conn: sqlite3.Connection, message_id: int, why: str,
           now: datetime | None = None) -> None:
    """A try that did not go: the next one after the back-off, or none if
    that would be past its time, with why."""
    now = _now(now)
    message = get(conn, message_id)
    if message is None:
        return
    wait = RETRY_AFTER[message.attempts] if message.attempts < len(RETRY_AFTER) else RETRY_THEN
    later = now + wait
    over = later.astimezone() > sqltime.moment(message.until)
    with db.transaction(conn):
        conn.execute("UPDATE outbox SET attempts = attempts + 1, next_try_at = ?, stopped = ?"
                     " WHERE id = ?",
                     (None if over else sqltime.stamp(later), why if over else None,
                      message.id))


def stop(conn: sqlite3.Connection, message_id: int, why: str) -> None:
    """No more tries, for a reason other than failing: a card answered
    before it could be sent."""
    with db.transaction(conn):
        conn.execute("UPDATE outbox SET next_try_at = NULL, stopped = ? WHERE id = ?"
                     " AND sent_at IS NULL", (why, int(message_id)))


def stop_channel(conn: sqlite3.Connection, remote: str, why: str) -> int:
    """No more tries for any unsent message to `remote`: the user unpaired
    it, and nothing will send them. They stay in the app's inbox, saying
    why. Returns how many stopped."""
    with db.transaction(conn):
        return conn.execute("UPDATE outbox SET next_try_at = NULL, stopped = ? WHERE remote = ?"
                            " AND sent_at IS NULL AND stopped IS NULL", (why, remote)).rowcount


def seen(conn: sqlite3.Connection, ids: list[int], now: datetime | None = None
         ) -> list[Message]:
    """Shown in the app. Returns those seen for the first time, for the
    app's conversation to record."""
    stamp = sqltime.stamp(_now(now))
    with db.transaction(conn):
        rows = [conn.execute("UPDATE outbox SET seen_at = ? WHERE id = ? AND seen_at IS NULL"
                             " RETURNING *", (stamp, int(i))).fetchone() for i in ids]
        fresh = [Message.from_row(r) for r in rows if r is not None]
        _settle_told(conn, [m.id for m in fresh], stamp)
    return fresh


def _settle_told(conn: sqlite3.Connection, ids: list[int], stamp: str) -> None:
    """The lines these messages carried, said now: the first place they
    reached is when the user had them."""
    conn.executemany("UPDATE told SET said_at = ? WHERE outbox_id = ? AND said_at IS NULL",
                     [(stamp, i) for i in ids])


# How many rows the inbox lists.
INBOX_SIZE = 100


def inbox(conn: sqlite3.Connection, limit: int = INBOX_SIZE) -> list[Message]:
    """Unread first, then newest first."""
    rows = conn.execute("SELECT * FROM outbox ORDER BY seen_at IS NOT NULL, id DESC LIMIT ?",
                        (int(limit),)).fetchall()
    return [Message.from_row(r) for r in rows]
