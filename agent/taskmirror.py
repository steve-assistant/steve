"""The user's commitments, mirrored to a tasks list on their phone.

The design constraint is the whole of this module, so it goes first: **there
is one system of record and it is the obligations ledger**
(`tools/obligations.py`; the `commitments` table until 10 October 2026).
A tasks list is a surface on their phone, not a store. A commitment with a
due date is mirrored out; a task the user ticks is read back in, and one
closed here is ticked there. Two-way, one owner.

Without that discipline this becomes the third overlapping to-do list, and
the failure mode of three lists is silent: an item in the wrong one is never
surfaced, and the user finds out by missing it. That is worse than not having
the feature, because the user would have been relying on it.

Two consequences of the rule worth stating, because both look like
limitations and are not:

  - `add_task` does not create a task. It creates a commitment, which is
    mirrored at once (`mirror`). The task is the shadow.
  - Ticking a task closes the commitment, and that does not violate "never
    auto-close". The rule is against *inference* -- concluding from a later
    email that something was probably done. A tick is the user saying so.

Until step 5 this spoke to one service by name; it now speaks to the tasks
account (`agent/connectors.py`), and `obligations.task_id` holds the id the
slot handed out, which names the account as well as the task. Blocking
throughout: callers run it on a thread with its own connection.
"""

from __future__ import annotations

import logging
import sqlite3

from agent import connectors
from agent.connectors import Connected
from core import modules, writes
from steve_sdk import ids
from steve_sdk.classes import Task
from steve_sdk.errors import ConnectorError

log = logging.getLogger("taskmirror")

# Stored in `obligations.task_id` to mean "the user removed the mirror; do not put
# it back". Without it the nightly sync recreates a task the user deleted, every
# night, which is the kind of small insistence that gets a feature muted.
NO_MIRROR = "-"

# Per sync. A cap in code, like every other cap here.
MAX_MIRRORED = 25

# How far back a sync looks for items closed here whose task is still
# unticked: long enough to outlast a week offline, short enough that an old
# list is not re-read every hour.
TICK_BACK_DAYS = 7

# Every task on the list, for a sync: read whole, ticked or not.
EVERY_TASK = 10_000


def account(conn: sqlite3.Connection) -> Connected | None:
    """The tasks account commitments are mirrored to: the first there is."""
    found = connectors.accounts(conn, "tasks")
    return found[0] if found else None


def on_user_phone(commitment) -> bool:
    """Whether a commitment belongs on their tasks list.

    Anything the user asked for themselves, dated or not: "add it to my tasks app"
    that never reaches the app is the one failure the user notices. One found in
    their sent mail only if it has a date -- a tasks app sorts and nags by date,
    and a list of undated things someone else's email implied is noise.
    """
    return bool(commitment.due_date) or commitment.source == "conversation"


def mirrored(task_id: str | None) -> bool:
    """Whether an obligation's `task_id` names a task: set, and not NO_MIRROR."""
    return bool(task_id) and task_id != NO_MIRROR


def _push(conn: sqlite3.Connection, found: Connected, commitment) -> bool:
    """One commitment out to their phone, and the task's id kept against it."""
    from tools import obligations

    # A task the user dictated is its own quote; repeating it as the note is noise.
    quoted = commitment.quote and commitment.quote.strip() != commitment.text.strip()
    task = Task(native_id="", title=commitment.text,
                notes=f"“{commitment.quote}”" if quoted else "", due=commitment.due_date)
    made = connectors.blocking(found, lambda p: writes.create(p, task))
    if not made.native_id:
        return False
    obligations.set_task(conn, commitment.id, found.id(made.native_id))
    return True


def mirror(conn: sqlite3.Connection, commitment_id: int) -> dict:
    """Put one commitment on their phone now.

    For `add_task`. Waiting for the next sync meant a task asked for at 9pm
    appeared at 3am, and the user checked the app in between and found nothing
    (25 September). If the account cannot be reached, the next sync tries again.
    """
    from tools import commitments

    commitment = commitments.get(conn, int(commitment_id))
    if commitment is None or commitment.status != "open":
        return {"on_phone": False, "error": "no open commitment with that id"}
    if mirrored(commitment.task_id):
        return {"on_phone": True}
    if not on_user_phone(commitment) or commitment.task_id == NO_MIRROR:
        return {"on_phone": False}
    found = account(conn)
    if found is None:
        return {"on_phone": False, "error": "no tasks account is connected"}
    try:
        return {"on_phone": _push(conn, found, commitment)}
    except Exception as exc:  # noqa: BLE001 - the next sync retries
        log.exception("could not mirror commitment %s", commitment_id)
        return {"on_phone": False, "error": type(exc).__name__}


def complete(conn: sqlite3.Connection, task_id: str) -> None:
    """Tick the task `task_id` names, on the account it came from: exactly
    that task, found again by its id (`core/writes.modify`). Raises NoAccount
    or the account's error."""
    found, native = connectors.by_id(conn, task_id)
    connectors.blocking(found, lambda p: writes.modify(p, native, p.complete))


async def tick(conn: sqlite3.Connection, task_id: str) -> bool:
    """`complete`, off the event loop on a connection of its own. False, and
    logged, if it could not: the hourly sync ticks it then."""
    from agent import offloop

    try:
        await offloop.with_own_conn(conn, complete, task_id)
    except Exception:  # noqa: BLE001 - the hourly sync ticks it
        log.warning("could not tick task %s; the sync will", task_id, exc_info=True)
        return False
    return True


def open_tasks(conn: sqlite3.Connection, limit: int = 25) -> dict:
    """Everything outstanding on the list commitments go to."""
    found = account(conn)
    if found is None:
        return {"error": "no tasks account is connected", "kind": "not_connected"}
    try:
        tasks = connectors.blocking(found, lambda p: p.tasks(open_only=True, limit=limit))
    except ConnectorError as exc:
        return {"error": connectors.say(exc, found.name), "kind": exc.kind}
    return {"tasks": [{"id": found.id(t.native_id), "title": t.title, "due": t.due or ""}
                      for t in tasks]}


async def sync(conn: sqlite3.Connection) -> dict:
    """`reconcile`, off the event loop on a connection of its own."""
    from agent import offloop

    return await offloop.with_own_conn(conn, reconcile)


def reconcile(conn: sqlite3.Connection) -> dict:
    """Push commitments out, read ticked tasks back in.

    Blocking, and run by callers on a thread with its own connection
    (`sync`, or the attention loop's hourly source). The first version handed
    an async function's calls to threads and kept SQLite on the loop;
    before that, the whole thing went to `asyncio.to_thread` on the caller's
    connection and raised `ProgrammingError` on its first query, every time
    -- which the caller caught and logged, so the mirror simply never ran and
    nothing said so. `offloop.with_own_conn` is the shared answer to both.

    Pull before push, deliberately. Doing it the other way would create a
    task and then immediately read the list back without it, which is
    harmless but makes the counts in the report lie.
    """
    from tools import commitments, obligations

    found = account(conn)
    if found is None:
        return {"mirrored": 0, "closed": 0, "error": "no tasks account is connected"}
    try:
        # Every task, ticked or not, in one read rather than one per
        # commitment: a read per row turned a sync of twenty into twenty
        # round trips.
        every = connectors.blocking(found, lambda p: p.tasks(open_only=False,
                                                             limit=EVERY_TASK))
    except Exception as exc:  # noqa: BLE001 - opening the list can fail too
        log.exception("could not read the task list")
        return {"mirrored": 0, "closed": 0, "error": type(exc).__name__}
    done = {found.id(t.native_id): t.done for t in every}
    slot = f"tasks:{found.key}"

    closed = 0
    for row in conn.execute(
        "SELECT id, task_id FROM obligations WHERE kind IN ('commitment', 'task')"
        " AND status IN ('open', 'asked', 'waiting') AND task_id IS NOT NULL AND task_id <> ?"
        f" AND {modules.live(conn, 'obligations')}",
        (NO_MIRROR,),
    ).fetchall():
        if connectors.slot_of(row["task_id"]) != slot:
            continue  # another account's, or none Steve handed out
        state = done.get(row["task_id"])
        if state:
            # The user ticked it. Not an inference -- them saying so.
            if commitments.resolve(conn, int(row["id"]), by="tasks tick"):
                closed += 1
        elif state is None:
            # Gone from the list. Leave the commitment alone, because a
            # deleted reminder is not a finished obligation, but stop
            # mirroring it -- recreating a task the user removed, nightly, is how
            # a helpful feature becomes a nuisance.
            obligations.set_task(conn, int(row["id"]), NO_MIRROR)

    # The other direction: closed here, still unticked on their phone. A
    # commitment closed in conversation (`commitment_done`) left its task
    # showing until the user ticked it themselves, and they had already said it was
    # done (the scouts' audit, 10 October 2026).
    ticked = 0
    for row in conn.execute(
        "SELECT id, task_id FROM obligations WHERE kind IN ('commitment', 'task')"
        " AND status IN ('done', 'dropped') AND task_id IS NOT NULL AND task_id <> ?"
        f" AND closed_at >= datetime('now', ?) AND {modules.live(conn, 'obligations')}",
        (NO_MIRROR, f"-{TICK_BACK_DAYS} days"),
    ).fetchall():
        if done.get(row["task_id"]) is not False:
            continue
        native = ids.parse(row["task_id"]).native
        try:
            connectors.blocking(found, lambda p, native=native: writes.modify(
                p, native, p.complete))
            ticked += 1
        except Exception:  # noqa: BLE001 - the next sync tries again
            log.exception("could not tick the task for obligation %s", row["id"])

    pushed = 0
    for commitment in commitments.open_ones(conn, limit=MAX_MIRRORED * 2):
        if pushed >= MAX_MIRRORED:
            break
        if commitment.task_id or not on_user_phone(commitment):
            continue
        try:
            if _push(conn, found, commitment):
                pushed += 1
        except Exception:  # noqa: BLE001 - one bad create is not the sync
            log.exception("could not mirror commitment %s", commitment.id)

    return {"mirrored": pushed, "closed": closed, "ticked": ticked, "error": None}
