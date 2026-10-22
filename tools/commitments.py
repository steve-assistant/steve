"""Things the user said they would do.

Two rules shape this module, and both are about the same asymmetry: a missed
commitment is an annoyance, an invented one is a betrayal. The user acts on it,
finds there was nothing there, and never trusts the feature again.

**Every row carries the sentence it came from.** Not a paraphrase -- the
verbatim span, checked against the source by code in `tools/extract.py`
before it ever reaches this module. A commitment that cannot show its
working does not get stored.

**Nothing is ever closed by inference.** A candidate can be surfaced -- "this
looks done, was it?" -- and the user answers. Auto-closing something the user did not do
fails silently, which is the worst way for this to fail, because the failure
is indistinguishable from success right up until the deadline.

Since 10 October 2026 the rows live in the obligations ledger
(`tools/obligations.py`, kinds `commitment` and `task`), which keeps each
one's number; this module is the view the night, the briefing and the people
tools already read. "Their saying so" now includes their words in conversation
("Solve is done", matched by code to one item: `agent/closing.py`), which is
them saying so, not an inference from their mail.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

# What "before they're late" means. Wide enough to be useful, narrow enough
# that the morning digest is not a standing list of everything outstanding --
# "here are your 14 open commitments" every day is how this gets muted.
SOON_DAYS = 3

# Per digest. A cap in code, for the same reason as every other cap here.
MAX_IN_DIGEST = 3


@dataclass
class Commitment:
    id: int
    text: str
    quote: str
    due_date: str | None
    status: str
    person: str | None
    person_id: int | None
    source: str
    source_id: str | None
    # The Google Task mirroring this one, if any. See `modules/google_cloud/tasks.py`
    # -- the task is the shadow, this row is the record.
    task_id: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Commitment:
        keys = row.keys()
        return cls(
            id=int(row["id"]), text=row["text"], quote=row["quote"],
            due_date=row["due_date"], status=_status(row["status"]),
            person=row["person"] if "person" in keys else None,
            person_id=row["person_id"], source=row["source"],
            source_id=row["source_id"],
            task_id=row["task_id"] if "task_id" in keys else None,
        )

    @property
    def overdue(self) -> bool:
        return bool(self.due_date) and self.due_date < date.today().isoformat()

    def as_dict(self) -> dict:
        return {
            "id": self.id, "text": self.text, "quote": self.quote,
            "due_date": self.due_date, "status": self.status,
            "person": self.person, "overdue": self.overdue,
            "source": self.source,
        }

    def line(self) -> str:
        """One line, for a digest or a chat reply."""
        when = ""
        if self.due_date:
            when = " (overdue)" if self.overdue else f" — due {self.due_date}"
        who = f" · {self.person}" if self.person else ""
        return f"{self.text}{when}{who}"


# The ledger's kinds that are theirs to do: what the user committed to in mail, and
# what they asked to be tracked.
KINDS = ("commitment", "task")

_SELECT = """
SELECT c.*, p.name AS person
  FROM obligations c LEFT JOIN people p ON p.id = c.person_id
 WHERE c.kind IN ('commitment', 'task')
"""
_OPEN = "c.status IN ('open', 'asked', 'waiting')"


def _live(conn: sqlite3.Connection) -> str:
    """Not one kept from an account that is off (`core/modules.live`)."""
    from core import modules

    return modules.live(conn, "obligations", "c")


def _status(row_status: str) -> str:
    """The three words callers of this module know."""
    if row_status in ("open", "asked", "waiting"):
        return "open"
    return "done" if row_status == "done" else "dropped"


def add(
    conn: sqlite3.Connection,
    *,
    text: str,
    quote: str,
    due_date: str | None = None,
    person_id: int | None = None,
    source: str = "sent_mail",
    source_id: str | None = None,
) -> int | None:
    """Store one. None if it was already stored.

    `quote` is required by the signature, not merely expected, because the
    only thing standing between this table and a confidently fabricated
    obligation is that every row can point at the words it came from.
    """
    if not (text or "").strip() or not (quote or "").strip():
        return None
    from tools import obligations

    # Deduplicated on the *quote*, not the text (`obligations.add`). Nightly
    # windows overlap by three days, so the same sent message is read on
    # several nights, and the model phrases its summary differently each
    # time -- "Send the doctor's note" one night and "Send Zoe the
    # doctor's note" the next. The quote is copied verbatim and does not drift.
    return obligations.add(
        conn, kind="task" if source == "conversation" else "commitment",
        text=text.strip()[:300], quote=quote.strip()[:600], due_date=due_date,
        person_id=person_id, source=source, source_id=source_id,
    )


def get(conn: sqlite3.Connection, commitment_id: int) -> Commitment | None:
    row = conn.execute(f"{_SELECT} AND c.id = ?", (commitment_id,)).fetchone()
    return Commitment.from_row(row) if row else None


def open_ones(conn: sqlite3.Connection, limit: int = 25) -> list[Commitment]:
    """Everything outstanding, soonest first, undated last."""
    rows = conn.execute(
        f"{_SELECT} AND {_OPEN} AND {_live(conn)}"
        " ORDER BY c.due_date IS NULL, c.due_date, c.id LIMIT ?",
        (limit,),
    ).fetchall()
    return [Commitment.from_row(r) for r in rows]


def due_soon(
    conn: sqlite3.Connection, days: int = SOON_DAYS, limit: int = MAX_IN_DIGEST
) -> list[Commitment]:
    """What is about to be late, including what already is."""
    horizon = (date.today() + timedelta(days=days)).isoformat()
    rows = conn.execute(
        f"{_SELECT} AND {_OPEN} AND c.due_date IS NOT NULL AND {_live(conn)}"
        " AND c.due_date <= ? ORDER BY c.due_date LIMIT ?",
        (horizon, limit),
    ).fetchall()
    return [Commitment.from_row(r) for r in rows]


def for_person(conn: sqlite3.Connection, person_id: int) -> list[Commitment]:
    rows = conn.execute(
        f"{_SELECT} AND c.person_id = ? AND {_OPEN} AND {_live(conn)}"
        " ORDER BY c.due_date IS NULL, c.due_date",
        (person_id,),
    ).fetchall()
    return [Commitment.from_row(r) for r in rows]


def resolve(conn: sqlite3.Connection, commitment_id: int, *, status: str = "done",
            by: str = "user words") -> bool:
    """Close one, because the user said so -- in words, by a tick in Google
    Tasks, or through a tool they asked to be used. `by` says which.

    Deliberately has no inference path. Nothing anywhere calls this on a
    guess -- a candidate is surfaced and the user answers, which is the difference
    between a list the user trusts and a list that quietly loses things.
    """
    if status not in ("done", "dropped"):
        raise ValueError(f"status must be done or dropped, got {status!r}")
    from tools import obligations

    if get(conn, commitment_id) is None:
        return False
    return obligations.close(conn, commitment_id, by=by, status=status) is not None


def counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(status IN ('open', 'asked', 'waiting')) AS open,"
        " SUM(status = 'done') AS done FROM obligations WHERE kind IN ('commitment', 'task')"
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "open": int(row["open"] or 0),
        "done": int(row["done"] or 0),
    }
