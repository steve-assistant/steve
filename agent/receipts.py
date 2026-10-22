"""Whether an action did what it said: the state read back after it ran.

The verifier (agent/verify.py) catches a reply claiming what no tool did. It
cannot catch a tool that ran and was wrong: on 25 September 2026 three tasks
were "added to your Tasks" and were not on their phone when the user looked. So after
an executor reports success, the thing it says it made is read back from the
service that holds it, and compared with what was asked:

- `verified_success` -- it is there, as asked;
- `verification_failed` -- read back, and it is not (missing, or different);
  the action then reports a failure, so neither the model nor the message
  announcing a gated action can call it done (`approvals.reported_failure`);
- `tool_reported_success` -- not checked: no check exists for the tool, or
  the read-back itself failed. Says nothing either way, as before receipts.

The external review of 1 October 2026 (docs/security-model.md) asked for
this: "the model should not be the authoritative source for those statuses."
The read-back is code; the model only sees its answer.

Checks exist where their data lives and a read-back is one call: the
calendar, the tasks list and mail, on every account, each read back through
the account its id names (`agent/connectors.py`). A sent message is checked
where the account hands one back. Each check runs in a thread, once retried
on "not found" for a service slow to show what it just made, and is bounded
in time; a check that cannot finish leaves the action as the tool reported
it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from agent import sqltime

log = logging.getLogger(__name__)

VERIFIED = "verified_success"
FAILED = "verification_failed"
UNCHECKED = "tool_reported_success"

# A read-back is one API call; this bounds a slow one so it cannot hold the
# turn, and the retry gives a service a moment to show what it just made.
CHECK_TIMEOUT_S = 15.0
RETRY_AFTER_S = 1.5

@dataclass
class Receipt:
    tool: str
    status: str
    target: str = ""
    observed: dict = field(default_factory=dict)
    why: str = ""
    checked_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class NotThere(Exception):
    """The read-back found nothing where the action said it put something."""


# tool -> check(args, result) -> (target, observed); raises NotThere, or
# ValueError when what is there is not what was asked.
CHECKS: dict[str, Callable[[dict, dict], tuple[str, dict]]] = {}


def postcondition(tool: str):
    def register(fn):
        CHECKS[tool] = fn
        return fn

    return register


def _item(item_id: str, fetch: Callable[[Any, str], Any] | None = None):
    """The account an id came from, and the item as it is there now (None
    if it is gone), read through the account itself: so a draft made in
    Outlook is read back from Outlook, and an event from the calendar it was
    put on. Each account's own check, or none."""
    from agent import connectors
    from core import db
    from steve_sdk.errors import ConnectorError

    conn = db.open_store()
    try:
        found, native = connectors.by_id(conn, item_id)
    except connectors.NoAccount as exc:
        raise RuntimeError(str(exc)) from None
    finally:
        conn.close()
    read = fetch or (lambda provider, native: provider.fetch(native))
    try:
        return found, connectors.blocking(found, lambda p: read(p, native))
    except ConnectorError as exc:
        raise RuntimeError(connectors.say(exc, found.name)) from None


def _same_moment(a: dt.datetime | dt.date | str | None, b: str | None) -> bool:
    """A time as the calendar holds it and one as the action asked for it,
    compared as moments: a bare "2026-10-05T14:00" is local, as the
    calendar tools read it."""
    if not a or not b:
        return not a and not b
    from agent import zones

    def moment(value) -> dt.datetime | dt.date:
        parsed = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(parsed, dt.datetime) and not parsed.tzinfo:
            return parsed.replace(tzinfo=zones.local_zone())
        return parsed

    try:
        return moment(a) == moment(b)
    except ValueError:
        return str(a) == str(b)


def _when(moment) -> str | None:
    """A calendar time as the listings show it (`agent/calendars.py`)."""
    from agent import calendars, zones

    return calendars._text(moment, zones.local_zone())


# --- the calendar, on any account ----------------------------------------------


@postcondition("create_event")
def _event_created(args: dict, result: dict) -> tuple[str, dict]:
    event_id = result.get("id")
    if not event_id:
        raise RuntimeError("no event id to read back")
    _, found = _item(event_id)
    if found is None:
        raise NotThere("the event is not on the calendar")
    if found.title != args.get("summary"):
        raise ValueError(f"it is titled {found.title!r}, not {args.get('summary')!r}")
    if not found.all_day and not _same_moment(found.start, args.get("start")):
        raise ValueError(f"it starts {_when(found.start)}, not {args.get('start')}")
    return event_id, {"summary": found.title, "start": _when(found.start),
                      "end": _when(found.end)}


@postcondition("update_event")
def _event_updated(args: dict, result: dict) -> tuple[str, dict]:
    event_id = args.get("event_id") or result.get("id")
    _, found = _item(event_id)
    if found is None:
        raise NotThere("the event is not on the calendar")
    for name, have in (("summary", found.title), ("location", found.location)):
        if args.get(name) is not None and have != args[name]:
            raise ValueError(f"its {name} is {have!r}, not {args[name]!r}")
    for name, have in (("start", found.start), ("end", found.end)):
        if args.get(name) and not _same_moment(have, args[name]):
            raise ValueError(f"its {name} is {_when(have)}, not {args[name]}")
    return event_id, {"summary": found.title, "start": _when(found.start),
                      "end": _when(found.end), "location": found.location or None}


@postcondition("delete_event")
def _event_deleted(args: dict, result: dict) -> tuple[str, dict]:
    event_id = args.get("event_id") or ""
    # A calendar that keeps a deleted event a while, marked cancelled, gives
    # none for it (`CalendarProvider.fetch`).
    _, found = _item(event_id)
    if found is None:
        return event_id, {"exists": False}
    raise ValueError("the event is still on the user's calendar")


# --- mail, on any account ----------------------------------------------------


def _mail(item_id: str):
    return _item(item_id, lambda provider, native: provider.fetch(native, body_chars=200))


@postcondition("send_email")
@postcondition("send_draft")
def _mail_sent(args: dict, result: dict) -> tuple[str, dict]:
    sent_id = result.get("id")
    if not sent_id:
        # Outlook hands a sent message to its outbox, not back to us.
        raise RuntimeError("the account gave no id to read the sent message by")
    _, sent = _mail(sent_id)
    if sent is None:
        raise NotThere("the sent message is not there")
    if "SENT" not in sent.labels and sent.direction != "out":
        raise ValueError("it is not in Sent")
    return sent_id, {"labels": list(sent.labels)} if sent.labels else {"sent": True}


@postcondition("draft_email")
def _draft_made(args: dict, result: dict) -> tuple[str, dict]:
    draft_id = result.get("draft_id")
    if not draft_id:
        raise RuntimeError("no draft id to read back")
    if _mail(draft_id)[1] is None:
        raise NotThere("the draft is not there")
    return draft_id, {"exists": True}


@postcondition("delete_draft")
def _draft_gone(args: dict, result: dict) -> tuple[str, dict]:
    draft_id = args.get("draft_id") or ""
    if _mail(draft_id)[1] is None:
        return draft_id, {"exists": False}
    raise ValueError("the draft is still there")


@postcondition("modify_email")
def _mail_modified(args: dict, result: dict) -> tuple[str, dict]:
    _, found = _mail(args["message_id"])
    if found is None:
        raise NotThere("the message is not there")
    # A star reads back as the slot's "STARRED": Gmail's own label, or on
    # IMAP the \Flagged flag (`core/engines/imap.py`), never a label so named.
    labels = set(found.labels)
    wrong = [what for what, ok in (
        ("still in the inbox", not args.get("archive") or "INBOX" not in labels),
        ("still unread", not args.get("mark_read") or not found.unread),
        ("not starred", not args.get("star") or "STARRED" in labels)) if not ok]
    if wrong:
        raise ValueError("it is " + " and ".join(wrong))
    return args["message_id"], {"labels": list(found.labels)}


# --- the tasks list, on any account --------------------------------------------


def _task_of(commitment_id) -> str | None:
    from agent import taskmirror
    from core import db
    from tools import commitments

    conn = db.open_store()
    try:
        found = commitments.get(conn, int(commitment_id))
    finally:
        conn.close()
    task_id = found.task_id if found else None
    return task_id if taskmirror.mirrored(task_id) else None


def _status(task) -> str:
    return "completed" if task.done else "open"


@postcondition("add_task")
def _task_added(args: dict, result: dict) -> tuple[str, dict]:
    """Only a task said to be on their phone is read back: one the result
    already says is not there yet is reported truthfully as it is."""
    if not result.get("on_phone") or not result.get("commitment_id"):
        raise RuntimeError("not said to be on the user's phone")
    task_id = _task_of(result["commitment_id"])
    if task_id is None:
        raise NotThere("the commitment has no task on their list")
    _, found = _item(task_id)
    if found is None:
        raise NotThere("the task was deleted")
    return task_id, {"title": found.title, "status": _status(found)}


@postcondition("complete_task")
def _task_completed(args: dict, result: dict) -> tuple[str, dict]:
    task_id = _task_of(args.get("commitment_id"))
    if task_id is None:
        raise RuntimeError("no task on their list to read back")
    _, found = _item(task_id)
    if found is None:
        raise NotThere("the task is gone from their list")
    if not found.done:
        raise ValueError("it is open on the user's phone, not completed")
    return task_id, {"status": _status(found)}


# --- running a check ----------------------------------------------------------


def check(tool: str, args: dict, result: dict) -> Receipt | None:
    """The receipt for one action that reported success, or None if no check
    exists for the tool. Blocking: run it in a thread."""
    fn = CHECKS.get(tool)
    if fn is None:
        return None
    # SQLite's UTC text, as every other time Steve keeps: `time.strftime`
    # wrote the local time in the same shape, hours off and unmarked.
    stamp = sqltime.stamp(dt.datetime.now(dt.UTC))
    for attempt in (1, 2):
        try:
            target, observed = fn(args, result)
            return Receipt(tool, VERIFIED, str(target), observed, "", stamp)
        except NotThere as exc:
            if attempt == 1:
                time.sleep(RETRY_AFTER_S)
                continue
            return Receipt(tool, FAILED, why=f"it is not there ({exc})", checked_at=stamp)
        except ValueError as exc:
            return Receipt(tool, FAILED, why=str(exc), checked_at=stamp)
        except Exception as exc:  # noqa: BLE001 - an unchecked action, not a failed one
            return Receipt(tool, UNCHECKED, why=f"could not read it back: {exc}"[:200],
                           checked_at=stamp)
    return None  # unreachable


async def attach(tool: str, args: dict, result):
    """`result` with its receipt; a contradicted action reports a failure.

    Called by `actions.perform` for every action, free or approved. A
    result that already reports a failure is left alone: there is nothing
    to confirm."""
    from agent.approvals import reported_failure

    if not isinstance(result, dict) or reported_failure(result) is not None:
        return result
    if tool not in CHECKS:
        return result
    try:
        receipt = await asyncio.wait_for(asyncio.to_thread(check, tool, args, result),
                                         CHECK_TIMEOUT_S)
    except TimeoutError:
        receipt = Receipt(tool, UNCHECKED, why="the read-back took too long")
    if receipt is None:
        return result
    out = {**result, "receipt": receipt.as_dict()}
    if receipt.status == FAILED:
        log.warning("%s reported success but %s", tool, receipt.why)
        _ledger(tool, receipt)
        out.update(ok=False, error=f"{tool} reported success, but {receipt.why}. "
                                   "Tell the user it did not happen as asked.")
    return out


def _ledger(tool: str, receipt: Receipt) -> None:
    """Into the failure ledger, where the night looks for what to fix."""
    from agent import issues, runtime

    issues.record(runtime.conn, "action_unverified", f"{tool}: {receipt.why[:60]}",
                  f"{tool} said it worked, but {receipt.why[:140]}",
                  excerpt=str(receipt.observed)[:400])
