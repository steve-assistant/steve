"""What the user owes: commitments that are about to be late.

The commitments ledger (`tools/commitments.py`) is filled overnight from
their sent mail -- "I'll send it by Friday" -- and until now surfaced only as
three lines in the morning digest. As a source, a commitment coming due can
be prepared for (a reminder the evening before, a draft of the thing
promised) and not merely listed.

Keyed by the commitment and its due date, so moving the date is a new
mention and an unchanged one is not.
"""

from __future__ import annotations

import logging
import sqlite3

from agent.attention.store import Observation

log = logging.getLogger("attention")

# How far ahead "about to be late" reaches, in days.
DUE_WITHIN_DAYS = 3


def commitments(conn: sqlite3.Connection) -> list[Observation]:
    from agent import taskmirror
    from tools import commitments as ledger

    # What the user ticked on their tasks list first, so a commitment they have done is
    # not raised as coming due. Nightly until 25 September: "wish Hazel
    # happy birthday" was ticked in the morning and stayed open until 3am.
    # Blocking is fine here; sources run on a thread with their own
    # connection. An outage costs the tick, not the observations.
    try:
        taskmirror.reconcile(conn)
    except Exception:  # noqa: BLE001
        log.exception("could not sync the tasks list")

    out = []
    for item in ledger.due_soon(conn, days=DUE_WITHIN_DAYS, limit=15):
        out.append(
            Observation(
                source="commitments",
                key=f"commitment:{item.id}:{item.due_date}",
                title=f"The user committed to: {item.text}",
                detail="; ".join(
                    part for part in (
                        "overdue" if item.overdue else f"due {item.due_date}",
                        f"to {item.person}" if item.person else "",
                    ) if part
                ),
                when=item.due_date,
                ref={"commitment_id": item.id},
            )
        )
    return out
