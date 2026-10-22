"""Threads left hanging, in both directions.

What the products that feel like assistants do and this agent did not
(Instinct, Catch): notice that something is waiting. Two kinds:

- **The user owes a reply.** A real person wrote, days ago, and the last word in
  the thread is still theirs.
- **They owe the user one.** The user wrote, days ago, and nobody has answered -- the
  recruiter who went quiet, the professor who never confirmed.

Both come from envelopes only -- who, when, which thread, which direction
-- read fresh from every mail account that can list them
(`MailProvider.envelopes`), because the ledger this could have used is
filled once a night and "have they replied yet?" asked of last night's data
nags them about the reply they sent this morning. What is read is also written
to the ledger (`ingest._record`), so meeting prep reads a fresh one too. No
body is read here.

Each thread is mentioned once per state: the key names the account, the
thread and its last message, so the same silence is one observation, a new
message in the thread is a new one, and an account switched off has its
queued observations skipped (`core/modules.TAGGED`). Whether it matters --
most received mail needs no reply, and group mail rarely does -- is the
judge's question. Whether a
draft may be written without asking them is not: a reply the user owes could need
something only the user knows (a thank-you note is the user's example), so its task
is taken off in code and the headline asks instead (`triage.ask_first`). A
nudge on their own unanswered email needs nothing and may be drafted.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import sqlite3
from dataclasses import dataclass

from agent.attention import settings
from agent.attention.store import Observation
from agent.connectors import Connected
from steve_sdk.classes import Message as MailMessage

log = logging.getLogger("attention")

# How far back headers are read: past the longer of the two windows, so the
# message that ends a silence is seen even when it is the older one.
LOOKBACK_DAYS = 12
LIMIT = 300


@dataclass
class Message:
    account: Connected
    id: str  # its slot id, as a tool takes it
    thread: str  # the thread's slot id
    direction: str  # sent | received
    at: dt.datetime  # local, naive
    subject: str
    counterparts: list[tuple[str, str | None]]
    snippet: str = ""


def _messages(found: Connected, listed: list[MailMessage], direction: str) -> list[Message]:
    """One account's envelopes as threads are made of, by slot id
    (`ingest.envelope`)."""
    from tools import ingest, people

    out = []
    for listing in listed:
        if not listing.thread or not listing.native_id:
            continue
        message = ingest.envelope(found, listing)
        header = message["to"] if direction == "sent" else message["from"]
        out.append(Message(
            account=found,
            id=message["id"],
            thread=message["thread_id"],
            direction=direction,
            at=dt.datetime.fromisoformat(ingest._occurred(message)).replace(tzinfo=None),
            subject=listing.subject or "(no subject)",
            counterparts=people.parse_addresses(header or ""),
            snippet=listing.snippet.strip(),
        ))
    return out


def _read(conn: sqlite3.Connection) -> list[Message]:
    """Every mail account, both directions, envelopes only; recorded as read.
    An account not on this computer, or one that cannot list its mail, is
    left out; one that fails fails the source, to be tried again."""
    from agent import connectors
    from steve_sdk.errors import NotConnected, Unsupported
    from tools import ingest, people

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=LOOKBACK_DAYS)
    found: list[Message] = []
    for account in connectors.accounts(conn, "mail"):
        for direction in ("sent", "received"):
            try:
                listed = connectors.blocking(
                    account, lambda p, d=direction: p.envelopes(
                        sent=d == "sent", since=since, limit=LIMIT), limit_s=None)
            except (NotConnected, Unsupported):
                break
            ingest._record(conn, [ingest.envelope(account, m) for m in listed if m.native_id],
                           direction)
            found += _messages(account, listed, direction)
    # As every ingest ends: counts and flags from the log, so a newly seen
    # sender is judged by what is known of them, not by a running tally.
    people.recount(conn)
    return found


def _person(conn: sqlite3.Connection, email: str, name: str | None,
            mine: frozenset[str]) -> bool:
    """A real person and not the user: not a service, and not demoted by the ledger."""
    from tools import people

    email = (email or "").lower()
    if not email or email in mine or people.looks_automated(email, name):
        return False
    row = conn.execute("SELECT is_person FROM people WHERE email = ?", (email,)).fetchone()
    return row is None or bool(row["is_person"])


def _who(counterparts: list[tuple[str, str | None]]) -> tuple[str, str]:
    email, name = counterparts[0]
    return (name or email.split("@")[0]), email


def dropped(
    conn: sqlite3.Connection, messages: list[Message], now: dt.datetime,
    mine: frozenset[str]
) -> list[Observation]:
    """The threads whose last word has been waiting too long, either way."""
    from agent.toolset.mail import word_for

    by_thread: dict[str, list[Message]] = {}
    for message in messages:
        by_thread.setdefault(message.thread, []).append(message)

    out = []
    for thread, in_thread in by_thread.items():
        last = max(in_thread, key=lambda m: m.at)
        people = [(e, n) for e, n in last.counterparts if _person(conn, e, n, mine)]
        if not people:
            continue
        days = (now - last.at).days
        name, email = _who(people)
        found = last.account
        label = word_for(found)

        if last.direction == "received":
            if not settings.OWE_REPLY_AFTER_DAYS <= days <= settings.OWE_REPLY_UNTIL_DAYS:
                continue
            kind = "owed by the user"
            title = f"{name} wrote {days} days ago and you haven't replied: {last.subject}"
        else:
            if not settings.AWAIT_REPLY_AFTER_DAYS <= days <= settings.AWAIT_REPLY_UNTIL_DAYS:
                continue
            kind = "owed to the user"
            title = f"No reply from {name} in {days} days to your email: {last.subject}"

        stamp = hashlib.sha1(last.id.encode("utf-8")).hexdigest()[:12]
        threaded = hashlib.sha1(thread.encode()).hexdigest()[:12]
        out.append(Observation(
            source="threads",
            key=f"thread:mail:{found.key}:{threaded}:{stamp}",
            title=title,
            detail=f"{label}; a reply {kind}; {len(in_thread)} message(s) in the thread"
                   + (f"; {last.snippet[:240]}" if last.snippet else ""),
            when=last.at.isoformat(timespec="minutes"),
            ref={
                "message_id": last.id, "account": found.name, "waiting": kind,
                "with": f"{name} <{email}>", "subject": last.subject,
            },
        ))
    out.sort(key=lambda o: o.when or "", reverse=True)
    return out


def threads(conn: sqlite3.Connection, *, now: dt.datetime | None = None) -> list[Observation]:
    from agent import connectors

    return dropped(conn, _read(conn), now or dt.datetime.now(), connectors.addresses(conn))
