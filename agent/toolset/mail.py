"""Mail: every account's, searched, read, drafted and sent through the
connector slot (`agent/connectors.py`)."""

from __future__ import annotations

import asyncio
import html
import logging
import re

from pydantic_ai import RunContext

from agent import connectors, offloop, screening
from agent.actions import act, executable
from agent.approvals import one_line
from agent.connectors import Connected, NoAccount
from agent.toolset import oncard
from agent.toolset.clients import commitment_store
from agent.turn import Deps
from core import writes
from steve_sdk import classes, exact
from steve_sdk.classes import Draft, Message
from steve_sdk.errors import ConnectorError

log = logging.getLogger("toolset.mail")

# Words that stand for a name in an address *format*, never a name. On 8
# October 2026 the user said "lastname.firstname@northwind.example is the format"
# for a thank-you to Owen Tate; the task kept the format, and an Outlook
# draft was saved addressed to "lastname.firstname@northwind.example" as
# written. Split on . _ - +, so "first_name" is two of these.
PLACEHOLDER_WORDS = frozenset({
    "first", "last", "firstname", "lastname", "fname", "lname", "givenname", "surname",
    "familyname", "name", "fullname", "middle", "middlename", "initial", "firstinitial",
    "lastinitial", "flast", "firstlast", "lastfirst", "yourname", "theirname",
})
_ADDRESS = re.compile(r"([A-Za-z0-9._%+'-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# A blank left in a body for someone to fill: "[Your name]", "<your name>",
# "[Insert date]", "{name}". On 13 October 2026, a user with no name saved
# yet asked for a reply, and the draft was signed "[Your name]": sent as
# written, that is what Kim would have read. A bracket alone is no blank --
# "[1]", "[External]", a link's text -- so it must open on "your", an
# instruction, or a name.
_BLANK = re.compile(
    r"[\[<{]\s*(?:(?:your|insert|add|enter)\b[^\[\]<>{}\n]{0,40}"
    r"|(?:(?:recipient|sender|person|contact)'?s?\s+)?(?:full\s+|first\s+|last\s+)?name"
    r"|signature)\s*[\]>}]",
    re.IGNORECASE)


def placeholder_address(to: str) -> str | None:
    """The first address in `to` whose name part is a format rather than a
    person -- every piece a placeholder word or a lone initial, at least one
    a word ("lastname.firstname", "f.last", "first_name") -- or None."""
    for local, domain in _ADDRESS.findall(to or ""):
        pieces = [p for p in re.split(r"[._+-]", local.lower()) if p]
        if (any(p in PLACEHOLDER_WORDS for p in pieces)
                and all(p in PLACEHOLDER_WORDS or len(p) == 1 for p in pieces)):
            return f"{local}@{domain}"
    return None


def blank_in(body: str) -> str | None:
    """The first blank left to fill in `body`, or None."""
    return match.group(0) if (match := _BLANK.search(body or "")) else None


def _only_blanks(line: str) -> bool:
    return bool(_BLANK.search(line)) and not _BLANK.sub("", line).strip(" \t,.|-")


def unsigned(body: str) -> str:
    """`body` without a sign-off made only of blanks: "Love,\n[Your name]"
    ends "Love,", as mail is signed for a user who has saved no name.

    The model is told when no name is saved, and still signs "[Your name]"
    now and then. Refused, it should send again unsigned, but in a routine
    it asked the user instead, every time (the yardstick and a probe, 13
    October 2026), and the note was never sent. A line that holds only
    blanks carries nothing, so leaving it out loses nothing. A blank
    anywhere else is refused: what it stands for is the model's to fill.
    """
    lines = (body or "").split("\n")
    kept = len(lines)
    while kept and (not lines[kept - 1].strip() or _only_blanks(lines[kept - 1])):
        kept -= 1
    if not any(_only_blanks(line) for line in lines[kept:]):
        return body
    # Logged: left out unseen, the slip would never be counted again.
    log.info("left out a sign-off of blanks: %r", "\n".join(lines[kept:]).strip())
    return "\n".join(lines[:kept])


def _refuse_placeholder(to: str, body: str = "") -> dict | None:
    """The refusal a drafting or sending tool returns for a format in place
    of an address, or a blank in the body; None when `to` names real people
    and the body is whole."""
    if (bad := placeholder_address(to)) is not None:
        return {
            "ok": False,
            "error": (f"{bad} is an address format, not an address. Use the person's "
                      "own address, from their mail or who_is, or ask the user for it."),
        }
    if (blank := blank_in(body)) is not None:
        # It says nothing was queued: send_email's own text says a second
        # call is the same card, which reads as if the first one made it.
        return {
            "ok": False,
            "error": (f"Nothing was written or queued: the body has a blank, {blank}, that "
                      "would go out as written. Call this again with it filled in from what "
                      "you know, or with it left out."),
        }
    return None


def _address_for(conn, to: str) -> str:
    """The address `to` means: as given, or a name the user has written to."""
    from tools import people, style

    address = style.address_in(to)
    if not address and to.strip():
        match = people.find(conn, to.strip(), limit=1)
        address = match[0].email if match else ""
    return address


def localise(messages: list[dict]) -> list[dict]:
    """Add a readable local timestamp to each message, in place.

    Both mailboxes return `internal_date` as epoch milliseconds, which is
    the right thing to sort on and the wrong thing to hand a model. Asked
    for unread Outlook mail it rendered every one of them four hours out,
    because nothing in the payload said which zone the number was in and
    UTC is the obvious guess.

    The same instinct as `agent.loop._now_line`: a timestamp the model has to convert
    is a timestamp it will eventually convert wrongly, and the machine
    already knows the answer.
    """
    import datetime as dt

    from agent import zones

    zone = zones.local_zone()
    for message in messages:
        stamp = message.get("internal_date")
        if not stamp:
            continue
        moment = dt.datetime.fromtimestamp(stamp / 1000, zone)
        message["when"] = moment.strftime("%Y-%m-%d %H:%M %Z")
    return messages


# Characters newsletters pad their preview text with -- U+034F and the
# zero-width spaces, a hundred or more to a snippet -- so a mail client shows
# nothing after the preheader. To a model they are tokens and nothing else.
_PADDING = re.compile("[\u034f\u200b-\u200d\u2060\ufeff\u00ad]+")
# Labels that say nothing a reader can use: Gmail's ids for their own labels,
# and what `unread` already says.
_NO_LABEL = re.compile(r"^(?:Label_\d+|UNREAD|CATEGORY_PERSONAL)$")


def for_the_model(messages: list[dict]) -> list[dict]:
    """Search results with what the model would only have to read past
    taken out, after `localise`.

    A search result carried its time three ways (`internal_date` in epoch
    milliseconds, the raw `date` header, and `when`), the thread id again
    when it was the message id, labels like "Label_1", and snippets whose
    entities were never decoded ("We&#39;ve") and whose padding ran to
    hundreds of invisible characters. Over their last 200 searches (6 October
    2026) a Gmail result averaged 661 characters and is 503 without them;
    Outlook's and the password mailbox's carry less of it, about 4%. Only the model's
    copy is trimmed: the attention loop reads `internal_date`.
    """
    out = []
    for message in messages:
        message = dict(message)
        if message.get("when"):
            message.pop("internal_date", None)
            message.pop("date", None)
        if message.get("thread_id") == message.get("id"):
            message.pop("thread_id", None)
        labels = [la for la in message.get("labels") or [] if not _NO_LABEL.match(str(la))]
        if labels:
            message["labels"] = labels
        else:
            message.pop("labels", None)
        for key in ("subject", "snippet"):
            if isinstance(message.get(key), str):
                message[key] = " ".join(html.unescape(_PADDING.sub("", message[key])).split())
        out.append(message)
    return out


# What a result's sender wrote, or chose to put in its headers.
SENDERS_WORDS = ("from", "to", "cc", "subject", "snippet")


async def screened(conn, found: list[dict], copies: list[dict]) -> list[dict]:
    """Search results as the injection screen allows (agent/screening.py).
    Each one's sender, recipients, subject and snippet are its sender's
    words, screened as the attention loop screens a new email's preview
    (`screening.preview_text`), so one screening serves both. `found` as the
    mailboxes gave them, `copies` as the model reads them, in the same
    order. One the screen did not pass keeps where it sits and when it
    came, and loses those words to what the screen allows. They reached the
    model unscreened until step 11's audit."""
    seen = await asyncio.gather(*(screening.shown(
        conn, "email_preview", message.get("id"), message.get("from"),
        screening.preview_text(message)) for message in found))
    for copy, shown in zip(copies, seen, strict=True):
        if shown.how != "text":
            for key in SENDERS_WORDS:
                copy.pop(key, None)
            copy.update(screened=shown.how, preview=shown.text)
    return copies


async def sent_shown(conn, address: str, sent: list[dict]) -> list[dict]:
    """The user's emails to one person (`style.examples`) as the injection
    screen allows (agent/screening.py), each its subject and text as one
    item. The text is the user's, with the email it answered cut
    (`style.own_words`); but a reply's subject is its sender's, and a quote
    in a form the cut does not know comes whole. One the screen did not
    pass keeps when it was sent, and loses its subject and text to what the
    screen allows. They reached the model unscreened until the tools were
    made to declare what they return."""
    seen = await asyncio.gather(*(screening.shown(
        conn, "email_body", f"sent to {address}, {e.get('sent') or ''}", None,
        f"Subject: {e.get('subject') or ''}\n\n{e.get('text') or ''}") for e in sent))
    for example, shown in zip(sent, seen, strict=True):
        if shown.how != "text":
            example.pop("subject", None)
            example.update(screened=shown.how, text=shown.text)
    return sent


def word_for(found: Connected) -> str:
    """What the user reads for an account: the name it was given. Outlook
    had a word of its own ("Outlook", not "outlook") until step 5's item 10
    made it an account the user names."""
    return found.name


def shown(found: Connected, message: Message) -> dict:
    """A message as a search lists it, the same from every account: an id
    `read_email` takes back to its account, the account, who, what, when.

    Each mailbox answered in its own shape: Gmail with labels and no
    direction, Outlook with "received" on every message, the password mailbox with its
    Message-ID as `rfc_id`, and each with a thread id that only Gmail's
    `email_thread` could use. A thread id is given only by an account that
    can read a thread, and a direction only when it was sent."""
    out = {"id": found.id(message.native_id), "account": found.name,
           "from": message.sender, "to": message.to, "subject": message.subject,
           "snippet": message.snippet, "internal_date": message.when,
           "unread": message.unread}
    if message.cc:
        out["cc"] = message.cc
    if message.labels:
        out["labels"] = list(message.labels)
    if message.folder:
        out["folder"] = message.folder
    if message.direction == "out":
        out["direction"] = "sent"
    if message.thread and found.provider.has(classes.THREADS):
        out["thread_id"] = found.id(message.thread)
    if message.attachments:
        out["attachments"] = list(message.attachments)
    return out


def whole(found: Connected, message: Message) -> dict:
    """A message as `read_email` gives it: as listed, with its body."""
    out = shown(found, message)
    out.pop("snippet")
    if message.bcc:
        out["bcc"] = message.bcc
    body = message.body or ""
    length = max(message.body_length, len(body))
    out.update(body=body, body_length=length, body_truncated=length > len(body))
    return out


def _failed(found: Connected | None, exc: Exception) -> dict:
    """A tool's answer for an account that could not do it (`oncard.failed`)."""
    return oncard.failed(found, exc, word_for)


def _sender(conn, reply_to: str | None, account: str) -> tuple[Connected, str]:
    """The account a message goes from, and the native id it answers: the
    account the message being answered is in, else the one named, else the
    first. Raises NoAccount."""
    if reply_to:
        return connectors.by_id(conn, reply_to)
    chosen = connectors.named(conn, "mail", account or None)
    if not chosen:
        raise NoAccount("no mail account is connected")
    return chosen[0], ""


def read_any(message_id: str, upto: int) -> dict:
    """One email from whichever account its id came from. Blocking: it
    routes the id on a connection of its own."""
    from core import db

    conn = db.open_store()
    try:
        found, native = connectors.by_id(conn, message_id)
    except NoAccount as exc:
        return {"error": str(exc), "kind": "not_found"}
    finally:
        conn.close()
    try:
        message = connectors.blocking(found, lambda p: p.fetch(native, body_chars=upto))
    except ConnectorError as exc:
        return {"error": connectors.say(exc, word_for(found)), "kind": exc.kind}
    if message is None:
        return {"error": f"there is no such message in {word_for(found)} now; it may have "
                         "been moved or deleted", "kind": "not_found"}
    return whole(found, message)


def _send_card(found: Connected, draft: Message) -> str:
    """The card for sending a draft: everyone it reaches and what it is,
    never its body, which waits whole in their Drafts (`approvals.quoted_body`).
    Its digest binds the same: a recipient, a word or an attachment changed
    before the yes asks again (`core/writes.send_draft`)."""
    label = word_for(found)
    lines = [f"Send from your {label} account", f"To: {one_line(draft.to) or '?'}"]
    lines += [f"Cc: {one_line(draft.cc)}"] if draft.cc else []
    lines += [f"Bcc: {one_line(draft.bcc)}"] if draft.bcc else []
    lines.append(f"Subject: {one_line(draft.subject) or '?'}")
    lines += ([f"Attachments: {one_line(', '.join(draft.attachments))}"]
              if draft.attachments else [])
    return "\n".join(lines) + f"\n\nThe full email is in your {label} Drafts."


def _delete_card(found: Connected, draft: Message) -> str:
    return (f"Delete a draft in your {word_for(found)} Drafts\n"
            f"To: {one_line(draft.to) or '?'}\nSubject: {one_line(draft.subject) or '?'}")


def _email_card(head: str, to: str, subject: str) -> str:
    """A card for an email the model wrote: whom it goes to, and its
    subject, each on one line after its label (`approvals.one_line`). Taken
    as written, a subject with a line break could put lines of the model's
    own on the card, another card's answer among them (step 11's fourth
    review). The body is on no card: it waits whole in their Drafts."""
    return f"{head} {one_line(to)}\nSubject: {one_line(subject)}"


@executable("send_email")
async def _do_send_email(
    *, to: str, subject: str, body: str, reply_to_message_id: str | None = None,
    account: str = "",
) -> dict:
    """A message sent at once: run only where it is free -- a note to the
    user's own address, or a send inside an approved routine's envelope.
    Every other email goes through a draft and its card (`send_email`)."""
    from agent import approvals

    # Read here: the account's call runs in a thread of its own.
    verdict, carded = approvals.free_verdict(), approvals.from_card()
    # Again here, for a card made before the tools refused one.
    body = unsigned(body)
    if (refused := _refuse_placeholder(to, body)) is not None:
        return refused
    conn, owned = commitment_store()
    try:
        found, reply = _sender(conn, reply_to_message_id, account)
    except NoAccount as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if owned:
            conn.close()

    def go(provider) -> Message:
        connectors.require(found, classes.SEND)
        made = provider.make_draft(Draft(to=to, subject=subject, body=body, reply_to=reply))
        # The message is the call's own arguments, made and sent in one
        # turn of the account. Free, it goes on the monitor's verdict; from
        # a card (one made before asked sends went through Drafts), the card
        # showed these arguments, which is what was just made.
        return writes.send_draft(provider, made.native_id, verdict=verdict,
                                 card=made.digest() if carded else None)

    try:
        sent = await connectors.call(found, go)
    except ConnectorError as exc:
        return _failed(found, exc)
    return _sent(found, sent)


def _sent(found: Connected, sent: Message) -> dict:
    out: dict = {"sent": True, "account": found.name, "to": sent.to, "subject": sent.subject}
    if sent.native_id:
        out["id"] = found.id(sent.native_id)
    if sent.ident:
        # Its Message-ID, by which the own mailbox knows a mail the agent
        # sent, its Sent copy included (`agent/mailbox.agent_sent`).
        out["ident"] = exact.norm(sent.ident)
    if sent.thread and found.provider.has(classes.THREADS):
        out["thread_id"] = found.id(sent.thread)
    if "filed_in_sent" in sent.extra:
        out["filed_in_sent"] = sent.extra["filed_in_sent"] == "yes"
    if "draft_kept" in sent.extra:
        # It went, and its draft is still in Drafts: sending that draft
        # again would send the mail twice.
        out["draft_kept"] = sent.extra["draft_kept"]
    if sent.extra.get("refused"):
        # It went to the rest, and the server would not take it for these:
        # the user is told who did not get it, not that it went to everyone.
        out["refused"] = sent.extra["refused"]
    return out


@executable("draft_email")
async def _do_draft_email(
    *, to: str, subject: str, body: str, reply_to_message_id: str | None = None,
    account: str = "",
) -> dict:
    """A draft in the account the message being answered came to, else the
    one named.

    A reply to Outlook mail is an Outlook draft, made by Outlook's own Reply so the
    recipients and threading are what pressing Reply gives them. It used to
    go to Gmail whatever the id, which cannot find an Outlook message: every
    reply to their Outlook mail -- most of the mail that needs one -- failed.
    """
    body = unsigned(body)
    if (refused := _refuse_placeholder(to, body)) is not None:
        return refused
    conn, owned = commitment_store()
    try:
        found, reply = _sender(conn, reply_to_message_id, account)
    except NoAccount as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if owned:
            conn.close()
    draft = Draft(to=to, subject=subject, body=body, reply_to=reply)
    try:
        made = await connectors.call(found, lambda p: p.make_draft(draft))
    except ConnectorError as exc:
        return _failed(found, exc)
    out = {"drafted": True, "draft_id": found.id(made.native_id), "account": found.name,
           "to": made.to, "subject": made.subject,
           "where": made.extra.get("where") or f"{word_for(found)} Drafts"}
    if made.thread and found.provider.has(classes.THREADS):
        out["thread_id"] = found.id(made.thread)
    if made.extra.get("url"):
        out["url"] = made.extra["url"]
    return out


# How much of an email's body one `read_email` returns; a longer one is read
# on with `from_char`. The mailboxes' own default is 4,000.
READ_CHARS = 6_000


async def _send_via_drafts(
    ctx: RunContext[Deps], found: Connected, to: str, subject: str, body: str,
    reply_to_message_id: str | None,
) -> dict:
    """An email the user is asked to send: saved to its account's Drafts, then sent
    only on their yes.

    Nothing is sent without the user's explicit yes, and two steps make the
    yes an informed one: the whole email waits in their Drafts, where they
    can read or edit it, and the card asks them to send that draft as it
    stood when the card was made -- one edited since is asked about again.
    `send_draft` is gated in `core/consent.yaml` with no exception for any
    recipient. Every account goes this way; Gmail joined the others on 4
    October 2026.

    The card names the email and quotes none of it (agent/approvals.py,
    "What a card may say"). It used to quote the first 160 characters: not
    enough of it to judge the email by.
    """
    label = word_for(found)
    drafted = await act(
        ctx, "draft_email",
        {"to": to, "subject": subject, "body": body,
         "reply_to_message_id": reply_to_message_id, "account": found.name},
        preview=_email_card(f"{label} draft to", to, subject),
    )
    if not isinstance(drafted, dict) or drafted.get("error") or not drafted.get("draft_id"):
        return {"ok": False, "error": (drafted or {}).get("error", "no draft was saved"),
                "note": f"Could not save the {label} draft, so nothing was queued to send."}
    card = await _send_card_for(ctx, drafted["draft_id"])
    result = card if isinstance(card, dict) else await act(ctx, "send_draft", *card)
    if isinstance(result, dict):
        # The key is `draft_id` because a key named as an id is what gets a
        # short handle (agent/handles.py), and this one may be a long
        # Outlook id the model passes back to delete_draft.
        return {**result, "draft_id": drafted["draft_id"], "where": f"{label} Drafts"}
    return result


async def _send_card_for(ctx: RunContext[Deps], draft_id: str) -> tuple[dict, str] | dict:
    """The card for sending the draft `draft_id` names, bound to the draft
    as it is now: its arguments and what it shows, or why there is none.
    Each caller hands it to `act` itself, where the gate test can see it."""
    try:
        found, native = connectors.by_id(ctx.deps.conn, draft_id)
        connectors.require(found, classes.SEND)
        draft = await connectors.call(found, lambda p: p.fetch(native))
    except NoAccount as exc:
        return {"ok": False, "error": str(exc)}
    except ConnectorError as exc:
        return _failed(found, exc)
    if draft is None:
        return {"ok": False, "error": f"there is no such draft in {word_for(found)} now"}
    return {"draft_id": draft_id, **oncard.bound(draft)}, _send_card(found, draft)


@executable("delete_draft")
async def _do_delete_draft(*, draft_id: str, shown: str | None = None,
                           ident: str | None = None) -> dict:
    from agent import approvals

    # Read here: the account's call runs in a thread of its own. Without a
    # card, an account that cannot delete on condition needs Jev's verdict.
    verdict = approvals.free_verdict()
    said = await oncard.run(
        {"draft_id": draft_id, "shown": shown, **({"ident": ident} if ident else {})},
        "draft_id",
        lambda p, native, card, ident: writes.delete(p, native, expected=ident, card=card,
                                                     verdict=verdict),
        _delete_card, word_for)
    if "done" not in said:
        return said
    return {"deleted": True, "draft_id": draft_id, "account": said["found"].name}


@executable("send_draft")
async def _do_send_draft(*, draft_id: str, shown: str | None = None,
                         ident: str | None = None) -> dict:
    """Send a saved draft, from the account it is in, as its card showed it.
    Only ever on their yes, or inside a routine's approved envelope:
    `send_draft` is gated in `core/consent.yaml`, whoever it is to."""
    from agent import approvals

    # Read here: the account's call runs in a thread of its own.
    verdict = approvals.free_verdict()
    said = await oncard.run(
        {"draft_id": draft_id, "shown": shown, **({"ident": ident} if ident else {})},
        "draft_id",
        lambda p, native, card, ident: writes.send_draft(p, native, expected=ident, card=card,
                                                         verdict=verdict),
        _send_card, word_for)
    if "done" not in said:
        return said
    return _sent(said["found"], said["done"])


@executable("modify_email")
async def _do_modify_email(
    *, message_id: str, archive: bool = False, mark_read: bool = False, star: bool = False
) -> dict:
    try:
        found, native = oncard.routed(message_id)
    except NoAccount as exc:
        return {"ok": False, "error": str(exc)}

    def change(provider, item: Message) -> Message:
        return provider.modify(item, read=True if mark_read else None, archive=archive,
                               add_labels=("STARRED",) if star else ())

    try:
        changed = await connectors.call(
            found, lambda p: writes.modify(p, native, lambda item: change(p, item)))
    except ConnectorError as exc:
        return _failed(found, exc)
    out = {"modified": True, "message_id": message_id, "account": found.name,
           "unread": changed.unread}
    if changed.labels:
        out["labels"] = list(changed.labels)
    return out


# What `modify_email` needs of an account, by argument.
_MODIFY_NEEDS = {"archive": classes.ARCHIVE, "mark_read": classes.FLAGS, "star": classes.LABELS}


def register(tool) -> None:
    """Attach the mail tools. `tool` is `register_tools`'s filter."""

    # Every one of these returns a dict describing a failure rather than
    # raising, so "that account needs signing in again" reaches the model as
    # something it can relay instead of aborting the turn.

    @tool
    async def search_email(
        ctx: RunContext[Deps], query: str, limit: int = 10, account: str = "all"
    ) -> dict:
        """Search the user's mail. Every account unless you narrow it.

        `account` is one of the accounts the mail section lists, by its name,
        when the user named one. Otherwise leave it out: "my email" means all
        of them.

        Write the query in Gmail's syntax -- `from:sam has:attachment
        newer_than:3d`, `is:unread label:important`. An account that cannot
        honour an operator drops it rather than matching it as text, and
        says so in `ignored_terms`: that account's answer is wider than you
        asked for. Plain words match the subject, sender and recipients.

        Every account covers filed mail, not just the inbox -- a message in a
        subfolder is normal. Results carry `account`, `unread`, and where the
        account has folders the `folder` it sits in, plus an id you can hand
        straight to `read_email`. Headers and snippets only; read one for the
        body.

        When the search misses -- nothing came back, or nothing mentioning
        its telling terms -- it searches again for those terms one at a time
        (a numbered word like "325th", a date, a name) and says so in
        `widened`; a message that came from one of those says which in
        `found_by`. So do not repeat those narrower searches yourself; try
        other words, or say what was searched. `found_in` is how many each
        account found, before the list was cut to `limit`.
        """
        from agent import mailsearch

        try:
            chosen = connectors.named(ctx.deps.conn, "mail", account)
        except NoAccount as exc:
            return {"query": query, "error": str(exc)}
        said = await mailsearch.search(query, limit, chosen, shown,
                                       named=(account or "all").lower() != "all")
        said["messages"] = await screened(ctx.deps.conn, said["messages"],
                                          for_the_model(localise(said["messages"])))
        # `problems` named rather than swallowed: "I found nothing" and "one
        # of their accounts was unreachable" are answers the user would act on
        # differently.
        return said

    @tool
    async def read_email(ctx: RunContext[Deps], message_id: str, from_char: int = 0) -> dict:
        """Read one email, from any of the user's accounts.

        Pass the id exactly as a search returned it: it says which account
        to look in.

        A long email comes a part at a time. When `body_truncated` is true,
        `more` says how much is left and the `from_char` to read on from --
        do, before saying the email does not mention something.
        """

        # Every mailbox cut a body at 4,000 characters, and only Gmail said
        # so: past that, a long Outlook email could not be read at all, and a
        # model told nothing read the cut as its end.
        start = max(0, int(from_char or 0))
        upto = start + READ_CHARS
        found = await asyncio.to_thread(read_any, message_id, upto)
        if isinstance(found, dict) and "body" in found and not found.get("error"):
            found["body"] = str(found.get("body") or "")[start:]
            if start:
                found["body_from"] = start
            if found.get("body_truncated"):
                left = int(found.get("body_length") or upto) - upto
                found["more"] = (f"{left:,} more characters: read_email again with "
                                 f"from_char={upto}")
        found = await screening.mail(ctx.deps.conn, found)  # agent/screening.py
        return localise([found])[0]

    @tool
    async def send_email(
        ctx: RunContext[Deps],
        to: str,
        subject: str,
        body: str,
        reply_to_message_id: str | None = None,
        account: str = "",
    ) -> dict:
        """Queue an email for the user to approve. This does not send it.

        The user gets a card with the recipients and subject, and approves it
        to send. Say in one sentence what you queued -- who it is to and what
        it is about -- and then stop. Do not call this again for the same
        message; a second call is the same card, not a second email.

        Mail to the user's own address is not queued, because a note to
        oneself is not correspondence.

        Pass reply_to_message_id when replying, so it threads: the reply goes
        from the account the message came to. A new email goes from
        `account`, one of the accounts the mail section lists. Every email
        the user is asked about is saved to its account's drafts first, where
        they can read all of it, and the card asks them to send that draft --
        tell them it is waiting there.
        """
        from agent.actions import asks

        body = unsigned(body)
        if (refused := _refuse_placeholder(to, body)) is not None:
            return refused
        try:
            found, _ = _sender(ctx.deps.conn, reply_to_message_id, account)
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        args = {
            "to": to, "subject": subject, "body": body,
            "reply_to_message_id": reply_to_message_id, "account": found.name,
        }
        if await asks(ctx, "send_email", args):
            # Their card showed the first 180 characters as if they were the
            # email, and a yes sent the rest unread. It waits in their
            # Drafts, where the user reads all of it, and the card quotes
            # none of it. Only when the user would be asked: a note to
            # themselves, or a send their approved routine may make
            # (`core/monitor.envelope_gate`), goes at once.
            return await _send_via_drafts(ctx, found, to, subject, body, reply_to_message_id)
        return await act(ctx, "send_email", args, preview=_email_card("Email to", to, subject))

    @tool
    async def email_thread(ctx: RunContext[Deps], thread_id: str, limit: int = 20) -> dict:
        """Read a whole conversation, oldest first.

        Use this whenever the question is what was agreed, decided or promised
        rather than what one message says. `search_email` and `read_email`
        return a `thread_id` from an account that can read a conversation;
        hand it straight here rather than reading the messages one at a time.
        """
        try:
            found, native = connectors.by_id(ctx.deps.conn, thread_id)
            connectors.require(found, classes.THREADS)
            messages = await connectors.call(found, lambda p: p.thread(native, limit=limit))
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        except ConnectorError as exc:
            return _failed(found, exc)
        screened = await asyncio.gather(
            *(screening.mail(ctx.deps.conn, whole(found, m)) for m in messages))
        return {"thread_id": thread_id, "account": found.name, "count": len(screened),
                "messages": localise(list(screened))}

    @tool
    async def writing_style(ctx: RunContext[Deps], to: str = "") -> dict:
        """How the user writes email. Call it before every `draft_email`.

        Pass the recipient -- an address, or a name they have written to --
        and this returns their style guide, learned weekly from their sent
        mail, plus up to two emails they actually sent that person. Those
        beat the guide: greet, sign off and run to the length they did with
        them. With nobody to match, go by the guide. Never borrow facts from
        the examples; they show the user's voice, not what to say.
        `user_own_words`, when there, is what they have told you themselves
        about how they write, and beats both.
        """
        from agent import facts
        from tools import style

        # A name, not an address, is looked up: the ledger knows who the user writes to.
        address = _address_for(ctx.deps.conn, to)
        found = await offloop.with_own_conn(ctx.deps.conn, style.for_drafting, address)
        if found.get("sent_to_them"):
            found["sent_to_them"] = await sent_shown(ctx.deps.conn, address,
                                                     found["sent_to_them"])
        if stated := facts.stated_style(ctx.deps.conn):
            found["user_own_words"] = stated
        return found

    @tool
    async def draft_email(
        ctx: RunContext[Deps],
        to: str,
        subject: str,
        body: str,
        reply_to_message_id: str | None = None,
        account: str = "",
    ) -> dict:
        """Write an email without sending it, into the right account's drafts.

        Prefer this over pasting proposed text into the conversation. A draft
        is editable and sendable by the user wherever they read their mail; a
        paragraph in chat has to be retyped.

        Pass reply_to_message_id when replying: the reply is drafted in the
        account the message came to, addressed and threaded from the
        original -- an Outlook reply by Outlook itself, which ignores `to` and
        `subject`. A new email is drafted in `account`, one of the accounts
        the mail section lists. Say which account's drafts it is in, and give
        the user the `url` when it returns one.
        """
        body = unsigned(body)
        if (refused := _refuse_placeholder(to, body)) is not None:
            return refused
        try:
            found, _ = _sender(ctx.deps.conn, reply_to_message_id, account)
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        args = {
            "to": to, "subject": subject, "body": body,
            "reply_to_message_id": reply_to_message_id, "account": found.name,
        }
        return await act(
            ctx, "draft_email", args,
            preview=_email_card("Draft to", to, subject),
        )

    @tool
    async def delete_draft(ctx: RunContext[Deps], draft_id: str) -> dict:
        """Throw away a draft you wrote. The undo for `draft_email`.

        Use it when the user rejects a draft rather than leaving it for them
        to clear out -- and when you are replacing one, because two drafts of
        the same reply is worse than none. On an account that cannot delete
        on condition that the draft is unchanged, the user is asked first.
        """
        try:
            found, native = connectors.by_id(ctx.deps.conn, draft_id)
            draft = await connectors.call(found, lambda p: p.fetch(native))
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        except ConnectorError as exc:
            return _failed(found, exc)
        if draft is None:
            return {"ok": False, "error": f"there is no such draft in {word_for(found)} now; "
                                          "it may be gone already"}
        return await act(ctx, "delete_draft", {"draft_id": draft_id, **oncard.bound(draft)},
                         preview=_delete_card(found, draft))

    @tool
    async def send_draft(ctx: RunContext[Deps], draft_id: str) -> dict:
        """Send a draft exactly as it stands, once the user has approved it.

        Queued for their approval like any other send. The card binds the
        draft as it is when you call this: if they edit it before saying
        yes, they are asked again about their version. A draft sends from the
        account it is in -- but some mailboxes save a draft edited in their own
        app under a new id, so if this id is not found, search that account's
        drafts again rather than retrying the old id.
        """
        card = await _send_card_for(ctx, draft_id)
        return card if isinstance(card, dict) else await act(ctx, "send_draft", *card)

    @tool
    async def modify_email(
        ctx: RunContext[Deps],
        message_id: str,
        archive: bool = False,
        mark_read: bool = False,
        star: bool = False,
    ) -> dict:
        """Archive, mark read, or star a message. All of it reversible."""
        args = {
            "message_id": message_id, "archive": archive,
            "mark_read": mark_read, "star": star,
        }
        try:
            found, _ = connectors.by_id(ctx.deps.conn, message_id)
            for name, capability in _MODIFY_NEEDS.items():
                if args[name]:
                    connectors.require(found, capability)
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        except ConnectorError as exc:
            return _failed(found, exc)
        done = [k for k in ("archive", "mark_read", "star") if args[k]]
        return await act(
            ctx, "modify_email", args,
            preview=f"{', '.join(done) or 'no change'} on a message in {word_for(found)}",
        )
