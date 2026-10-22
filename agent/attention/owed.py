"""What the user is owed that is already closed, for the checks before anything is said.

"Solve is done" at 01:00 on 9 October closed nothing, and the morning
briefing six and a half hours later still said "Tailspin Solve is due
today": the observation raised from commitment 5 was held for the briefing,
and nothing looked at the commitment again before saying it. `vet.closed`
asks this.

Read from the obligations ledger (`tools/obligations.py`), which holds the
commitments with their numbers: an observation raised from commitment 5
carries `commitment_id` 5, and the ledger's row 5 is it.
"""

from __future__ import annotations

import sqlite3

from tools.obligations import CLOSED


def closed_refs(conn: sqlite3.Connection) -> set[str]:
    """`commitment:<id>` for every commitment or task that is done, dropped
    or expired."""
    marks = ",".join("?" * len(CLOSED))
    rows = conn.execute(
        f"SELECT id FROM obligations WHERE kind IN ('commitment', 'task')"
        f" AND status IN ({marks})", CLOSED)
    return {f"commitment:{row[0]}" for row in rows}
