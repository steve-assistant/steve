"""The commitment store, for executors that run outside a request.

Until step 5's item 4f this also handed out Google's calendar client, for a
note in every turn on whether the calendar's display zone was the user's.
The calendar tools give every time in the user's zone, offset and all, so
the note told the model what the times already said; it cost a Google call
in each process, and named a provider in the turn's code.
"""

from __future__ import annotations


def commitment_store():
    """The database, for an executor that runs outside a request.

    Executors are module-level and take no context, so they reach for the
    worker's connection. That is None whenever the worker is not what is
    running -- the `python -m agent.attention` dry run, a test, the night
    window -- and the failure was an `AttributeError` on NoneType several
    frames down, which reads as a bug in the commitment store rather than a
    missing server.
    """
    from agent import runtime

    if runtime.conn is not None:
        return runtime.conn, False
    from core import db

    return db.open_store(), True
