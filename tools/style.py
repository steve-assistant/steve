"""How the user writes, so a draft the agent leaves them reads as theirs.

A draft in the model's own voice -- "I hope this email finds you well", a
sign-off the user never uses, three paragraphs where the user writes two lines -- is a
draft the user rewrites, and a draft the user rewrites saved them nothing. The user asked for
drafts "more in my voice" on 24 September 2026.

Two halves, both from their *sent* mail only (`sent_mail`). That is the
invariant commitment extraction keeps (`tests/gate/test_sent_mail_only.py`),
and for the same reason: their own words are the only mail text here that
reaches a model unredacted.

- `sample` and `save` are the night's (`night.learning._learn_style`, weekly): about
  forty recent sent emails become a short style guide, kept as the memory
  `reference/writing-style.md`. A `reference` and not a `user` memory, so the
  attention judge, which reads what is known about them, is not handed a page
  about greetings with every batch.
- `for_drafting` is what the `writing_style` tool returns before a draft: the
  guide, and up to two emails the user actually sent that person. Those beat any
  guide -- how the user greets Jane is shown rather than described.

Quoted text is cut before anything is used: a sent reply carries the email it
answers, and a guide learned from that is a guide to other people's voices.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3

log = logging.getLogger("tools.style")

NAME = "writing-style"
PATH = f"reference/{NAME}.md"
CURSOR = "style"
# A voice does not change overnight, and relearning it nightly would be most
# of what the learning block spends.
EVERY_DAYS = 7
# Read once per draft, not with every prompt, so it can afford room; the
# first real guide ran to 1,800 characters of rules worth keeping.
MAX_GUIDE = 2000
SAMPLE = 40
PER_MESSAGE = 900
EXAMPLES = 2
EXAMPLE_CHARS = 800
# How far back to look for mail to one person.
EXAMPLE_DAYS = 180

# Where the email the user was answering begins. Gmail and Apple Mail write "On
# <date>, <someone> wrote:" (wrapped over two lines when the name is long);
# Outlook writes a rule and a From/Sent block, or the block alone.
_QUOTED = re.compile(
    r"^(?:On [\s\S]{4,300}?wrote:[ \t]*$"
    r"|-{2,}\s*Original Message\s*-{2,}"
    r"|_{8,}[ \t]*$"
    r"|From: .+\n(?:Sent|Date): "
    r"|Sent from my \w+)",
    re.IGNORECASE | re.MULTILINE,
)
_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def own_words(body: str) -> str:
    """What the user wrote, without the email they were answering."""
    text = (body or "").replace("\r\n", "\n")
    if match := _QUOTED.search(text):
        text = text[: match.start()]
    lines = [line for line in text.split("\n") if not line.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def address_in(text: str) -> str:
    """The first email address in `text`, and nothing else from it.

    What goes into the Gmail query is this and only this, so a recipient
    written as "jane@northwind.example OR in:inbox" cannot turn a search of their sent
    mail into a search of everything.
    """
    found = _ADDRESS.search(text or "")
    return found.group(0).lower() if found else ""


# ---------------------------------------------------------------------------
# Learning, weekly
# ---------------------------------------------------------------------------


def sample(messages: list[dict], limit: int = SAMPLE) -> tuple[str, int]:
    """Their sent emails as the learner sees them, and how many there are.

    The To line is kept because tone follows audience -- a landlord is
    written to differently from a sister -- and the guide is asked to say
    how.
    """
    blocks = []
    for message in messages:
        words = own_words(message.get("body") or "")
        if not words:
            continue  # a forward with no note of their own
        blocks.append(
            f"To: {message.get('to') or '?'}\n"
            f"Subject: {message.get('subject') or ''}\n\n{words[:PER_MESSAGE]}"
        )
        if len(blocks) >= limit:
            break
    return "\n\n=====\n\n".join(blocks), len(blocks)


def due(conn: sqlite3.Connection, today: dt.date) -> bool:
    """Is the guide missing, or a week old?"""
    from tools import memory, people

    learned = people.cursor(conn, CURSOR)
    if not learned or memory.read(PATH) is None:
        return True
    try:
        return (today - dt.date.fromisoformat(learned)).days >= EVERY_DAYS
    except ValueError:
        return True


def trimmed(guide: str) -> str:
    """At most `MAX_GUIDE` characters, cut at a line so no rule is half-said."""
    guide = guide.strip()
    if len(guide) <= MAX_GUIDE:
        return guide
    cut = guide.rfind("\n", 0, MAX_GUIDE)
    return guide[: cut if cut > 0 else MAX_GUIDE].rstrip()


def save(conn: sqlite3.Connection, guide: str, today: dt.date) -> None:
    """Keep the guide, overwriting last week's in place."""
    from tools import memory, people

    guide = trimmed(guide)
    memory.remember(
        conn, kind="reference", name=NAME,
        summary="How the user writes email: greetings, sign-offs, length and tone",
        body=guide,
    )
    people.set_cursor(conn, CURSOR, today.isoformat(), note=f"{len(guide)} chars")


def guide() -> str:
    from tools import memory

    found = memory.read(PATH)
    return found.body if found else ""


# ---------------------------------------------------------------------------
# Before a draft
# ---------------------------------------------------------------------------


def sent_mail(conn: sqlite3.Connection, *, days: int, limit: int, to: str = "") -> list[dict]:
    """What the user sent in the last `days` days, up to `limit` from each
    mail account that can say, newest first, with bodies. Blocking.

    Through `MailProvider.sent_items`, the one read bound to the sent
    folder: the night's learning and a draft's examples go to a model, and
    `tests/gate/test_sent_mail_only.py` holds them to it. With `to`, only
    what went to that address, checked here as well as asked of the
    account. An account that cannot list its sent mail is passed over, and
    so is one that fails: one account's trouble is not every account's.
    Each message is known by its slot id, so what is learned from it names
    its account and goes when the account does.

    Until 14 October 2026 this was Gmail's `in:sent` query and Outlook's
    Sent Items, read separately here and in the night.
    """
    from agent import connectors
    from steve_sdk.errors import ConnectorError

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    found: list[dict] = []
    for account in connectors.accounts(conn, "mail"):
        try:
            sent = connectors.blocking(
                account, lambda p: p.sent_items(since=since, limit=limit, to=to), limit_s=None)
        except ConnectorError as exc:
            log.info("no sent mail from %s: %s", account.name, exc)
            continue
        except Exception:  # noqa: BLE001 - one account failing is not every one
            log.exception("could not read sent mail from %s", account.name)
            continue
        for message in sent:
            if not message.native_id or to.lower() not in message.to.lower():
                continue
            stamp = message.when
            found.append({
                "id": account.id(message.native_id),
                "account": account.name,
                "to": message.to,
                "subject": message.subject,
                "body": message.body or "",
                "internal_date": stamp,
                "date": dt.datetime.fromtimestamp(stamp / 1000).strftime("%Y-%m-%d")
                if stamp else "",
            })
    found.sort(key=lambda m: m["internal_date"], reverse=True)
    return found


def examples(conn: sqlite3.Connection, address: str, limit: int = EXAMPLES) -> list[dict]:
    """Up to `limit` emails the user sent to this address, newest first, their words only.

    From their sent mail only (`sent_mail`), never a search of the inbox:
    what is returned goes to a model.
    """
    address = address_in(address)
    if not address:
        return []

    out = []
    for message in sent_mail(conn, days=EXAMPLE_DAYS, limit=limit, to=address):
        words = own_words(message["body"])
        if not words:
            continue
        out.append({"subject": message["subject"], "sent": message["date"],
                    "text": words[:EXAMPLE_CHARS]})
        if len(out) >= limit:
            break
    return out


def for_drafting(conn: sqlite3.Connection, address: str = "") -> dict:
    """The guide, and what the user last wrote to this person. Blocking."""
    out: dict = {"guide": guide()}
    if not out["guide"]:
        out["note"] = "No style guide learned yet; go by the examples, if any."
    if address_in(address):
        try:
            out["sent_to_them"] = examples(conn, address)
        except Exception as exc:  # noqa: BLE001 - a draft without examples still beats none
            log.warning("could not read sent mail for %s: %s", address, exc)
            out["sent_to_them"] = []
            out["problem"] = f"could not read the user's sent mail: {type(exc).__name__}"
    return out
