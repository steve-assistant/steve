"""Things to say later, and things to keep checking.

Two shapes, one table, because they differ only in what firing means:

    remind — "at 5pm remind me to grab groceries"
    check  — "every 10 minutes for the next hour, what's the score?"

The second is the interesting one. Running a question every ten minutes and
reporting the answer every time is how you teach someone to ignore you: six
messages saying 14-7 bury the one saying 21-7. So a check reports when the
answer *changes*, plus once at the start so the first reading arrives, plus
once at the end so it closes the loop rather than trailing off. The user asked
to be told the score, not to be told every ten minutes.

**These fire whatever the hour**, which is the opposite of the watcher. The
watcher is unsolicited and keeps quiet hours; a reminder was asked for, and
an alarm that declines to go off at six in the morning is not an alarm.

Time arrives as ISO from the model, which knows the current time because it
is injected into every turn. Parsing "next Tuesday" is a job a language model
is good at and a regular expression is not, so it is not attempted here --
but the result is validated, because a model that is good at something is not
the same as a model that is reliable at it.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from core import db

log = logging.getLogger("reminders")

# How often the scheduler looks. Twenty seconds keeps "at 5pm" honest without
# spinning.
TICK_S = 20

# Ceilings. A recurring check is a model call each time, and "every minute
# until I say stop" is a request nobody means literally.
MIN_INTERVAL_S = 60
MAX_FIRES = 60
MAX_SPAN = timedelta(hours=24)
MAX_AHEAD = timedelta(days=90)


@dataclass
class Reminder:
    id: int
    kind: str
    text: str
    next_fire_at: str
    every_seconds: int
    until_at: str | None
    fires: int
    max_fires: int
    last_result: str | None
    status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Reminder:
        return cls(
            id=int(row["id"]), kind=row["kind"], text=row["text"],
            next_fire_at=row["next_fire_at"], every_seconds=int(row["every_seconds"]),
            until_at=row["until_at"], fires=int(row["fires"]),
            max_fires=int(row["max_fires"]), last_result=row["last_result"],
            status=row["status"],
        )

    @property
    def recurring(self) -> bool:
        return self.every_seconds > 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "next": self.next_fire_at,
            "every_minutes": round(self.every_seconds / 60) if self.recurring else None,
            "until": self.until_at,
            "fires": self.fires,
            "status": self.status,
        }

    def describe(self) -> str:
        when = _pretty(self.next_fire_at)
        if not self.recurring:
            return f"#{self.id} {self.text} — {when}"
        every = round(self.every_seconds / 60)
        return f"#{self.id} {self.text} — every {every} min until {_pretty(self.until_at)}"


class BadTime(ValueError):
    """The model produced a time that cannot be used."""


def _parse(value: str, *, field: str) -> datetime:
    """Read an ISO timestamp, and refuse anything that is not one.

    Naive datetimes are treated as local, which is what the model means when
    it writes "2026-09-20T17:00:00" after being told the local time.
    """
    text = (value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BadTime(
            f"{field} must be an ISO 8601 datetime like 2026-09-20T17:00:00, got {value!r}"
        ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _pretty(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return _parse(value, field="t").strftime("%a %d %b, %H:%M")
    except BadTime:
        return value


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


def schedule(
    conn: sqlite3.Connection,
    *,
    kind: str,
    text: str,
    when: str,
    every_minutes: int = 0,
    until: str = "",
    session_id: int | None = None,
    now: datetime | None = None,
) -> Reminder:
    """Book a reminder or a recurring check. Raises BadTime on nonsense."""
    now = now or datetime.now()
    if kind not in ("remind", "check"):
        raise BadTime(f"kind must be 'remind' or 'check', got {kind!r}")
    if not text.strip():
        raise BadTime("a reminder needs something to say")

    first = _parse(when, field="when")
    # A minute of slack: a model computing "in 5 minutes" from an injected
    # clock is often a few seconds behind by the time the call lands, and
    # refusing that is pedantic rather than safe.
    if first < now - timedelta(minutes=1):
        raise BadTime(f"{when} is in the past (it is now {now.isoformat(timespec='minutes')})")
    if first > now + MAX_AHEAD:
        raise BadTime(f"{when} is more than {MAX_AHEAD.days} days away")

    every_seconds = 0
    until_at: str | None = None
    max_fires = 1

    if every_minutes:
        every_seconds = max(MIN_INTERVAL_S, int(every_minutes) * 60)
        if not until.strip():
            raise BadTime("a repeating check needs an end time; say when to stop")
        end = _parse(until, field="until")
        if end <= first:
            raise BadTime("the end time is before the first check")
        if end - first > MAX_SPAN:
            raise BadTime(f"a repeating check can run for at most {MAX_SPAN}")
        until_at = end.isoformat(timespec="seconds")
        # +1 so the run that lands exactly on the end time still happens.
        max_fires = min(MAX_FIRES, int((end - first).total_seconds() // every_seconds) + 1)

    with db.transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO reminders
                (kind, text, session_id, next_fire_at, every_seconds, until_at, max_fires)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, text.strip(), session_id, first.isoformat(timespec="seconds"),
             every_seconds, until_at, max_fires),
        )
    return get(conn, int(cur.lastrowid))


def get(conn: sqlite3.Connection, reminder_id: int) -> Reminder | None:
    row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    return Reminder.from_row(row) if row else None


def active(conn: sqlite3.Connection, limit: int = 25) -> list[Reminder]:
    rows = conn.execute(
        "SELECT * FROM reminders WHERE status = 'active' ORDER BY next_fire_at LIMIT ?",
        (limit,),
    ).fetchall()
    return [Reminder.from_row(r) for r in rows]


RECENT_HOURS = 12


def recent(conn: sqlite3.Connection, hours: int = RECENT_HOURS, limit: int = 15) -> list[dict]:
    """What went off or was cancelled in the last `hours`, newest first, with
    when (local time).

    Listing only what is still scheduled once cost them six reminders. The mop
    and laundry reminders had just gone off, the user answered "both complete", and
    the list the model checked held neither -- only the week's gym and run
    reminders, which it took "both" to mean and cancelled. What has just gone
    off is what a reply like that is about.

    Both sides of the comparison are SQLite's clock: `finished_at` is written
    as `datetime('now')`.
    """
    from agent import sqltime

    rows = conn.execute(
        "SELECT * FROM reminders WHERE status != 'active' AND finished_at IS NOT NULL"
        "   AND finished_at >= datetime('now', ?) ORDER BY finished_at DESC, id DESC LIMIT ?",
        (f"-{int(hours)} hours", limit),
    ).fetchall()
    out = []
    for row in rows:
        at = sqltime.moment(row["finished_at"]).astimezone().replace(tzinfo=None)
        what = "went_off" if row["status"] == "done" else row["status"]
        out.append({**Reminder.from_row(row).as_dict(),
                    what: at.isoformat(timespec="minutes")})
    return out


def cancel(conn: sqlite3.Connection, reminder_id: int) -> bool:
    return cancel_many(conn, [reminder_id]) == 1


def cancel_many(conn: sqlite3.Connection, ids: list[int]) -> int:
    """Cancel several at once. Returns how many were actually active.

    One statement rather than a loop because "cancel everything" otherwise
    costs a tool call per reminder, and a turn has a budget of twenty -- so a
    busy week would fail halfway through and leave the rest running, which is
    the worst possible outcome for a cancel.
    """
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE reminders SET status = 'cancelled', finished_at = datetime('now')"
            f" WHERE status = 'active' AND id IN ({placeholders})",
            [int(i) for i in ids],
        )
    return cur.rowcount


def cancel_all(conn: sqlite3.Connection) -> int:
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE reminders SET status = 'cancelled', finished_at = datetime('now')"
            " WHERE status = 'active'"
        )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


def due(conn: sqlite3.Connection, now: datetime | None = None) -> list[Reminder]:
    now = now or datetime.now()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE status = 'active' AND next_fire_at <= ?"
        " ORDER BY next_fire_at",
        (now.isoformat(timespec="seconds"),),
    ).fetchall()
    return [Reminder.from_row(r) for r in rows]


def advance(
    conn: sqlite3.Connection, reminder: Reminder, *, result: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Record a firing and move the clock on. Returns whether it is finished.

    The next time is computed from the schedule rather than from now, so a
    check that fired late does not drift the whole series later with it.
    """
    now = now or datetime.now()
    fires = reminder.fires + 1
    finished = not reminder.recurring or fires >= reminder.max_fires

    next_at = reminder.next_fire_at
    if not finished:
        scheduled = _parse(reminder.next_fire_at, field="next") + timedelta(
            seconds=reminder.every_seconds
        )
        # If we fell far behind, skip ahead rather than firing a backlog.
        while scheduled <= now:
            scheduled += timedelta(seconds=reminder.every_seconds)
        if reminder.until_at and scheduled > _parse(reminder.until_at, field="until"):
            finished = True
        next_at = scheduled.isoformat(timespec="seconds")

    with db.transaction(conn):
        conn.execute(
            """
            UPDATE reminders
               SET fires = ?, next_fire_at = ?, last_result = COALESCE(?, last_result),
                   status = ?, finished_at = CASE WHEN ? THEN datetime('now') END
             WHERE id = ?
            """,
            (fires, next_at, result, "done" if finished else "active",
             finished, reminder.id),
        )
    return finished


def should_report(reminder: Reminder, answer: str, *, finished: bool) -> bool:
    """Is this answer worth sending?

    First reading always, because otherwise the first ten minutes are silent.
    A changed answer always, because that is the entire point. The last one
    always, so the series closes rather than trailing off. Everything in
    between only if it differs -- six messages saying 14-7 bury the one
    saying 21-7.
    """
    if reminder.fires == 0 or finished:
        return True
    return _normalise(answer) != _normalise(reminder.last_result or "")


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())
