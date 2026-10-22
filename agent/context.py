"""One context for every turn of theirs, rebuilt from the database each time.

What the model read used to depend on how long the worker had been up. Live,
`history.compact` kept fifteen turns with no age limit and the last one whole,
its tool results and its old clock line included; after a restart `_resume`
kept a day, thirty messages and eight pings. On 8 October 2026 at 21:00 the
day's replay held seventeen items, nine of them the Northwind/Owen saga, and the
briefing the user was answering was not one of them: it went at 21:00:00, was
stored at 21:05, and their 21:00:33 "Solve is done" sat before the message it
answered. The model wrote them a note
about Northwind's email format (E2); their "why did you say that?" was then read as
being about the briefing, which by then came after it (E3). Signal's history
was 83% pings, each with two stamps from two writers.

So the history a turn reads is built here, the same live or after a restart:

1. **By when it was said**: each message is stored as it is said, and a
   message to them once it is confirmed sent.
2. **Their last `RECENT_EXCHANGES` exchanges verbatim**, each with when the user
   asked, what its tools did (`history.receipt`), and, for the latest, a
   digest of what its tools found, for a follow-up about it.
3. **Everything older in the day as one settled block**, one line an item in
   one label format, said to be settled: reference, not requests.
4. **Open now** (`closing.open_now`), after their message rather than before
   the history: what is owed and what closed in the last day, by whom.

`history` and `replying_to` take the moment to read as of (`at`), now when
not given: the tests' clock.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from agent import history as hist
from agent.cut import cut
from agent.history import UNPROMPTED
from core.call_policy import PRESENT_CHANNELS

log = logging.getLogger("context")

def builds_here(channel: str) -> bool:
    """Whether a turn on `channel` reads the built context on this machine,
    by default. Only their own conversations (`PRESENT_CHANNELS`): a background
    job or the night keeps the history of the run it is in."""
    return channel in PRESENT_CHANNELS

# How far back a turn reads. The user asked for "a 24 hour rolling window".
WINDOW_HOURS = 24
# Their exchanges read word for word -- the last few, and only from the last
# few hours; the rest of the day is settled. At 21:00 on 8 October the last
# three of their messages went back to 16:18, and two of them were the Owen
# draft the user had already told it off for.
RECENT_EXCHANGES = 3
RECENT_HOURS = 3
# How far apart the told ledger's moment and the conversation's may be for
# one message sent them: each is written once the send is confirmed.
STORED_LATE = timedelta(minutes=15)
# What was sent to them this shortly before the user spoke is what they are answering.
SENT_JUST_BEFORE = timedelta(minutes=30)
# The settled block: at most this many lines, the newest kept, each cut.
SETTLED_MAX = 30
SETTLED_CHARS = 220
# A recent message, at most (a long briefing is cut with a mark).
RECENT_CHARS = 3_000
# What the latest exchange's tools found, for "which of those two is
# closer?": a digest of each, and of all of them together.
FOUND_CHARS = 1_200
FOUND_TOTAL = 5_000

SETTLED_HEAD = ("[Earlier in this conversation, the last day -- settled. For reference only: "
                "none of it is a request now, and what is still open is listed with the "
                "user's message under Open now.]")

# A reminder's own stamp, which says what went off.
_WENT_OFF = re.compile(r"^\[(reminder|check) (\d+) went off\]\s*")


@dataclass
class Item:
    """One thing said in the conversation, placed when it was said."""

    at: datetime                 # aware
    kind: str                    # user | reply | sent | tool
    text: str
    order: int = 0               # its id, to keep a stable order at one moment
    tool: str | None = None
    args: str | None = None
    note: str = ""               # "reminder 12 went off"
    came_from: str = ""          # a ping's provenance: "weather, about Ridgeview, WA"


def _moment(stamp: str | None) -> datetime | None:
    from agent import sqltime

    try:
        return sqltime.moment(str(stamp)) if stamp else None
    except (TypeError, ValueError):
        return None


def _items(conn: sqlite3.Connection, session_id: int, *, at: datetime,
           exclude_id: int | None) -> list[Item]:
    """What was said on this session in the window before `at`, in the order
    it was said."""
    from agent import sqltime

    since = at - timedelta(hours=WINDOW_HOURS)
    rows = conn.execute(
        "SELECT id, role, content, model, tool_name, tool_args, created_at FROM messages"
        " WHERE session_id = ? AND created_at >= ? AND created_at <= ? AND id IS NOT ?"
        " ORDER BY id",
        (session_id, sqltime.stamp(since), sqltime.stamp(at), exclude_id)).fetchall()
    items: list[Item] = []
    for row in rows:
        stored = _moment(row["created_at"])
        if stored is None:
            continue
        text = row["content"] or ""
        if row["role"] == "tool":
            items.append(Item(stored, "tool", text, row["id"], row["tool_name"],
                              row["tool_args"]))
            continue
        if row["role"] == "user":
            items.append(Item(stored, "user", text, row["id"]))
            continue
        if row["model"] == UNPROMPTED:
            note = ""
            if found := _WENT_OFF.match(text):
                note = f"{found.group(1)} {found.group(2)} went off"
                text = text[found.end():]
            items.append(Item(stored, "sent", text, row["id"], note=note))
        else:
            items.append(Item(stored, "reply", text, row["id"]))
    items = [i for i in items if since <= i.at <= at]
    _came_from(conn, items)
    return sorted(items, key=lambda i: (i.at, i.order))


def _came_from(conn: sqlite3.Connection, items: list[Item]) -> None:
    """Where each ping and pre-event brief came from, on its item: the
    source, the place and the judge's reason the attention loop kept with
    every line it sent (agent/attention/told.py). At 00:24 on 9 October the user
    asked "why do you think I'm in Ridgeview?" about a ping sent at 23:03,
    and nothing a turn could read said where it came from. A briefing is
    left as it is: a line for each of its items would be most of it."""
    from agent import sqltime
    from agent.attention.told import OPENING_CHARS

    sent = [i for i in items if i.kind == "sent"]
    if not sent:
        return
    try:
        rows = conn.execute(
            "SELECT said_at, line, source, place, reason FROM told"
            " WHERE how IN ('ping', 'before') AND said_at >= ? AND said_at <= ?",
            (sqltime.stamp(min(i.at for i in sent) - STORED_LATE),
             sqltime.stamp(max(i.at for i in sent) + STORED_LATE))).fetchall()
    except sqlite3.Error:
        return
    for item in sent:
        whence = []
        for row in rows:
            said = _moment(row["said_at"])
            if said is None or abs(said - item.at) > STORED_LATE \
                    or (row["line"] or "")[:OPENING_CHARS] not in item.text:
                continue
            whence.append((row["source"] or "not an observation")
                          + (f", about {row['place']}" if row["place"] else "")
                          + (f", judged {row['reason']}" if row["reason"] else ""))
        item.came_from = "; ".join(dict.fromkeys(whence))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _short(at: datetime) -> str:
    return at.astimezone(hist._zone()).strftime("%a %H:%M")


def _who(item: Item) -> str:
    if item.kind == "user":
        return "the user"
    if item.kind == "reply":
        return "you"
    about = "; ".join(x for x in (item.note, f"from {item.came_from}" if item.came_from else "")
                      if x)
    return "you, unprompted" + (f" ({about})" if about else "")


def _line(item: Item) -> str:
    text = " ".join(hist.as_asked(item.text).split())
    text = re.sub(r"\s*\[asked [^\]]*\]\s*$", "", text)
    return f"- {_short(item.at)} · {_who(item)}: {cut(text, SETTLED_CHARS)}"


def _settled(items: list[Item]) -> list[ModelMessage]:
    """Everything before the recent exchanges, as one block of lines."""
    lines = []
    for item in items:
        if item.kind == "tool":
            if item.tool in hist.RECEIPT_TOOLS:
                lines.append(f"  {hist.receipt(item.tool, item.args, item.text)}")
            continue
        lines.append(_line(item))
    if not lines:
        return []
    lines = lines[-SETTLED_MAX:]
    return [ModelRequest(parts=[UserPromptPart(content=SETTLED_HEAD + "\n" + "\n".join(lines))])]


# Tools whose result is bookkeeping, not something found.
_NOT_FOUND = frozenset({"note_feedback", "escalate", "remember", "forget_memory"})


def _found(tools: list[Item]) -> str:
    """What the latest exchange's tools found, each cut, for a follow-up."""
    parts, used = [], 0
    for item in tools:
        if item.tool in hist.RECEIPT_TOOLS or item.tool in _NOT_FOUND or not item.text.strip():
            continue
        piece = f"{item.tool}: {cut(' '.join(item.text.split()), FOUND_CHARS)}"
        if used + len(piece) > FOUND_TOTAL:
            break
        parts.append(piece)
        used += len(piece)
    return ("[what your tools found for this, cut short: " + " | ".join(parts) + "]"
            if parts else "")


def _headline_if_report(text: str) -> str:
    """A report about the agent itself, kept to its headline
    (`history.SELF_REPORT`): one "Upgrade #88 did not go in" with its pytest
    output was 1,414 characters of a day's history on 7 October."""
    if hist.SELF_REPORT.match(text):
        return cut(text.split("\n\n", 1)[0], hist.SELF_REPORT_CHARS,
                   mark=" […the rest of this report is not replayed]")
    return text


def _recent(items: list[Item]) -> list[ModelMessage]:
    """The recent exchanges as they were said: their words with when, the
    replies, and what was sent to them between them."""
    out: list[ModelMessage] = []
    last_theirs = max((n for n, i in enumerate(items) if i.kind == "user"), default=-1)
    request: ModelRequest | None = None
    tools: list[Item] = []
    for n, item in enumerate(items):
        if item.kind == "tool":
            if request is not None:
                if item.tool in hist.RECEIPT_TOOLS:
                    request.parts.append(UserPromptPart(
                        content=hist.receipt(item.tool, item.args, item.text)))
                if n > last_theirs:
                    tools.append(item)
            continue
        text = cut(item.text, RECENT_CHARS, mark=" […{n:,} more characters, not replayed]")
        if item.kind == "user":
            if "[asked " not in text:
                text = hist.asked(text, item.at.astimezone(hist._zone()))
            request = ModelRequest(parts=[UserPromptPart(content=text)])
            out.append(request)
        elif item.kind == "sent":
            when = item.at.astimezone(hist._zone()).strftime(hist.WHEN)
            how = f", {item.note}" if item.note else ""
            how += f"; from {item.came_from}" if item.came_from else ""
            out.append(ModelResponse(parts=[TextPart(
                content=f"[sent unprompted, {when}{how}] {_headline_if_report(text)}")]))
        else:
            out.append(ModelResponse(parts=[TextPart(content=text)]))
    if tools and (found := _found(tools)):
        last = next((m for m in reversed(out) if isinstance(m, ModelRequest)), None)
        if last is not None:
            last.parts.append(UserPromptPart(content=found))
    return out


def history(conn: sqlite3.Connection, session_id: int, *, at: datetime | None = None,
            exclude_id: int | None = None) -> list[ModelMessage]:
    """The history a turn on this session reads, as of `at` (now), without
    the message it answers (`exclude_id`)."""
    at = at or datetime.now(UTC)
    items = _items(conn, session_id, at=at, exclude_id=exclude_id)
    lately = at - timedelta(hours=RECENT_HOURS)
    theirs = [n for n, i in enumerate(items) if i.kind == "user" and i.at >= lately]
    start = theirs[-RECENT_EXCHANGES:][0] if theirs else len(items)
    # What was sent to them shortly before the first recent thing -- their word,
    # or the message this turn answers -- is what that answers, and is read
    # whole: "Solve is done" came 33 seconds after the evening briefing.
    anchor = items[start].at if start < len(items) else at
    while start > 0 and items[start - 1].kind == "sent" \
            and anchor - items[start - 1].at <= SENT_JUST_BEFORE:
        start -= 1
    return _settled(items[:start]) + _recent(items[start:])


def open_now(conn: sqlite3.Connection) -> str:
    """The Open now block for their message, or "" when nothing is open or
    lately closed."""
    from agent import closing

    try:
        return closing.open_now(conn).render()
    except Exception:  # noqa: BLE001 - a turn is never lost to its context
        log.warning("could not say what is open", exc_info=True)
        return ""


OPEN_HEAD = ("(Not the user's words: what the ledger says is open or lately closed. Act "
             "on their message; touch these only when it points at one.)")

# Before the user has slept, "tomorrow" is the coming daytime. At 00:26 on Friday 9
# October the user wrote "ropes course tomorrow" about Friday's, and was given
# Saturday's weather (E16).
SMALL_HOURS_END = 5


def small_hours(now: datetime) -> str:
    """In the small hours, what "tomorrow" probably means; "" by day."""
    here = now.astimezone(hist._zone())
    if here.hour >= SMALL_HOURS_END:
        return ""
    return (f" It is past midnight and the user may not have slept: by \"tomorrow\" or "
            f"\"in the morning\" they most likely mean today, {here:%A %d %B}, not "
            f"{here + timedelta(days=1):%A}. Where the day matters, check what is on "
            "that day before choosing.")


def clock_line(now: datetime) -> str:
    """The clock their message carries (`loop.now_line_at`) at `now`, and in
    the small hours what "tomorrow" probably means."""
    from agent.loop import now_line_at

    return now_line_at(now.astimezone(hist._zone())) + small_hours(now)


def turn_text(text: str, *, clock: str, open_block: str = "",
              replying: str = "", facts: str = "", unchecked: str = "") -> str:
    """Their message as the model reads it: their words, the clock, the
    accounts Steve could not check, what the user is most likely replying
    to, the facts their words call for (agent/facts.py `relevant`), then
    Open now.

    The facts after their words, not in the instructions: they change with
    every message, and in front of the history they would break its cache
    each turn (see the clock, `agent/loop.py`)."""
    from agent.facts import RELEVANT_HEAD

    out = f"{text}\n\n[{clock}]"
    if unchecked:
        out += f"\n[Accounts Steve could not check: {unchecked}]"
    if replying:
        out += f"\n[{replying}]"
    if facts:
        out += f"\n\n{RELEVANT_HEAD}\n{facts}"
    if open_block:
        out += f"\n\n{OPEN_HEAD}\n{open_block}"
    return out


# How much of the message the user is replying to the note quotes.
REPLYING_CHARS = 160


def replying_to(conn: sqlite3.Connection, session_id: int, *, at: datetime | None = None,
                exclude_id: int | None = None) -> str:
    """What their message most likely answers, when code can tell: the last
    thing said in the conversation was sent to them unprompted, minutes ago.
    "yeah that's fine" came a minute after a Google security alert, with a
    7:15 call arranged twenty minutes before it in view, and replayed it
    cancelled the call (E11); "Solve is done" came 33 seconds after the
    evening briefing (E2). "" when the last word was a reply, or long ago."""
    at = at or datetime.now(UTC)
    items = [i for i in _items(conn, session_id, at=at, exclude_id=exclude_id)
             if i.kind != "tool"]
    if not items or items[-1].kind != "sent" or at - items[-1].at > SENT_JUST_BEFORE:
        return ""
    last = items[-1]
    minutes = max(0, round((at - last.at).total_seconds() / 60))
    said = cut(" ".join(_headline_if_report(last.text).split()), REPLYING_CHARS)
    whence = f" (from {last.came_from})" if last.came_from else ""
    return (f"The user wrote this {minutes} min after you sent them, unprompted{whence}: "
            f"“{said}” -- "
            "most likely their reply to that, unless their words say otherwise.")
