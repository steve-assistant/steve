"""What closes an open item, in code, and what is open right now.

Every turn used to decide afresh, from old text, whether something was still
owed, and got it wrong in both directions. "Solve is done" (8 October 2026,
21:00) closed nothing, so the next morning's briefing said it was due today
and the model answering them reached for the nearest open thing in its
history, the Northwind email format. "both complete" (27 September) cancelled six
gym and run reminders it did not name. A thank-you was drafted once and again
eight hours later, when LinkedIn failed and the model fell back on the oldest
request in view.

So code closes what can be closed by code, and says how:

- **Their words** (`from_words`): a short message that says something is done
  or not needed, and names exactly one open item by a word only it has.
  "Solve is done" closes the Solve task; "both complete" names nothing and
  closes nothing. Ambiguity closes nothing either: the model has
  `commitment_done` for that, on their word.
- **Their answer to a card** (`keep`, `declined`).
- **A tick on their tasks list** (`taskmirror.reconcile`).

And what has gone quiet is asked about rather than guessed at
(`briefing_questions`). The user's rule, 10 October 2026: "it's a 2 way
street, it's really easy for me to say yes or no to it doing something".
The question is a card, so YES and NO are read before any model, as
approvals are.

`open_now` is what the next turn reads: the ledger's open items, active
reminders and waiting cards, and what closed in the last day with what
closed it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agent import cardwords
from tools import obligations
from tools.obligations import Obligation

log = logging.getLogger("closing")

# A message longer than this is not "X is done": it is a story with "done"
# in it, and closing an item on a story is a guess.
MAX_WORDS = 30

# The card asking about an old item. Not a tool the model holds: only code
# raises it (`briefing_questions`), and their YES runs `keep`.
KEEP_TOOL = "keep_open_item"

_DONE = re.compile(
    r"\b(?:is|are|was|were|it'?s|all)\s+(?:all\s+|now\s+|already\s+)?"
    r"(?:done|finished|submitted|complete|completed|handled|sorted|taken care of)\b"
    r"|\bi(?:'ve|\s+have)?\s+(?:already\s+|just\s+)?"
    r"(?:done|did|finished|submitted|completed|handled|sent|paid|turned in)\b"
    r"|^\s*(?:done|finished|submitted|completed|sent|paid)\b"
    r"|\b(?:done|finished|submitted|completed)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_DROP = re.compile(
    r"\b(?:never\s?mind|no longer need(?:ed)?|(?:don'?t|do not) need|no need (?:for|to)"
    r"|forget (?:about )?(?:the|it|that)|scrap (?:the|that|it))\b"
    r"|\bno\s+(?:\w+\s+){0,5}needed\b",
    re.IGNORECASE,
)
# "not done yet", "haven't finished": a done word that means the opposite.
_NEGATED = re.compile(r"\b(?:not|never|yet|almost|nearly)\b|n'?t\b", re.IGNORECASE)

# Words that say nothing about which item is meant: the verbs of doing, and
# what every task is.
_GENERIC = frozenset({
    "about", "after", "again", "also", "already", "assignment", "before", "call",
    "complete", "completed", "could", "deadline", "draft", "due", "email", "emails",
    "finish", "finished", "from", "have", "here", "into", "just", "meeting", "message",
    "need", "needed", "next", "note", "notes", "please", "reply", "really", "send",
    "sent", "should", "submit", "submitted", "task", "tasks", "thank", "thanks", "that",
    "their", "them", "then", "there", "these", "thing", "things", "this", "today",
    "tomorrow", "tonight", "week", "weekend", "what", "when", "which", "will", "with",
    "would", "your", "yours", "done", "handled", "sorted",
})
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _terms(text: str) -> set[str]:
    """Words that could pick out one item: four letters or more and not
    generic, or a number like 531."""
    out = set()
    for word in _WORD.findall((text or "").lower().replace("’", "'")):
        word = word.removesuffix("'s")
        if any(c.isdigit() for c in word):
            if len(word) >= 3:
                out.add(word)
            continue
        if len(word) >= 4 and word not in _GENERIC:
            out.add(word[:-1] if len(word) > 4 and word.endswith("s") else word)
    return out


def _item_terms(item: Obligation) -> set[str]:
    return _terms(" ".join(filter(None, (item.text, item.quote, item.person))))


def what_words_close(text: str) -> str | None:
    """'done', 'dropped', or None: whether a message of theirs could close
    something at all, before asking what."""
    text = " ".join((text or "").split())
    if not text or "?" in text or len(text.split()) > MAX_WORDS:
        return None
    if _DROP.search(text):
        return "dropped"
    if _DONE.search(text) and not _NEGATED.search(text):
        return "done"
    return None


def named(text: str, items: list[Obligation]) -> Obligation | None:
    """The one item their words name, by a word no other item has more of, or
    None when none or several are named."""
    said = _terms(text)
    scored = sorted(((len(said & _item_terms(item)), item) for item in items),
                    key=lambda pair: -pair[0])
    if not scored or scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    return scored[0][1]


def from_words(conn: sqlite3.Connection, text: str) -> list[Obligation]:
    """Close what their message says is done or not needed, when it names one
    item unmistakably. Returns what it closed (none or one)."""
    status = what_words_close(text)
    if status is None:
        return []
    item = named(text, obligations.open_ones(conn, kinds=("commitment", "task")))
    if item is None:
        return []
    said = " ".join(text.split())[:120]
    closed = obligations.close(conn, item.id, status=status, by="user words",
                               outcome=f"the user said “{said}”")
    if closed is not None:
        log.info("the user's words closed %s %s: %s", closed.kind, closed.id, said)
        untrack_soon(closed)
    return [closed] if closed is not None else []


def untrack_soon(item: Obligation) -> None:
    """Tick its task on their list without holding up their turn; the hourly
    sync ticks it if this cannot (`taskmirror.reconcile`)."""
    from agent import taskmirror

    if not taskmirror.mirrored(item.task_id):
        return
    with contextlib.suppress(RuntimeError):  # no loop: the sync will do it
        asyncio.get_running_loop().create_task(_untrack(item.task_id))


async def _untrack(task_id: str) -> None:
    from agent import taskmirror
    from agent.toolset.clients import commitment_store

    conn, owned = commitment_store()
    try:
        await taskmirror.tick(conn, task_id)
    finally:
        if owned:
            conn.close()


# ---------------------------------------------------------------------------
# How old an item is
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """The moment, as one function a test can pin."""
    return datetime.now(UTC)


def age_hours(item: Obligation, now: datetime | None = None) -> float:
    from agent import sqltime

    try:
        then = sqltime.moment(str(item.created_at))
    except (TypeError, ValueError):
        return 0.0
    return ((now or _now()) - then).total_seconds() / 3600


# ---------------------------------------------------------------------------
# Asking them
# ---------------------------------------------------------------------------


def _when(stamp: str | None) -> str:
    from agent.history import clock

    return clock(stamp, "%a %d %b, %H:%M") or "earlier"


async def keep(conn: sqlite3.Connection, obligation_id: int) -> dict:
    """Their YES to a card about an item: it is kept open. Returns what to
    tell them (`say`)."""
    item = obligations.get(conn, obligation_id)
    if item is None:
        return {"ok": False, "error": f"no item {obligation_id}"}
    obligations.reopen(conn, item.id, outcome="the user said they still want it")
    return {"ok": True, "say": f"Kept: {item.text}."}


# ---------------------------------------------------------------------------
# What to ask them about (data/audits/2026-10-10/REDESIGN.md part 5)
# ---------------------------------------------------------------------------
#
# the maintainer, 10 October 2026: "more for sure" -- not far more -- and "if it can
# understand when it needs my help and ask me". The agent had reached out only
# to remind. So code finds what has gone quiet and is worth one word from them
# -- a dated item a day past its date with no sign it was done, or an
# undated one that has sat for days -- and the next briefing carries the
# question, a card the user answers YES or NO. A few
# a day at most (`settings.MAX_QUESTIONS_PER_DAY`); no answer is not a yes,
# and an item is asked about twice at most.

# A dated item is asked about this many days after its date.
OVERDUE_GRACE_DAYS = 1
# An undated one, this long after it was kept: "Stop by if Tomas is still
# there after 9:30", from their mail on 7 October, was over the same day.
UNDATED_AFTER_DAYS = 5
# Not asked again sooner than this, nor more than this many times.
ASK_AGAIN_DAYS = 3
MAX_ASKS = 2


def _asks(conn: sqlite3.Connection, item: Obligation) -> int:
    """How many cards have asked about it."""
    from agent import approvals

    return conn.execute("SELECT COUNT(*) FROM actions WHERE args_hash = ?",
                        (approvals._hash(KEEP_TOOL, {"obligation_id": item.id}),)).fetchone()[0]


def _asked_today(conn: sqlite3.Connection, now: datetime) -> int:
    from agent import sqltime

    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute("SELECT COUNT(*) FROM actions WHERE tool = ? AND asked_at >= ?",
                        (KEEP_TOOL, sqltime.stamp(midnight))).fetchone()[0]


def worth_asking(conn: sqlite3.Connection, *, now: datetime | None = None
                 ) -> list[tuple[Obligation, str]]:
    """What has gone quiet and is worth asking them about, with why: past
    its date first, then the long undated."""
    from agent import sqltime

    now = now or _now()
    today = now.astimezone().date()
    overdue_by = (today - timedelta(days=OVERDUE_GRACE_DAYS)).isoformat()
    found: list[tuple[int, str, Obligation, str]] = []
    for item in obligations.open_ones(conn, kinds=("commitment", "task"), limit=100):
        if item.status != "open":
            continue
        if item.asked_at and now - sqltime.moment(item.asked_at) < timedelta(days=ASK_AGAIN_DAYS):
            continue
        if _asks(conn, item) >= MAX_ASKS:
            continue
        if item.due_date and item.due_date < overdue_by:
            found.append((0, item.due_date, item, f"due {item.due_date}, no sign it was done"))
        elif not item.due_date and age_hours(item, now) > UNDATED_AFTER_DAYS * 24:
            found.append((1, str(item.created_at), item,
                          f"on the list since {_when(item.created_at)}"))
    return [(item, why) for *_, item, why in sorted(found, key=lambda f: (f[0], f[1]))]


def _preview(item: Obligation) -> str:
    """The card's first line and the briefing's question, the same line.
    On one line (`approvals.one_line`): with a line break in the text, a
    "\\r" or U+2028, the card's first line stopped there, the question did
    not, and the question lost its word (step 11's fourth review)."""
    from agent.approvals import one_line

    due = f" (due {one_line(item.due_date)})" if item.due_date else ""
    return f"Still to do: “{one_line(item.text)}”{due}?"


def _question_line(item: Obligation, token: str) -> str:
    return cardwords.question(_preview(item), token)


def briefing_questions(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[str]:
    """The questions a briefing carries: those already waiting on a card,
    then new ones up to the day's cap, each a card made quietly -- the
    briefing is the message that asks. Blocking; a briefing is written on a
    thread of its own (`briefing.gather`)."""
    from agent import approvals
    from agent.attention import settings
    from core.consent import Consent, Reach, Reversal

    now = now or _now()
    lines = []
    for action in approvals.pending(conn, limit=50):
        if action.tool == KEEP_TOOL and (item := obligations.by_action(conn, action.id)):
            lines.append(_question_line(item, action.token))
    left = settings.MAX_QUESTIONS_PER_DAY - _asked_today(conn, now)
    for item, why in worth_asking(conn, now=now):
        if left <= 0:
            break
        _, made = approvals.queue(
            conn, tool=KEEP_TOOL, args={"obligation_id": item.id}, preview=_preview(item),
            consent=Consent(gated=True, reach=Reach.PRIVATE, reversal=Reversal.REVERSIBLE,
                            reasons=[why]),
            channel="signal")
        if made is None:
            continue
        obligations.asking(conn, item.id, made.id)
        lines.append(_question_line(item, made.token))
        left -= 1
    return lines


def declined(conn: sqlite3.Connection, action_id: int) -> Obligation | None:
    """Their NO to a card about an item: dropped, on their word."""
    item = obligations.by_action(conn, action_id)
    if item is None or not item.is_open:
        return None
    return obligations.close(conn, item.id, status="dropped", by="user no",
                             outcome="the user said they no longer want it")


# ---------------------------------------------------------------------------
# What is open now
# ---------------------------------------------------------------------------


@dataclass
class OpenNow:
    """What a turn is told is open, and what closed lately."""

    open: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    def render(self) -> str:
        if not self.open and not self.closed:
            return ""
        parts = []
        if self.open:
            parts.append("Open now:\n" + "\n".join(f"- {line}" for line in self.open))
        if self.closed:
            parts.append("Closed in the last day (already done; do not do again):\n"
                         + "\n".join(f"- {line}" for line in self.closed))
        return "\n\n".join(parts)


def _item_line(item: Obligation, now: datetime) -> str:
    bits = [f"[{item.kind} {item.id}] {item.text}"]
    if item.due_date:
        # Against the moment it is read at (`_now`, which the tests pin).
        today = now.astimezone().date().isoformat()
        bits.append(f"overdue since {item.due_date}" if item.due_date < today
                    else f"due {item.due_date}")
    if item.stakes == "outward":
        bits.append("would reach someone: ask the user before doing it")
    if item.status == "asked":
        bits.append("the user has a card about it and has not answered")
    if item.outcome:
        bits.append(f"last: {item.outcome}")
    return " — ".join(bits)


def _closed_line(item: Obligation) -> str:
    what = {"done": "done", "dropped": "dropped", "expired": "expired"}[item.status]
    line = f"[{item.kind} {item.id}] {item.text} — {what} {_when(item.closed_at)}"
    if item.closed_by:
        line += f", by {item.closed_by}"
    return line + (f": {item.outcome}" if item.outcome else "")


def open_now(conn: sqlite3.Connection, *, limit: int = 12) -> OpenNow:
    """Across the ledger, reminders and cards: what is open, and what closed
    in the last day."""
    now = _now()
    view = OpenNow()
    view.open += [_item_line(i, now) for i in obligations.open_ones(conn, limit=limit)]
    view.open += _reminders_open(conn, limit, now)
    view.open += _cards_open(conn, limit)
    view.closed += [_closed_line(i) for i in obligations.closed_since(conn, 24, limit=limit)]
    view.closed += _reminders_closed(conn, limit)
    view.closed += _cards_withdrawn(conn, now, limit)
    return view


# Reminders listed one by one: those going off within this many hours. A
# week of gym and run reminders in front of "yeah that's fine" invited the
# model to do something to them (E11, replayed 10 October 2026); the rest
# are counted, and `list_reminders` has them.
REMINDERS_AHEAD_HOURS = 24


def _reminders_open(conn: sqlite3.Connection, limit: int, now: datetime) -> list[str]:
    from agent import reminders

    found = reminders.active(conn, limit=50)
    # `next_fire_at` is local time, written by Python.
    horizon = (now.astimezone() + timedelta(hours=REMINDERS_AHEAD_HOURS)).replace(tzinfo=None)
    soon = [r for r in found if r.next_fire_at <= horizon.isoformat(timespec="seconds")]
    lines = [f"[reminder {r.id}] “{r.text}” — goes off {r.next_fire_at.replace('T', ' ')[:16]}"
             for r in soon[:limit]]
    if later := len(found) - len(lines):
        lines.append(f"({later} more reminder{'s' if later > 1 else ''} set for later; "
                     "list_reminders has them)")
    return lines


def _reminders_closed(conn: sqlite3.Connection, limit: int) -> list[str]:
    from agent import history

    rows = conn.execute(
        "SELECT id, text, status, finished_at FROM reminders WHERE status != 'active'"
        " AND finished_at >= datetime('now', '-24 hours') AND finished_at <= datetime('now')"
        " ORDER BY finished_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        what = {"done": "went off", "cancelled": "cancelled"}.get(row["status"], row["status"])
        when = history.local(row["finished_at"])
        out.append(f"[reminder {row['id']}] “{row['text']}” — {what}"
                   + (f" {when:%a %H:%M}" if when else ""))
    return out


def _cards_withdrawn(conn: sqlite3.Connection, now: datetime, limit: int) -> list[str]:
    """Cards withdrawn in the day before `now`, because their account was
    switched off: Steve's next turn knows they will not happen, and why."""
    here = now.astimezone().replace(tzinfo=None) if now.tzinfo else now
    since = (here - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT token, tool, preview, withdrawn_reason FROM actions WHERE status = 'withdrawn'"
        " AND decided_at > ? AND decided_at <= ? ORDER BY id LIMIT ?",
        (since, here.isoformat(timespec="seconds"), limit)).fetchall()
    return [f"[card] {(r['preview'] or r['tool']).splitlines()[0]}"
            f" — withdrawn, not done: {r['withdrawn_reason'] or 'its account was turned off'}"
            for r in rows]


def _cards_open(conn: sqlite3.Connection, limit: int) -> list[str]:
    from agent import approvals

    found = approvals.pending(conn, limit=limit)
    # Without its word, which only the card says (agent/cardwords.py).
    return [f"[card] {(a.preview or a.tool).splitlines()[0]} — waiting for their yes or no"
            for a in found]
