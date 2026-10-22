"""Reading the mailbox into the people ledger, without a model.

Separate from `tools/people.py` because of what it touches. That module is
pure SQL over a local database and is trivially testable; this one talks to
every mail account and calendar. Keeping the seam means the 800-people
filtering logic can be tested against a hundred fabricated headers in
milliseconds.

Every account the user connected, through the slot (`agent/connectors.py`),
and each message and event recorded by its slot id, so an interaction names
its account and one switched off is left out of what people reads
(`core/modules.TAGGED`). Until 14 October 2026 there was a function for
Gmail and one for Outlook, and a mailbox of any other kind was never read.

Nothing here consults a model and no message body is read. Headers only:
who, when, what subject, which direction. That restraint is the entire reason
ingesting *received* mail is acceptable -- the moment a body is involved, the
rule is sent mail only, and that path is `tools/extract.py`.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from core import clock
from tools import people

if TYPE_CHECKING:
    from agent.connectors import Connected
    from steve_sdk.classes import Message

log = logging.getLogger("ingest")

# How much history a first run reads. Long enough to know who the user deals with,
# short enough that the first run is not an afternoon.
FIRST_RUN_DAYS = 90

# Per run, per direction. A cap in code rather than in a prompt, for the
# reason every cap in this codebase is.
MAX_MESSAGES = 300


def _window(conn: sqlite3.Connection, source: str, days: int | None) -> int:
    """How many days back to read. A cursor means "only since last time"."""
    if days is not None:
        return days
    if people.cursor(conn, source) is None:
        return FIRST_RUN_DAYS
    return 3


def _occurred(message: dict) -> str:
    """When this message happened, as a sortable ISO string.

    Prefers Gmail's `internalDate` over the `Date:` header. The header is
    whatever the sending client claimed, in RFC 2822, in a timezone of its
    choosing -- and `last_seen` is compared with `>=` and `MAX()`, which on a
    text column means an unparsed header sorts alphabetically. "Fri" before
    "Mon" is not a chronology.

    In the user's zone as `core.clock` names it, the one place it is asked.
    Asked of the machine here instead, a test that pinned the zone found a
    thread's age a day out on CI's runner, which is on UTC (22 October 2026).
    """
    zone = clock.local_zone()
    epoch_ms = message.get("internal_date")
    if epoch_ms:
        moment = datetime.fromtimestamp(int(epoch_ms) / 1000, zone).replace(tzinfo=None)
        return moment.isoformat(timespec="seconds")

    raw = message.get("date")
    if raw:
        from email.utils import parsedate_to_datetime

        try:
            return parsedate_to_datetime(raw).astimezone(zone).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            pass
    return datetime.now().isoformat(timespec="seconds")


def envelope(found: Connected, message: Message) -> dict:
    """One account's message as the ledger records it: who, to whom, when,
    the subject, and its ids as slot ids. Nothing else is carried, so no
    body or preview can reach the ledger by this road."""
    return {"id": found.id(message.native_id),
            "thread_id": found.id(message.thread) if message.thread else None,
            "from": message.sender, "to": message.to, "subject": message.subject,
            "internal_date": message.when or None, "date": message.extra.get("date")}


def _record(conn: sqlite3.Connection, messages: list[dict], direction: str) -> int:
    """Put one batch of envelopes (`envelope`) into the ledger, whichever
    account they came from: who and when is the whole of what it stores."""
    for message in messages:
        # The counterparty is whoever is on the other end: for mail the user sent,
        # the recipients; for mail the user received, the sender.
        header = message.get("to") if direction == "sent" else message.get("from")
        occurred = _occurred(message)
        for email, name in people.parse_addresses(header or ""):
            # Counted only when first logged: the same message is read
            # again every night and every few hours (`people.upsert`).
            person_id = people.upsert(
                conn, email, name=name, seen_at=occurred, sent=(direction == "sent"),
                count=False,
            )
            if not person_id:
                continue
            if people.record_interaction(
                conn, person_id,
                kind="email",
                direction=direction,
                source_id=message.get("id") or "",
                thread_id=message.get("thread_id"),
                subject=(message.get("subject") or "")[:200],
                occurred_at=occurred,
            ):
                people.counted(conn, person_id, sent=(direction == "sent"))
    return len(messages)


async def mail(
    conn: sqlite3.Connection,
    *,
    days: int | None = None,
    limit: int = MAX_MESSAGES,
) -> dict:
    """Ingest both directions of every mail account into people and
    interactions; `accounts` says how many of each came from which.

    Async, and the split matters: the accounts are asked in a thread because
    they are slow, and every SQLite call stays on this one because a
    connection belongs to the thread that opened it. Offloading the whole
    function was the first attempt and it raised on the first query --
    which is the good outcome, since the alternative failure is a night that
    silently writes nothing.

    An account not on this computer (classic Outlook absent), or one that
    cannot list a window of mail (`MailProvider.envelopes`), is skipped in
    silence: a machine without it is a normal machine, and the rest of the
    night should not notice.
    """
    from agent import connectors
    from steve_sdk.errors import NotConnected, Unsupported

    found: dict = {"sent": 0, "received": 0, "people": 0, "errors": [], "accounts": {}}

    for account in connectors.accounts(conn, "mail"):
        for direction in ("sent", "received"):
            source = f"ingest:mail:{account.key}:{direction}"
            window = _window(conn, source, days)
            since = datetime.now(UTC) - timedelta(days=window)
            try:
                listed = await connectors.call(
                    account, lambda p, s=since, d=direction: p.envelopes(
                        sent=d == "sent", since=s, limit=limit))
            except (NotConnected, Unsupported):
                break
            except Exception as exc:  # noqa: BLE001 - one direction failing is not both
                log.exception("ingest failed for %s %s", account.name, direction)
                found["errors"].append(f"{account.name} {direction}: {type(exc).__name__}")
                continue

            counted = _record(conn, [envelope(account, m) for m in listed if m.native_id],
                              direction)
            found[direction] += counted
            found["accounts"].setdefault(account.name, {"sent": 0, "received": 0})
            found["accounts"][account.name][direction] = counted
            people.set_cursor(
                conn, source, datetime.now().isoformat(timespec="seconds"),
                note=f"{counted} message(s) over {window}d",
            )

    # From the log rather than a running tally, which is what drifted.
    found["filtered"] = people.recount(conn)
    found["people"] = people.counts(conn)["real"]
    return found


# A recurring lecture is one fact, not forty. Collapsed by title before
# anything reads it -- the watcher learned this the expensive way, when its
# first sweep returned twenty-five items of which twenty-three were the same
# three classes.
def _distinct_titles(events: list[dict], limit: int = 25) -> list[str]:
    seen: dict[str, int] = {}
    for event in events:
        title = (event.get("summary") or "").strip()
        if title:
            seen[title] = seen.get(title, 0) + 1
    ordered = sorted(seen.items(), key=lambda kv: -kv[1])
    return [
        f"{title} (x{count})" if count > 1 else title
        for title, count in ordered[:limit]
    ]


async def calendar(conn: sqlite3.Connection, *, days: int = 14, agenda=None) -> dict:
    """Ingest who the user is meeting, and what their weeks look like.

    The calendar is where this account's signal actually is. Their Gmail turns
    out to be mostly services -- one sent message in ninety days -- while the
    calendar has classes, recruiting events and named guests on it. A people
    ledger built from mail alone would have been technically correct and
    empty.

    Attendees become people the same way senders do; the titles come back as
    a collapsed list for whatever wants to reason about their weeks.
    """
    from agent import offloop

    if agenda is None:
        from agent.calendars import agenda as agenda

    try:
        # On a connection of its own: the accounts are read from the
        # registry, in the thread that asks them.
        result = await offloop.with_own_conn(conn, agenda, days, limit=100)
    except Exception as exc:  # noqa: BLE001
        log.exception("calendar ingest failed")
        return {"events": 0, "titles": [], "errors": [type(exc).__name__]}
    if result.get("error"):
        return {"events": 0, "titles": [], "errors": [result["error"]]}

    events = result.get("events", [])
    for event in events:
        when = event.get("start") or ""
        for attendee in event.get("attendees") or []:
            email = (attendee.get("email") or "").strip().lower()
            # `self` is the user (both calendars mark it), not someone they met.
            if not email or attendee.get("self"):
                continue
            person_id = people.upsert(conn, email, seen_at=when, count=False)
            if person_id and people.record_interaction(
                conn, person_id,
                kind="event",
                direction="both",
                source_id=event.get("id") or "",
                subject=(event.get("summary") or "")[:200],
                occurred_at=when or datetime.now().isoformat(timespec="seconds"),
            ):
                people.counted(conn, person_id, sent=False)

    return {"events": len(events), "titles": _distinct_titles(events), "errors": []}
