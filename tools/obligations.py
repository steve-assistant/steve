"""Everything open that has no other home, and how each one closed.

Until 10 October 2026 nothing closed anything. The user wrote "Solve is done"
at 21:00 and again at 22:49 on 8 October, and the 07:40 briefing still said it
was due today; commitment 5 closed at 09:33 the next morning, by hand. A
thank-you the user asked for was drafted, and drafted again eight hours later
from the conversation's history, to the wrong address, because no record said
the first had run. Each of those was a model reading old text and deciding,
again, whether something was still owed. Here code keeps that state, and the
model reads it.

One row per thing owed: their commitments and the tasks the user dictates (the
`commitments` table carried over, with their numbers), and what they asked for
while the laptop was away (`ask`). Each says where it came from
(`source_ref`), whether doing it would reach someone (`stakes`), and, once
closed, what closed it (`closed_by`) and what came of it (`outcome`).
Nothing is deleted: a closed row is the record that it was done, which is
what the next turn needs to know before it does it again.

Reminders and cards are not copied here. Each has its own table that
already knows when it went off or was answered, and a copy would be one more
store to disagree with the rest; `agent/closing.py` reads across all three.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from core import db, modules

# Statuses that are still owed. `asked`: the user has a card about it and has not
# answered. `waiting`: on someone else.
OPEN = ("open", "asked", "waiting")
CLOSED = ("done", "dropped", "expired")
KINDS = ("commitment", "task", "ask", "question", "follow_up")

# Doing it would reach someone or spend: their rule (10 October 2026) is that an
# old request like that is asked about, never just done. Read from the words
# of the request, so it is a hint for the turn, not a gate -- the gate is
# still the card every outward tool raises.
_OUTWARD = re.compile(
    r"\b(?:send|email|e-mail|mail|reply|respond|write (?:to|back)|text|message|call|ring|"
    r"post|tweet|book|buy|order|pay|purchase|register|sign (?:me )?up|rsvp|invite|"
    r"draft (?:a |an |the )?(?:note|email|reply|message|thank))",
    re.IGNORECASE,
)


def stakes_of(text: str) -> str:
    return "outward" if _OUTWARD.search(text or "") else "low"


@dataclass
class Obligation:
    id: int
    kind: str
    text: str
    source: str
    status: str
    quote: str | None = None
    source_id: str | None = None
    source_ref: str | None = None
    person_id: int | None = None
    person: str | None = None
    stakes: str = "low"
    due_date: str | None = None
    outcome: str | None = None
    closed_at: str | None = None
    closed_by: str | None = None
    task_id: str | None = None
    detail: dict = field(default_factory=dict)
    action_id: int | None = None
    asked_at: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Obligation:
        keys = row.keys()
        try:
            detail = json.loads(row["detail"] or "{}")
        except ValueError:
            detail = {}
        return cls(
            id=int(row["id"]), kind=row["kind"], text=row["text"], source=row["source"],
            status=row["status"], quote=row["quote"], source_id=row["source_id"],
            source_ref=row["source_ref"], person_id=row["person_id"],
            person=row["person"] if "person" in keys else None,
            stakes=row["stakes"], due_date=row["due_date"], outcome=row["outcome"],
            closed_at=row["closed_at"], closed_by=row["closed_by"], task_id=row["task_id"],
            detail=detail if isinstance(detail, dict) else {},
            action_id=row["action_id"], asked_at=row["asked_at"], created_at=row["created_at"],
        )

    @property
    def is_open(self) -> bool:
        return self.status in OPEN

    @property
    def overdue(self) -> bool:
        return self.is_open and bool(self.due_date) and self.due_date < date.today().isoformat()


_SELECT = """
SELECT o.*, p.name AS person
  FROM obligations o LEFT JOIN people p ON p.id = o.person_id
"""


def add(
    conn: sqlite3.Connection,
    *,
    kind: str,
    text: str,
    source: str,
    quote: str | None = None,
    source_id: str | None = None,
    source_ref: str | None = None,
    person_id: int | None = None,
    stakes: str | None = None,
    due_date: str | None = None,
    detail: dict | None = None,
    created_at: str | None = None,
) -> int | None:
    """Keep one. None when it is already kept: the same `source_ref`, or the
    same words from the same source message (commitments' rule -- the night
    reads overlapping windows of sent mail and rephrases each time, so the
    verbatim quote is what does not drift)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    text = " ".join((text or "").split())
    if not text:
        return None
    quote = (quote or "").strip()[:600] or None
    if source_ref and by_ref(conn, source_ref) is not None:
        return None
    if source_id:
        twin = conn.execute(
            "SELECT id FROM obligations WHERE source_id = ? AND (quote = ? OR text = ?)",
            (source_id, quote, text[:300])).fetchone()
        if twin:
            return None
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO obligations (kind, text, quote, source, source_id, source_ref,"
            " person_id, stakes, due_date, detail, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')),"
            " datetime('now'))",
            (kind, text[:300], quote, source, source_id, source_ref, person_id,
             stakes or stakes_of(text), due_date,
             json.dumps(detail, default=str) if detail else None, created_at))
    return int(cur.lastrowid) if cur.rowcount else None


def get(conn: sqlite3.Connection, obligation_id: int) -> Obligation | None:
    row = conn.execute(f"{_SELECT} WHERE o.id = ?", (int(obligation_id),)).fetchone()
    return Obligation.from_row(row) if row else None


def by_ref(conn: sqlite3.Connection, source_ref: str) -> Obligation | None:
    row = conn.execute(f"{_SELECT} WHERE o.source_ref = ?", (source_ref,)).fetchone()
    return Obligation.from_row(row) if row else None


def by_action(conn: sqlite3.Connection, action_id: int) -> Obligation | None:
    """The item a card asks them about."""
    row = conn.execute(f"{_SELECT} WHERE o.action_id = ?", (int(action_id),)).fetchone()
    return Obligation.from_row(row) if row else None


def _marks(values) -> str:
    return ", ".join("?" * len(values))


def _live(conn: sqlite3.Connection) -> str:
    """Not one kept from an account that is off (`core/modules.live`)."""
    return modules.live(conn, "obligations", "o")


def open_ones(conn: sqlite3.Connection, *, kinds: tuple[str, ...] = KINDS,
              limit: int = 25) -> list[Obligation]:
    """Everything still owed, soonest first, undated last."""
    rows = conn.execute(
        f"{_SELECT} WHERE o.status IN ({_marks(OPEN)}) AND o.kind IN ({_marks(kinds)})"
        f" AND {_live(conn)} ORDER BY o.due_date IS NULL, o.due_date, o.id LIMIT ?",
        (*OPEN, *kinds, limit)).fetchall()
    return [Obligation.from_row(r) for r in rows]


def closed_since(conn: sqlite3.Connection, hours: float = 24, *,
                 kinds: tuple[str, ...] = KINDS, limit: int = 10) -> list[Obligation]:
    """What closed lately, newest first: the next turn's proof that a thing
    is done, before it does it again."""
    rows = conn.execute(
        f"{_SELECT} WHERE o.status IN ({_marks(CLOSED)}) AND o.kind IN ({_marks(kinds)})"
        " AND o.closed_at >= datetime('now', ?)"
        f" AND o.closed_at <= datetime('now') AND {_live(conn)}"
        " ORDER BY o.closed_at DESC, o.id DESC LIMIT ?",
        (*CLOSED, *kinds, f"-{float(hours)} hours", limit)).fetchall()
    return [Obligation.from_row(r) for r in rows]


def close(conn: sqlite3.Connection, obligation_id: int, *, by: str, status: str = "done",
          outcome: str | None = None) -> Obligation | None:
    """Close one that is still open, saying what closed it. Returns the row
    as closed, or None if it was not open. The Google Tasks mirror is ticked
    by whoever closes it (`closing.untrack_soon`) or, failing that, by the
    hourly sync (`taskmirror.reconcile`)."""
    if status not in CLOSED:
        raise ValueError(f"status must be one of {CLOSED}, got {status!r}")
    with db.transaction(conn):
        cur = conn.execute(
            f"UPDATE obligations SET status = ?, closed_by = ?, outcome = COALESCE(?, outcome),"
            " closed_at = datetime('now'), updated_at = datetime('now')"
            f" WHERE id = ? AND status IN ({_marks(OPEN)})",
            (status, by, (outcome or "").strip()[:300] or None, int(obligation_id), *OPEN))
    return get(conn, obligation_id) if cur.rowcount else None


def reopen(conn: sqlite3.Connection, obligation_id: int, *, outcome: str | None = None) -> bool:
    """Open again: the user said it was not done after all, or kept it when asked."""
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE obligations SET status = 'open', closed_at = NULL, closed_by = NULL,"
            " outcome = COALESCE(?, outcome), updated_at = datetime('now') WHERE id = ?",
            ((outcome or "").strip()[:300] or None, int(obligation_id)))
    return cur.rowcount > 0


def note(conn: sqlite3.Connection, obligation_id: int, *, outcome: str) -> None:
    """What came of an attempt that did not close it -- a run that failed,
    an offer made -- so the next look sees it was tried."""
    with db.transaction(conn):
        conn.execute(
            "UPDATE obligations SET outcome = ?, updated_at = datetime('now') WHERE id = ?",
            ((outcome or "").strip()[:300], int(obligation_id)))


def asking(conn: sqlite3.Connection, obligation_id: int, action_id: int) -> None:
    """The user has a card about it now."""
    with db.transaction(conn):
        conn.execute(
            "UPDATE obligations SET status = 'asked', action_id = ?, asked_at = datetime('now'),"
            " updated_at = datetime('now') WHERE id = ?", (int(action_id), int(obligation_id)))


def set_task(conn: sqlite3.Connection, obligation_id: int, task_id: str) -> None:
    with db.transaction(conn):
        conn.execute("UPDATE obligations SET task_id = ?, updated_at = datetime('now')"
                     " WHERE id = ?", (task_id, int(obligation_id)))


def for_person(conn: sqlite3.Connection, person_id: int) -> list[Obligation]:
    rows = conn.execute(
        f"{_SELECT} WHERE o.person_id = ? AND o.status IN ({_marks(OPEN)})"
        f" AND {_live(conn)} ORDER BY o.due_date IS NULL, o.due_date",
        (person_id, *OPEN)).fetchall()
    return [Obligation.from_row(r) for r in rows]
