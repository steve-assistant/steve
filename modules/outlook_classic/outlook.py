"""Work or school mail, read locally through the Outlook that is already
signed in.

The alternative was forwarding Outlook to Gmail, and it fails on the thing
that matters: forwarding carries what *arrives*. Sent mail never forwards,
and sent mail is the half commitment extraction reads -- their own words, the
half the user authored. It would also have put a permanent copy of an
organisation's mail in a personal Google account, which is the opposite of
the intent.

So this reads MAPI through classic Outlook instead. What that buys is not
convenience, it is the absence of a credential: Outlook is a first-party
client the organisation's tenant already trusts, so there is no app
registration, no admin ticket, and no refresh token for this agent to hold
or leak. Nothing leaves the computer.

It asks Outlook nothing until the user adds the account. Asking COM whether
Outlook holds an account starts Outlook, and a fresh install once did that
unasked, then read and watched its mail; a switch (`OUTLOOK`) stopped it
until step 5's item 10, and now adding the account is the user's yes. The
one way in, `_namespace`, opens Outlook through that account's context
(`ctx.com`, core/modctx.py), which reaches only the members the manifest
lists, and which only an added account's provider sets (`as_account`).

Three things learned the hard way, all live:

  - `Items.Sort("[ReceivedTime]")` on Sent Items silently yields nothing.
    Sent items are ordered by `[SentOn]`. The first probe reported "sent: 0"
    against a folder holding forty-six messages.
  - `MailItem.To` is display names -- "Doe, Jane". The address lives on
    the Recipients collection, and for an Exchange recipient `.Address` is an
    X.500 distinguished name, not SMTP. Resolving it takes `GetExchangeUser`
    with a `PR_SMTP_ADDRESS` fallback.
  - The mailbox is a cache, not the server. Only what Outlook has synced is
    visible, so the window this can see is bounded by the "mail to keep
    offline" setting rather than by anything here.

Windows-only by construction, and none of it imported here: core opens
COM, and initialises it once for each thread that asks.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from steve_sdk import query as query_syntax
from steve_sdk.errors import ConnectorError, NotConnected, TargetChanged

log = logging.getLogger("outlook")

# Outlook's own folder ids.
FOLDER_SENT = 5
FOLDER_INBOX = 6

# How far under the Inbox to look. A user who files with server-side rules
# keeps hundreds of messages -- newsletters, notifications, a rule-made
# `Junk/Auto` -- in subfolders, and a reader that only walked the Inbox root
# reported them as not existing rather than as filed.
MAX_FOLDER_DEPTH = 4

# Outlook's own bookkeeping, which is not mail. `Sync Issues` in particular
# holds replication logs that are permanently unread, so counting them would
# make "you have unread mail" permanently and uselessly true.
SKIP_FOLDERS = frozenset({
    "sync issues", "conflicts", "local failures", "server failures",
})

# `MailItem`. A calendar acceptance or a task request is still an interaction
# worth recording, but it has no body worth extracting from.
CLASS_MAIL = 43

# A recipient's `Type` (`OlMailRecipientType`): To, Cc, Bcc.
RECIPIENT_TO, RECIPIENT_CC, RECIPIENT_BCC = 1, 2, 3

# The property Exchange keeps the real address under when `Recipient.Address`
# hands back an X.500 distinguished name.
PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"

# Enough of a message to find a promise in. A commitment lives in the first
# paragraph or it is under a quoted thread, which is somebody else's words.
MAX_BODY_CHARS = 4_000


def _safe(fn):
    """Turn a COM failure into a result the model can relay.

    The Google modules have `guard` for this, but borrowing it here would
    import the Google stack into the one module whose whole argument is that
    it needs no credentials -- and a gate test checks exactly that. Same
    shape, four lines, no dependency.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ConnectorError:
            raise  # already the slot's: no account in use here
        except Exception as exc:  # noqa: BLE001 - a dead mailbox is an answer
            if (plain := unusable(exc)) is not None:
                raise plain from exc
            log.exception("%s failed", fn.__name__)
            return {"error": f"{type(exc).__name__}: {exc}"[:200], "kind": "outlook"}

    return wrapper


# What COM answers when Outlook cannot be used at all, by its HRESULT: the
# account's health, in words the user can act on, rather than one more
# failed search. Each is checked in the error and in the exception Outlook
# raised inside it, where a refusal from Outlook itself is.
_NOT_INSTALLED = frozenset({
    -2147221005,  # CO_E_CLASSSTRING: no such program registered
    -2147221164,  # REGDB_E_CLASSNOTREG
    -2147221003,  # CO_E_APPNOTFOUND
})
_REFUSED = frozenset({
    -2147467260,  # E_ABORT: the "A program is trying to access" prompt, denied
    -2147024891,  # E_ACCESSDENIED: denied by a policy
})
NOT_INSTALLED = (
    "Classic Outlook is not on this computer. The new Outlook cannot be "
    "connected this way: a personal Outlook.com, Hotmail or Live account can be "
    "added by its address instead, and a work account cannot be connected in "
    "this version of Steve.")
REFUSED = (
    "Outlook refused Steve. When Outlook asks whether \"a program is trying to "
    "access\" it, allow it; if it never asks, whoever manages this computer has "
    "turned that access off.")


def unusable(exc: BaseException) -> ConnectorError | None:
    """The account's trouble, in plain words, if `exc` says Outlook cannot
    be used here at all; None for a failure of one call."""
    args = getattr(exc, "args", ())
    codes = {args[0]} if args and isinstance(args[0], int) else set()
    inner = args[2] if len(args) > 2 else None
    if isinstance(inner, tuple) and len(inner) > 5 and isinstance(inner[5], int):
        codes.add(inner[5])
    if codes & _NOT_INSTALLED:
        return NotConnected(NOT_INSTALLED)
    if codes & _REFUSED:
        return NotConnected(REFUSED)
    return None


# The account the calls in this thread or task are for: its context, set by
# its provider around each call (`as_account`), so the functions below need
# no argument. Unset, there is no way in.
_ACCOUNT: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "outlook_account", default=None)


@contextlib.contextmanager
def as_account(ctx: Any) -> Iterator[Any]:
    """The calls inside are for the account whose context is `ctx`."""
    token = _ACCOUNT.set(ctx)
    try:
        yield ctx
    finally:
        _ACCOUNT.reset(token)


def _context() -> Any:
    ctx = _ACCOUNT.get()
    if ctx is None:
        raise NotConnected("no Outlook account is in use here")
    return ctx


def _application():
    """Outlook itself, opened through the account's context."""
    return _context().com.open("Outlook.Application")


def _namespace():
    return _application().GetNamespace("MAPI")


def accounts() -> list[str]:
    """The address of every account in Outlook's profile. Raises: an empty
    answer would read as "none of these is the user's"."""
    mapi = _namespace()
    return [str(mapi.Accounts.Item(i).SmtpAddress or "")
            for i in range(1, mapi.Accounts.Count + 1)]


def _smtp(entry) -> str:
    """This recipient's real address, whatever shape Exchange stored it in."""
    try:
        raw = (entry.Address or "").strip()
    except Exception:  # noqa: BLE001
        raw = ""
    if "@" in raw:
        return raw.lower()
    for resolve in (
        lambda: entry.AddressEntry.GetExchangeUser().PrimarySmtpAddress,
        lambda: entry.AddressEntry.PropertyAccessor.GetProperty(PR_SMTP),
    ):
        try:
            found = (resolve() or "").strip()
            if "@" in found:
                return found.lower()
        except Exception:  # noqa: BLE001 - a distribution list, or a dead entry
            continue
    return ""


def _named(display: str, address: str) -> str:
    """`Clara Moss <clara@northwind.example>`, the shape the header parser expects.

    Both halves, because each is load-bearing and neither is available from
    the other. `Recipients.Item(i).Address` on Exchange is an X.500 string
    with no name in it; `.Name` is "Singh, Gurnoor" with no address. Sending
    only the address is what the first live ingest did, and it produced
    thirty-seven contacts with no names at all -- a `who_is` that can only be
    searched by email is most of the way to useless.
    """
    display = (display or "").strip().strip('"')
    if not display or display.lower() == address:
        return address
    return f'"{display}" <{address}>'


def _recipients(item, kind: int | None = None) -> str:
    """The recipients, as addresses with their display names: those of one
    `kind` (`olTo`, `olCC`, `olBCC`), or all of them."""
    found = []
    try:
        for i in range(1, item.Recipients.Count + 1):
            entry = item.Recipients.Item(i)
            if kind is not None and int(getattr(entry, "Type", RECIPIENT_TO)) != kind:
                continue
            address = _smtp(entry)
            if address:
                found.append(_named(getattr(entry, "Name", ""), address))
    except Exception:  # noqa: BLE001
        pass
    return ", ".join(found)


def _sender(item) -> str:
    for resolve in (
        lambda: _smtp(item.Sender),
        lambda: item.SenderEmailAddress,
    ):
        try:
            found = (resolve() or "").strip()
            if "@" in found:
                return _named(getattr(item, "SenderName", ""), found.lower())
        except Exception:  # noqa: BLE001
            continue
    return ""


# Enough to tell an invitation from a newsletter, and to see a date if one
# is stated up front. Not enough to be a body: that is what `read` is for.
SNIPPET_CHARS = 400


def _snippet(item) -> str:
    """The first line or two, flattened."""
    try:
        body = (item.Body or "")[: SNIPPET_CHARS * 3]
    except Exception:  # noqa: BLE001 - not every item class has a body
        return ""
    return " ".join(body.split())[:SNIPPET_CHARS]


def _local_zone():
    """The zone Outlook's wall-clock times are in: the user's, which is
    this machine's (`ctx.zone`)."""
    return _context().zone


def _epoch_ms(when) -> int:
    """A MAPI timestamp as real epoch milliseconds.

    `ReceivedTime`, `SentOn` and the modification times are the local
    wall-clock time, and pywin32 hands them back labelled UTC -- the same
    thing `modules/outlook_classic/calendar.py` found of an appointment's `Start` and
    `End`, and why it reads `StartUTC` instead. Mail has no `ReceivedTimeUTC`,
    so the label is dropped and the clock read as local.

    This undoes a fix of 21 September 2026 that trusted the label. On 7
    October LinkedIn's two new-device emails, sent the second the user's agent
    signed in at 13:07:51 EDT, were stamped 13:07:31 and 13:07:50 *UTC* --
    09:07 local, four hours before the sign-in that caused them -- and the
    alert about them came to them as news five minutes after the agent's own
    login.

    The rebuild from `timetuple()` is kept: `pywintypes.datetime` does not
    always survive arithmetic. A naive value is local too.
    """
    wall = datetime(*when.timetuple()[:6])
    zone = _local_zone()
    try:
        moment = wall.replace(tzinfo=zone)
    except Exception:  # noqa: BLE001 - an odd zone object: the OS's own reading
        moment = wall
    return int(moment.timestamp() * 1000)


def _summarise(
    item, *, direction: str, include_body: bool, folder: str = "",
    body_chars: int = MAX_BODY_CHARS,
) -> dict | None:
    """One message, in the same shape `modules/google_cloud/gmail.py` returns.

    Deliberately identical so `tools/ingest.py` does not need to know which
    mailbox a message came out of -- the people ledger cares who and when,
    not which vendor delivered it.
    """
    try:
        when = item.SentOn if direction == "sent" else item.ReceivedTime
        summary = {
            "id": item.EntryID,
            "thread_id": getattr(item, "ConversationID", None),
            "from": _sender(item) if direction != "sent" else "",
            "to": _recipients(item) if direction == "sent" else "",
            "subject": str(item.Subject or ""),
            "internal_date": _epoch_ms(when),
            # Present because `modules/google_cloud/gmail.py` returns it and this
            # claims to return the same shape. It did not, so a merged
            # search could only ever answer "no unread mail" -- the Gmail
            # half carried the flag and the Outlook half silently did not.
            "unread": bool(getattr(item, "UnRead", False)),
            "folder": folder,
            # Gmail returns one and this claims to match its shape. It also
            # earns its cost: an Outlook EntryID is 148 characters, and a
            # model handed twenty of them to choose between will sooner or
            # later truncate one -- so the less often a decision needs a
            # read, the less often that happens.
            "snippet": _snippet(item),
        }
    except Exception as exc:  # noqa: BLE001 - one odd item is not the sweep
        log.debug("skipped an item: %s", exc)
        return None

    if include_body:
        body = _body(item)
        # Said, as Gmail says it: a model shown 4,000 characters of a longer
        # message with nothing to tell it so read the cut as the end.
        summary["body"] = body[:body_chars]
        summary["body_truncated"] = len(body) > body_chars
        summary["body_length"] = len(body)
    return summary


def _body(item) -> str:
    try:
        return str(item.Body or "")
    except Exception:  # noqa: BLE001
        return ""


def _lines(item) -> dict:
    """The recipients, each line apart: a draft's card binds its Cc and
    Bcc, and these are what it is read back with."""
    return {"to": _recipients(item, RECIPIENT_TO), "cc": _recipients(item, RECIPIENT_CC),
            "bcc": _recipients(item, RECIPIENT_BCC)}


def _roots(mapi, folder_id: int) -> list:
    """The default folder of that kind, in every store that has one.

    Iterated rather than `mapi.GetDefaultFolder`, which only ever answers
    for the default delivery store. One account today, but the day a
    personal account is added to this profile the old call would go on
    quietly reading half the mail.
    """
    found, seen = [], set()
    count = 0
    # Guarded: an older profile may have no `Stores` collection at all, and
    # losing the whole mailbox over a missing attribute is the wrong trade
    # when `GetDefaultFolder` below still answers.
    with contextlib.suppress(Exception):
        count = mapi.Stores.Count
    for i in range(1, count + 1):
        try:
            folder = mapi.Stores.Item(i).GetDefaultFolder(folder_id)
            key = folder.EntryID
        except Exception:  # noqa: BLE001 - archives and public folders have none
            continue
        if key not in seen:
            seen.add(key)
            found.append(folder)
    if not found:
        with contextlib.suppress(Exception):
            found.append(mapi.GetDefaultFolder(folder_id))
    return found


def _descend(folder, depth: int = 0):
    """A folder and everything filed underneath it."""
    yield folder
    if depth >= MAX_FOLDER_DEPTH:
        return
    try:
        children = folder.Folders
        count = children.Count
    except Exception:  # noqa: BLE001
        return
    for i in range(1, count + 1):
        try:
            child = children.Item(i)
            name = str(child.Name or "").lower()
        except Exception:  # noqa: BLE001
            continue
        if name in SKIP_FOLDERS:
            continue
        yield from _descend(child, depth + 1)


def _walk_one(folder, *, direction: str, cutoff, limit: int, bodies: bool):
    """Newest first within one folder, stopping at the cutoff.

    Iterated rather than `Restrict`ed: the Jet date syntax wants a locale
    format that varies by machine, and getting it wrong returns an empty set
    rather than an error -- the same silent-nothing failure as sorting Sent
    Items by ReceivedTime.
    """
    name = ""
    with contextlib.suppress(Exception):
        name = str(folder.Name or "")
    try:
        items = folder.Items
        items.Sort("[SentOn]" if direction == "sent" else "[ReceivedTime]", True)
    except Exception:  # noqa: BLE001 - one unreadable folder is not the mailbox
        log.debug("could not open %s", name or "a folder")
        return []

    if bodies:
        # Passed through, not re-asked: the callers that ask for bodies name
        # the Sent folder, and tests/gate/test_sent_mail_only.py holds
        # every literal `bodies=True` to that.
        return _read(items, direction=direction, cutoff=cutoff, limit=limit,
                     bodies=bodies, name=name)
    return _envelopes_of(folder, items, direction=direction, cutoff=cutoff,
                         limit=limit, name=name)


def _read(items, *, direction: str, cutoff, limit: int, bodies: bool, name: str) -> list[dict]:
    """Summaries off the top of a sorted folder, down to the cutoff."""
    found: list[dict] = []
    item = items.GetFirst()
    while item is not None and len(found) < limit:
        if bodies and getattr(item, "Class", CLASS_MAIL) != CLASS_MAIL:
            item = items.GetNext()
            continue
        summary = _summarise(
            item, direction=direction, include_body=bodies, folder=name
        )
        if summary is None:
            item = items.GetNext()
            continue
        if datetime.fromtimestamp(summary["internal_date"] / 1000) < cutoff:
            # Sorted descending, so the first message past the cutoff means
            # everything after it is older too.
            break
        found.append(summary)
        item = items.GetNext()
    return found


# ---------------------------------------------------------------------------
# Envelopes, kept between searches
#
# Reading a message's envelope is one COM call per property, and a search
# reads every envelope in a year of mail before it looks at the query: about
# 1,100 messages across nine folders, 18 seconds, whatever was asked. On 27
# September one question searched six times and took two minutes.
#
# So a folder's walk is kept and reused while the folder is unchanged -- the
# same count, the same number unread, the same newest message, which Outlook
# answers in milliseconds. New mail at the top is read on its own and put in
# front of what was kept. Anything else -- a message read, moved or deleted
# -- walks the folder again, and nothing is kept longer than ENVELOPE_TTL_S,
# which bounds what the mark cannot see (a subject edited in place).
# Envelopes only: bodies are never kept.
# ---------------------------------------------------------------------------

ENVELOPE_TTL_S = 900
# More new messages than this since the last walk, and it is walked again.
MAX_NEW_AT_TOP = 50


@dataclass
class _Walked:
    mark: tuple[int, int, str]  # item count, unread count, newest EntryID
    cutoff: datetime
    limit: int
    found: list[dict]
    at: float


_envelopes: dict[str, _Walked] = {}
# One walk at a time: a second search arriving mid-walk waits for it and then
# reads what it kept, rather than paying for the same walk alongside it.
_envelope_lock = threading.Lock()


def _envelopes_of(folder, items, *, direction: str, cutoff, limit: int, name: str) -> list[dict]:
    try:
        key = f"{folder.EntryID}:{direction}"
        first = items.GetFirst()
        mark = (int(items.Count), int(folder.UnReadItemCount),
                str(first.EntryID) if first is not None else "")
    except Exception:  # noqa: BLE001 - cannot tell if it changed, so do not keep it
        return _read(items, direction=direction, cutoff=cutoff, limit=limit,
                     bodies=False, name=name)

    with _envelope_lock:
        kept = _envelopes.get(key)
        if (kept is not None and time.monotonic() - kept.at < ENVELOPE_TTL_S
                and kept.cutoff <= cutoff and kept.limit >= limit):
            if kept.mark != mark:
                fresh = _new_at_top(items, kept, mark, direction=direction, name=name)
                if fresh is not None:
                    kept.found = (fresh + kept.found)[:kept.limit]
                    kept.mark = mark
            if kept.mark == mark:
                return _within(kept.found, cutoff, limit)

        found = _read(items, direction=direction, cutoff=cutoff, limit=limit,
                      bodies=False, name=name)
        _envelopes[key] = _Walked(mark, cutoff, limit, found, time.monotonic())
        return [dict(m) for m in found]


def _new_at_top(items, kept: _Walked, mark: tuple[int, int, str], *, direction: str,
                name: str) -> list[dict] | None:
    """The messages that arrived since `kept`, or None if more changed than
    that: the count grew by exactly what sits above the old newest message,
    and the unread count by exactly how many of those are unread."""
    added = mark[0] - kept.mark[0]
    if not 0 < added <= MAX_NEW_AT_TOP:
        return None
    fresh: list[dict] = []
    item = items.GetFirst()
    for _ in range(added):
        summary = _summarise(item, direction=direction, include_body=False,
                             folder=name) if item is not None else None
        if summary is None:
            return None
        fresh.append(summary)
        item = items.GetNext()
    try:
        below = str(item.EntryID) if item is not None else ""
    except Exception:  # noqa: BLE001
        return None
    if below != kept.mark[2]:
        return None
    if kept.mark[1] + sum(1 for m in fresh if m.get("unread")) != mark[1]:
        return None
    return fresh


def _within(found: list[dict], cutoff, limit: int) -> list[dict]:
    """Copies, because callers tag and annotate what they are given."""
    out: list[dict] = []
    for message in found:
        if datetime.fromtimestamp(message["internal_date"] / 1000) < cutoff:
            break
        out.append(dict(message))
        if len(out) >= limit:
            break
    return out


def _walk(folder_id: int, *, direction: str, days: int, limit: int, bodies: bool):
    """Newest first across the whole subtree, not just its root.

    `limit` is applied per folder and then again to the merged result,
    because a folder is walked newest-first and the interesting messages are
    at the front of each one. Taking `limit` from every folder and then
    sorting costs a few hundred summaries and cannot miss a recent message
    the way a global counter walked folder-by-folder would.
    """
    mapi = _namespace()
    cutoff = datetime.now() - timedelta(days=max(1, days))

    found: list[dict] = []
    for root in _roots(mapi, folder_id):
        for folder in _descend(root):
            found.extend(_walk_one(
                folder, direction=direction, cutoff=cutoff,
                limit=limit, bodies=bodies,
            ))

    found.sort(key=lambda m: m.get("internal_date") or 0, reverse=True)
    return found[:limit]


@_safe
def headers(direction: str, days: int, limit: int = 300) -> dict:
    """Envelope headers for one direction. No bodies, no model, no network."""
    folder = FOLDER_SENT if direction == "sent" else FOLDER_INBOX
    messages = _walk(folder, direction=direction, days=days, limit=limit, bodies=False)
    return {"count": len(messages), "messages": messages}


@_safe
def sent_with_bodies(days: int, limit: int = 20) -> dict:
    """Sent mail, with text, for commitment extraction.

    Sent only, and there is no inbox equivalent of this function on purpose.
    It is the one path where a model sees personal content unredacted, and
    what bounds it is direction: these are the user's own words about their own
    obligations.
    """
    messages = _walk(FOLDER_SENT, direction="sent", days=days, limit=limit, bodies=True)
    return {"count": len(messages), "messages": messages}


# ---------------------------------------------------------------------------
# What the model can ask for
# ---------------------------------------------------------------------------

# Outlook message ids are opaque and long; Gmail's are short hex. Rather than
# guess from shape, an Outlook id is handed to the model already prefixed, so
# `read_email` can route on it with no ambiguity at all.
ID_PREFIX = "outlook:"

# How far back a search looks. The mailbox is a local cache anyway, so this
# bounds the walk rather than the truth.
SEARCH_DAYS = 365


def tag(entry_id: str) -> str:
    return f"{ID_PREFIX}{entry_id}"


def untag(message_id: str) -> str | None:
    """The Outlook id inside a tagged one, or None if it is not ours."""
    text = (message_id or "").strip()
    return text[len(ID_PREFIX):] if text.startswith(ID_PREFIX) else None


# The query language, read here as far as MAPI can say it, lives in the
# SDK since the IMAP engine needs it too (step 5, item 6).
parse_query = query_syntax.parse


def _matches(summary: dict, parsed: query_syntax.Query) -> bool:
    """Every clause of the query, against one message's summary."""
    return query_syntax.matches(parsed, subject=str(summary.get("subject") or ""),
                                sender=str(summary.get("from") or ""),
                                recipient=str(summary.get("to") or ""),
                                unread=bool(summary.get("unread")))


@_safe
def search(query: str, limit: int = 10, *, days: int = SEARCH_DAYS) -> dict:
    """Find Outlook mail by words in the subject, sender or recipients."""
    parsed = parse_query(query)
    window = parsed.days or days
    found: list[dict] = []

    for folder, direction in (
        (FOLDER_INBOX, "received"), (FOLDER_SENT, "sent")
    ):
        for summary in _walk(
            folder, direction=direction, days=window, limit=400, bodies=False
        ):
            if not _matches(summary, parsed):
                continue
            summary["id"] = tag(summary["id"])
            summary["mailbox"] = "outlook"
            summary["direction"] = direction
            found.append(summary)

    found.sort(key=lambda m: m.get("internal_date") or 0, reverse=True)
    answer = {
        "query": query,
        "count": len(found[:limit]),
        "messages": found[:limit],
    }
    if parsed.ignored:
        # Said out loud rather than swallowed: a dropped operator means the
        # result is wider than what was asked for, and that is a thing the
        # model should be able to mention rather than present as exact.
        answer["ignored_terms"] = parsed.ignored
    return answer


@_safe
def read(message_id: str, body_chars: int = MAX_BODY_CHARS) -> dict:
    """One Outlook message, by the id a search returned: its body's first
    `body_chars` characters, and `body_length`."""
    entry_id = untag(message_id) or message_id
    mapi = _namespace()  # Outlook missing is the account's trouble (`_safe`), not the id's
    try:
        item = mapi.GetItemFromID(entry_id)
    except Exception:  # noqa: BLE001 - see below
        # Almost always a truncated id rather than a missing message: an
        # EntryID is 148 characters and a model that has been handed
        # twenty of them will eventually shorten one. MAPI reports that
        # as a bare "operation failed", which tells the model nothing it
        # can act on, so say the actionable thing instead.
        log.info("no Outlook item for an id of %d chars", len(entry_id))
        return {
            "error": (
                "no Outlook message has that id -- ids are long and must be "
                "passed back exactly as given. Search again to get it."
            ),
            "kind": "usage",
        }
    summary = _summarise(item, direction="received", include_body=True,
                         body_chars=body_chars)
    if summary is None:
        return {"error": "could not read that message", "kind": "usage"}
    # `_summarise` only fills the side its direction asks for, and a
    # message read by id could be either.
    summary["from"] = _sender(item)
    summary.update(_lines(item))
    summary["id"] = tag(entry_id)
    summary["mailbox"] = "outlook"
    return summary


def _as_html(text: str) -> str:
    """Plain text as a paragraph of HTML, for the top of a reply."""
    import html

    paragraphs = [p for p in text.strip().split("\n\n") if p.strip()]
    return "".join(
        f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs
    )


def _above_the_quote(reply_html: str, text: str) -> str:
    """Put the user's words above the quoted original, inside the body.

    `MailItem.Reply()` fills `HTMLBody` with the quoted thread already
    wrapped in `<html><body>`. Prepending to the string would put the text
    before `<html>`, which Outlook renders in some views and drops in
    others; it goes just inside `<body>` instead.
    """
    import re

    ours = _as_html(text)
    match = re.search(r"<body[^>]*>", reply_html, flags=re.IGNORECASE)
    if not match:
        return ours + reply_html
    return reply_html[: match.end()] + ours + reply_html[match.end():]


def _reply_to(item):
    """Reply -- or, to a message the user sent themselves, reply to everyone but them.

    A follow-up nudge ("just checking in on the below") is a reply to their own
    unanswered email, and Outlook's `Reply` on that addresses it to its
    sender, who is them. `ReplyAll` keeps the people the user wrote to; their own
    addresses are then taken off it.
    """
    try:
        mine = {a.lower() for a in accounts() if a}
    except Exception:  # noqa: BLE001 - a plain reply, then
        mine = set()
    sender = _sender(item)
    address = sender[sender.rfind("<") + 1 : -1] if sender.endswith(">") else sender
    if not mine or address.lower() not in mine:
        return item.Reply()
    reply = item.ReplyAll()
    for i in range(reply.Recipients.Count, 0, -1):
        if _smtp(reply.Recipients.Item(i)) in mine:
            reply.Recipients.Remove(i)
    return reply


@_safe
def draft_reply(message_id: str, body: str, *, cc: str = "", bcc: str = "") -> dict:
    """A reply to one Outlook message, saved in Outlook Drafts and not sent.

    Through Outlook's own `Reply()`, so the recipients, the `RE:` subject,
    the threading and the quoted original are exactly what pressing Reply
    would give them -- the user opens it, reads it, and presses Send or does not.
    Never sends, and the draft can be thrown away with `delete_draft`. The
    one `.Send()` in this module is `send_draft`, which only an approved
    action reaches.
    """
    entry_id = untag(message_id) or message_id
    mapi = _namespace()  # as in `read`
    try:
        item = mapi.GetItemFromID(entry_id)
    except Exception:  # noqa: BLE001 - almost always a shortened id; see `read`
        return {
            "error": (
                "no Outlook message has that id -- ids are long and must be "
                "passed back exactly as given. Search again to get it."
            ),
            "kind": "usage",
        }
    reply = _reply_to(item)
    # The copies asked for, in place of Outlook's own; none asked, Outlook's.
    if cc:
        reply.CC = cc
    if bcc:
        reply.BCC = bcc
    reply.HTMLBody = _above_the_quote(str(reply.HTMLBody or ""), body)
    reply.Save()
    return {
        "drafted": True,
        "draft_id": tag(reply.EntryID),
        "mailbox": "outlook",
        "to": str(reply.To or ""),
        "subject": str(reply.Subject or ""),
        "where": "Outlook Drafts",
    }


def _sending_account(mapi):
    """The account a new email goes from: the only one, or with several, None
    for Outlook's default, which the user chose in Outlook. It picked the
    account at one university's domain, which for anyone else was none."""
    try:
        found = [mapi.Accounts.Item(i) for i in range(1, mapi.Accounts.Count + 1)]
    except Exception:  # noqa: BLE001 - Outlook's default account, then
        return None
    return found[0] if len(found) == 1 else None


@_safe
def draft_new(to: str, subject: str, body: str, *, cc: str = "", bcc: str = "") -> dict:
    """A new email from their Outlook account, saved in Outlook Drafts and not sent.

    The first step of every Outlook send (`agent/toolset/mail.py`): the whole
    email sits in their Drafts to be read, and only their yes sends it
    (`send_draft`). A card's preview is capped on purpose, so the draft is
    where the full text is.
    """
    mapi = _namespace()
    item = _application().CreateItem(0)  # olMailItem
    account = _sending_account(mapi)
    if account is not None:
        with contextlib.suppress(Exception):
            item.SendUsingAccount = account
    item.To = to
    if cc:
        item.CC = cc
    if bcc:
        item.BCC = bcc
    item.Subject = subject
    item.HTMLBody = _as_html(body)
    item.Save()
    return {
        "drafted": True,
        "draft_id": tag(item.EntryID),
        "mailbox": "outlook",
        "to": str(item.To or to),
        "subject": str(item.Subject or subject),
        "where": "Outlook Drafts",
    }


@_safe
def send_draft(draft_id: str, *, unchanged: Callable[[dict], bool],
               body_chars: int = MAX_BODY_CHARS) -> dict:
    """Send an Outlook draft, only as its card showed it.

    Reached only through an approved `send_draft` action: nothing leaves
    the user's mailbox without their explicit yes. A message already sent
    is refused rather than sent again.

    Outlook sends an item by itself, whatever it holds by then: a Cc added
    in the moment between the write path's check and this call went,
    though the card never showed it. So the item is read again here, on
    the object `Send` is called on, as `read` reads it (`body_chars` of its
    body), and `unchanged` says whether that is still what the card bound.
    COM gives no send of a copy fixed at the check, so this is the nearest:
    the check and the send are on one object, a few calls apart.
    """
    entry_id = untag(draft_id) or draft_id
    item = _namespace().GetItemFromID(entry_id)
    if bool(getattr(item, "Sent", False)):
        return {"error": "that message was already sent; it is not a draft",
                "kind": "usage"}
    now = {**_lines(item), "subject": str(item.Subject or ""),
           "body": _body(item)[:body_chars]}
    if not unchanged(now):
        raise TargetChanged("the draft changed after it was checked; nothing was sent")
    to, subject = str(item.To or ""), str(item.Subject or "")
    item.Send()
    return {"sent": True, "mailbox": "outlook", "to": to, "subject": subject}


@_safe
def delete_draft(draft_id: str) -> dict:
    """Throw away a draft this module saved. The inverse of `draft_reply`.

    Refuses anything already sent: a sent item is not a draft, and deleting
    it from Sent Items would be destroying their record, not undoing ours.
    """
    entry_id = untag(draft_id) or draft_id
    item = _namespace().GetItemFromID(entry_id)
    if bool(getattr(item, "Sent", False)):
        return {"error": "that message was sent; it is not a draft", "kind": "usage"}
    item.Delete()
    return {"deleted": True, "draft_id": tag(entry_id)}
