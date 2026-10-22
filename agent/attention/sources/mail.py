"""New mail, from each mail account the user connected.

Every few minutes, which is only affordable because a poll that finds
nothing new costs one call: an account that can list ids cheaply lists them,
the store says which it has seen, and only the rest are fetched
(`MailProvider.arrived`). The watcher this replaces fetched headers for up
to twenty-five messages on every sweep, which is why it could only afford to
sweep every three hours.

One source per account (`watch`), named after it (`mail:<account key>`), so
an account switched off stops being polled and its queued observations are
skipped (`core/modules.TAGGED`). Each was a function named after its provider,
and a second account of one kind could not be watched at all.

Read mail is included, not just unread. The user reads on their phone and acts
later; "they have opened it" is not "it has been dealt with", and a date in an
email they skimmed still belongs on their calendar. The judge is told which is
which, because telling them about something the user has plainly already seen is
the noise that gets a notifier muted.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3

from agent.attention.store import Observation, known
from agent.connectors import Connected
from steve_sdk.classes import Message

log = logging.getLogger("attention")

# How many messages one poll lists. More than a busy two days of inbox.
LIST_LIMIT = 50

# How far back one poll looks.
DAYS = 2

# The mailing headers an account may give in a message's `extra`, and its
# own sorting of the sender (`vet.SORTED`), which `vet.sender_class` reads.
_HEADERS = ("auto_submitted", "precedence", "list_id", "list_unsubscribe",
            "sorted_as", "filed_under")


def _key(message_id: str) -> str:
    """A message's key: its slot id, so the key names its account."""
    return f"mail:{message_id}"


def _observe(message: dict, found: Connected,
             conn: sqlite3.Connection | None = None) -> Observation:
    """One message, as the judge and the doer will see it: its sender,
    subject and preview as the injection screen allows (agent/screening.py),
    screened in place -- the sources run in a thread with a connection of
    their own. Until 11 October 2026 only Gmail's were screened, and only in
    shadow; Outlook's and the password mailbox's reached the judge unscreened.

    The id goes into `ref` as the slot id `read_email` takes back to the
    account; a long one is shown to the doer as a short handle
    (agent/handles.py), which it can pass back to read or reply.
    """
    from email.utils import parseaddr

    from agent import screening
    from agent.toolset.mail import word_for

    seen = "already read" if not message.get("unread") else "unread"
    mailbox = word_for(found)
    snippet = str(message.get("snippet") or "").strip()[:300]
    subject = message.get("subject") or "(no subject)"
    sender = message.get("from") or "unknown"
    preview = screening.shown_here(conn, "email_preview", message.get("id"),
                                   message.get("from"), screening.preview_text(message))
    passed = preview.how == "text"
    return Observation(
        source=f"mail:{found.key}",
        key=_key(str(message.get("id"))),
        title=f"{subject} -- from {sender}" if passed else f"An email in their {mailbox} inbox",
        detail=f"{mailbox}, {seen}. {snippet if passed else preview.text}".strip(),
        when=message.get("when"),
        ref={
            "message_id": message.get("id"),
            "thread_id": message.get("thread_id"),
            "account": found.name,
            # The address alone when the screen did not pass it: a display
            # name is the sender's to write.
            "from": message.get("from") if passed else parseaddr(str(sender))[1],
            "subject": message.get("subject") if passed else None,
            **_structure(message),
        },
    )


def _listed(found: Connected, message: Message) -> dict:
    """A message as the mail tools list it (`toolset.mail.shown`), with what
    the sender checks read: the mailing headers and the RFC Message-ID."""
    from agent.toolset.mail import shown

    out = shown(found, message)
    out.update({k: v for k, v in message.extra.items() if k in _HEADERS and v})
    if message.ident:
        out["rfc_id"] = message.ident
    return out


def _structure(message: dict) -> dict:
    """What `vet.sender_class` and `vet.names` read, as far as this account's
    listing gave it: the mailing headers and the account's own sorting, how
    many it was addressed to, the RFC Message-ID. Kept in `ref` because a
    column on `observations` is a frozen change (a migration in
    db/migrations/).

    An account that lists received mail as envelopes (classic Outlook, over
    COM) gives none of it, and its `to` is empty: no count then, since 0
    would be a lie, not an absence. Its mail is checked on the address, the
    greeting and the fingerprint instead."""
    from tools import people

    out: dict = {}
    headers = {key: message[key] for key in _HEADERS if message.get(key)}
    if headers:
        out["headers"] = headers
    if rfc := message.get("rfc_id"):
        out["rfc_id"] = rfc
    addressed = ", ".join(str(message.get(k) or "") for k in ("to", "cc")).strip(", ")
    if addressed:
        out["recipients"] = len(people.parse_addresses(addressed))
    return out


def watch(conn: sqlite3.Connection, found: Connected) -> list[Observation]:
    """New mail in one account since `DAYS` ago, less what earlier polls
    observed. An account not on this computer (classic Outlook closed for
    good, a mailbox whose password is not here) is a normal laptop, not a
    failure: nothing."""
    from agent import connectors
    from agent.toolset.mail import localise
    from steve_sdk.errors import NotConnected

    def seen(natives: list[str]) -> set[str]:
        observed = known(conn, [_key(found.id(n)) for n in natives])
        return {n for n in natives if _key(found.id(n)) in observed}

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=DAYS)
    try:
        arrived = connectors.blocking(
            found, lambda p: p.arrived(since=since, limit=LIST_LIMIT, seen=seen),
            limit_s=None)
    except NotConnected:
        return []
    return [_observe(m, found, conn) for m in localise([_listed(found, m) for m in arrived])]
