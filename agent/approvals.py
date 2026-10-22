"""Things the agent wants to do, and how the user says yes.

The classifier in `core/consent.py` decides what needs asking. This is the
asking: a ledger, a card on their phone, and the machinery to run the thing
afterwards exactly once.

**The turn ends; the action fires later.** A tool that needs approval returns
immediately with a receipt, the conversation finishes normally, and the work
happens when the user taps. Blocking the tool while waiting was considered and
rejected outright: a turn held open for a reply that may come tomorrow holds
everything behind it. This is not a new mechanism anyway:
`defer_to_background` already works exactly this way.

**Exactly once, under concurrency.** A tray click and a Signal "yes" can
arrive together. `approved -> executing` is claimed with the same
`UPDATE ... WHERE status = ? RETURNING *` idiom as `jobs.claim`, so whichever
transaction commits first owns the action and the other sees nothing to do.
That one statement is the entire defence against sending an email twice.

**Free actions are recorded too.** They go straight to `done`, or to `failed`
when the executor reported a failure. A log of only the things you were
already watching is not a log, and a log that calls a failure done is worse.

**Approval is never expressible as model output.** The reply intercept runs
before the model sees the message, so nothing said in a conversation can
approve anything -- including anything the model itself writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import re
import sqlite3
import unicodedata
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import groupby

from agent import cardwords
from core import db, modules, unseen
from core.consent import TOKEN_WORDS, Consent, spend_caps, token_for

log = logging.getLogger("approvals")

# How long a card stays answerable. Generous because the laptop -- which is
# the only thing that can execute -- may be asleep when the user reads it, and a
# card that expired while they were on a train is worse than one that lingered.
TTL = timedelta(hours=12)

# Between approval and execution, for anything that cannot be taken back.
# It turns the worst irreversible action into a reversible one for free, and
# it is how every mail client the user already uses behaves.
HOLD_SECONDS = 30
# Past its hold and this many seconds more, an executing card may have begun
# its call though nothing marked it (`_calling`): a worker from before the
# mark never sets it, nor was it set on a card executing when migration 25
# came (step 11's fourth review). So it is taken back no later.
CALL_MARGIN_SECONDS = 5
# An executing card whose call has not begun, which UNDO and an unlink may
# still take back: unmarked by `_calling`, and claimed (`executed_at`) no
# longer ago than `_held_since`. A row with no `executed_at` is not taken.
_UNCALLED = ("(status = 'executing' AND called_at IS NULL"
             " AND datetime(executed_at) > datetime('now', ?))")


def _held_since() -> str:
    """SQLite's modifier for the oldest claim still inside its hold and
    margin, read at each call, as tests shorten the hold."""
    return f"-{HOLD_SECONDS + CALL_MARGIN_SECONDS} seconds"


# What a card may say, as a rule about what it says rather than its length.
#
# A card shows whole what the user is agreeing to -- whom it reaches, when, the
# values a form will send, a call's script, the code that will run --
# however long; Signal carries a long message as an attachment. It never
# quotes the body of a message it would send: an email the user is asked to send
# waits whole in their Drafts, where the user reads and can edit it, and its card
# names it (`toolset/mail._send_via_drafts`). `quoted_body` holds every card
# to that.
#
# Until 4 October 2026 every card was cut at 300 characters instead, "a
# security limit, not a formatting one": the card crossed a server in
# plaintext, and a whole email body must not. A length was the wrong tool
# for it. It still put an email's first 180 characters on that server, and it
# cut everything else the user had to read whole: a call's script lost its last
# line, a routine's recipients, an event's guests, a form's values.
QUOTE_CHARS = 30


# Quotation marks that are not opening or closing punctuation by their
# Unicode category (Pi, Pf), each folded to a straight single one by
# `one_line`, as those are.
_QUOTES = frozenset('"\u201e\u201a\u201f\u201b\u301d\u301e\u301f'
                    "\u2032\u2033\u2035\u2036\u02ba\u02dd\u2e42\uff02\uff07'`\u00b4")


def one_line(text: str) -> str:
    """Model-written text for a card: on one line, every quotation mark a
    straight single one, and nothing the eye does not see.

    A routine's card quoted its instruction as written: one that closed the
    quote and wrote "Each run may, without asking: ..." on a line of its own
    read as the card itself (step 11's review, D10). Its second review found
    the same through the schedule's condition, through quotation marks other
    than the two folded then, and through characters that are not shown or
    that turn the text around (control and format characters, U+202E,
    U+200B), and its third review the ones shown as nothing outside those
    (core/unseen.py). So the card's first line is what the yes allows,
    which the model writes none of, and each part the model wrote is on a
    labelled line of its own, through this. A routine or goal holding such
    a character is refused when it is made (agent/routines.py), since what
    the card cannot show, the runs still read."""
    kept = []
    for ch in unicodedata.normalize("NFKC", text or ""):
        if ch.isspace():
            kept.append(" ")
        elif unseen.unseen(ch):
            continue
        elif ch in _QUOTES or unicodedata.category(ch) in ("Pi", "Pf"):
            kept.append("'")
        else:
            kept.append(ch)
    return " ".join("".join(kept).split())


def quoted_body(preview: str, args: dict) -> str | None:
    """The line of a message body this card would quote, if any.

    A message's body is its `body` argument; a line of it at least
    `QUOTE_CHARS` long found on the card is a quote. Shorter lines -- "Hi
    Ann," and "Thanks, Sam" -- say nothing, and are as likely on any card.
    """
    body = args.get("body")
    if not isinstance(body, str):
        return None
    for line in body.splitlines():
        line = line.strip()
        if len(line) >= QUOTE_CHARS and line in preview:
            return line
    return None


# Writes a card to the outbox (agent/worker/links.notify_user, as a card):
# the text, and the action it asks about, so an answered card is not sent.
Notifier = Callable[..., Awaitable[int | None]]
Executor = Callable[[str, dict], Awaitable[dict]]

_notify: Notifier | None = None
_execute_tool: Executor | None = None
# Whether an answer recorded as a channel's still counts (`decide`).
_answers_from: Callable[[str], bool] | None = None


def set_notifier(fn: Notifier) -> None:
    """Injected from the worker's lifespan (agent/server.py), not imported:
    so this module imports neither Signal nor the toolset."""
    global _notify
    _notify = fn


def set_executor(fn: Executor) -> None:
    global _execute_tool
    _execute_tool = fn


def set_answers_from(fn: Callable[[str], bool]) -> None:
    """Injected as the notifier is: whether a channel is linked now
    (agent/worker/links.answers_from)."""
    global _answers_from
    _answers_from = fn


# ---------------------------------------------------------------------------
# A turn's cards
# ---------------------------------------------------------------------------


@dataclass
class _Turn:
    """The cards a turn has made and not yet asked."""

    made: list[int]
    open: bool = True


# Set by `held` for the length of a turn. A task the turn starts copies it,
# and may outlive the turn: so it says whether the turn is still open, and a
# card made once it is not is asked at once rather than held for an end that
# has passed.
_TURN: ContextVar[_Turn | None] = ContextVar("approvals_turn", default=None)
# True while the action of a card the user said yes to runs (`execute`).
_FROM_CARD: ContextVar[bool] = ContextVar("approvals_from_card", default=False)
# The monitor's verdict while a free action runs (`actions.run_free`): what
# `core/writes.send_draft` sends without a card on, and nothing else does.
_FREE: ContextVar[object | None] = ContextVar("approvals_free", default=None)

# A grouped card's arguments: {"grouped": [{"args": ..., "preview": ...}, ...]}.
GROUPED = "grouped"

# The cards being asked now. The event loop keeps only a weak reference to a
# task, so one nothing else holds can be collected before it runs: a card
# made, never shown.
_ASKING: set[asyncio.Task] = set()
# The cards made inside a turn still open (`held`), by id.
_HELD: set[int] = set()


def _ask_soon(action: Action | None) -> None:
    """`_ask`, not awaited: the turn must not be held open by a Signal round trip."""
    with contextlib.suppress(RuntimeError):
        task = asyncio.get_running_loop().create_task(_ask(action))
        _ASKING.add(task)
        task.add_done_callback(_ASKING.discard)


async def asked() -> None:
    """Wait for the cards being asked now: for a caller, such as a test,
    that reads what the user was shown."""
    while _ASKING:
        await asyncio.gather(*_ASKING, return_exceptions=True)


def from_card() -> bool:
    """Whether the action running now is one the user said yes to on its
    card, not a free one. On an account that cannot make a write conditional,
    the yes is the condition (`core/writes.py`, step 4): an executor passes
    the card's digest on only then."""
    return _FROM_CARD.get()


def free_verdict() -> object | None:
    """The monitor's verdict (`core.monitor.Gate`) that the action running
    now is free, or None: from a card, or anywhere `run_free` did not start."""
    return _FREE.get()


@contextlib.contextmanager
def running_free(verdict: object):
    """What runs inside is the free action `verdict` let through."""
    token = _FREE.set(verdict)
    try:
        yield
    finally:
        _FREE.reset(token)


@contextlib.contextmanager
def approved():
    """What runs inside runs as the action of a card the user said yes to
    (`execute`, and a test standing in for it)."""
    token = _FROM_CARD.set(True)
    try:
        yield
    finally:
        _FROM_CARD.reset(token)


@contextlib.asynccontextmanager
async def held(conn: sqlite3.Connection):
    """Hold the cards a turn makes until it ends, then ask each once.

    Cleaning up five drafts on an account without conditional deletes was
    five cards: five messages on their phone, each answered by its own word.
    Held to the turn's end, the cards one turn makes for the same tool and
    account become one (`queue`) before the user has seen any of them -- a
    card that grew after they read it would take a yes to what they had not
    read."""
    turn = _Turn(made=[])
    token = _TURN.set(turn)
    try:
        yield
    finally:
        turn.open = False
        _TURN.reset(token)
        _HELD.difference_update(turn.made)
        for action_id in dict.fromkeys(turn.made):
            action = get(conn, action_id)
            if action is not None and action.status == "pending":
                _ask_soon(action)


def held_now() -> frozenset[int]:
    """The cards open turns hold, not yet asked. The phone never sees one
    before its turn ends; the app's notes, reading every card pending, showed
    one mid-turn, and a yes to it took the items joined after."""
    return frozenset(_HELD)


def _open_turn() -> _Turn | None:
    turn = _TURN.get()
    return turn if turn is not None and turn.open else None


def items(action: Action) -> list[dict]:
    """A card's items, each {"args", "preview"}: one, unless grouped."""
    if isinstance(action.args.get(GROUPED), list):
        return list(action.args[GROUPED])
    return [{"args": action.args, "preview": action.preview}]


def _account(args: dict) -> str | None:
    """The account a call's item belongs to, by the first id in it that
    names one ("<class>:<key>"); None for a call that names none."""
    from agent.connectors import slot_of

    return next((slot for value in args.values() if (slot := slot_of(value)) is not None),
                None)


def account_key(args: dict) -> str | None:
    """The key of the account a card acts on, kept in its own column so that
    switching the account off withdraws the card (`core/modules.withdraw_cards`).
    A grouped card's items share one account."""
    grouped = args.get(GROUPED)
    first = grouped[0]["args"] if isinstance(grouped, list) and grouped else args
    if isinstance(first.get("module_key"), str):
        # A module's own card (agent/modtools.py) goes with the module.
        return first["module_key"]
    slot = _account(first)
    return slot.split(":")[1] if slot else None


def listing(group: list[dict]) -> str:
    """One card's text for several items."""
    shown = "\n\n".join(f"{n}. {item['preview']}" for n, item in enumerate(group, 1))
    return (f"{len(group)} on one card: yes to all of them here, or to some of them "
            f"in the app.\n\n{shown}")


def _join(conn: sqlite3.Connection, turn: _Turn, *, tool: str, args: dict, preview: str,
          consent: Consent) -> dict | None:
    """Add this item to a card the turn holds for the same tool and account,
    asking the same way; that card's receipt, or None if there is none."""
    account = _account(args)
    if account is None or consent.spend_usd:
        return None
    asked = (str(consent.reach), str(consent.reversal), list(consent.reasons))
    for action_id in reversed(turn.made):
        action = get(conn, action_id)
        if (action is None or action.status != "pending" or action.tool != tool
                or action.spend_usd or (action.reach, action.reversal, action.reasons) != asked):
            continue
        group = items(action)
        if _account(group[0]["args"]) != account:
            continue
        if any(item["args"] == args for item in group):
            return _receipt(action, repeated=True)
        group.append({"args": args, "preview": preview})
        grouped = {GROUPED: group}
        with db.transaction(conn):
            # Never a card with a second factor bound to it: that was given
            # for the card as it was.
            done = conn.execute(
                "UPDATE actions SET args_json = ?, args_hash = ?, preview = ?"
                " WHERE id = ? AND status = 'pending'"
                " AND NOT EXISTS (SELECT 1 FROM approval_proofs WHERE action_id = ?)",
                (json.dumps(grouped, default=str), _hash(tool, grouped), listing(group),
                 action_id, action_id)).rowcount
        if done:
            return _receipt(get(conn, action_id), joined=len(group))
    return None


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass
class Action:
    id: int
    tool: str
    args: dict
    preview: str
    status: str
    token: str | None
    reach: str
    reversal: str
    reasons: list[str]
    spend_usd: float
    expires_at: str | None
    ask_message_ts: int | None = None
    session_id: int | None = None
    account: str | None = None
    withdrawn_reason: str | None = None
    # What a yes binds: the tool and its arguments, as they are now.
    args_hash: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Action:
        return cls(
            id=int(row["id"]),
            tool=row["tool"],
            args=json.loads(row["args_json"] or "{}"),
            preview=row["preview"],
            status=row["status"],
            token=row["token"],
            reach=row["reach"],
            reversal=row["reversal"],
            reasons=json.loads(row["reasons"] or "[]"),
            spend_usd=float(row["spend_usd"] or 0),
            expires_at=row["expires_at"],
            ask_message_ts=row["ask_message_ts"],
            session_id=row["session_id"],
            account=row["account"],
            withdrawn_reason=row["withdrawn_reason"],
            args_hash=row["args_hash"],
        )

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        return datetime.fromisoformat(self.expires_at) < datetime.now()

    @property
    def high_risk(self) -> bool:
        """Whether a yes to it takes a second factor (core/monitor.py)."""
        from core.consent import high_risk

        return high_risk(self.tool)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "tool": self.tool,
            "preview": self.preview,
            "status": self.status,
            "token": self.token,
            "reach": self.reach,
            "reversal": self.reversal,
            "reasons": self.reasons,
            "spend_usd": self.spend_usd,
            "expires_at": self.expires_at,
            "high_risk": self.high_risk,
            "withdrawn_reason": self.withdrawn_reason,
        }


def _hash(tool: str, args: dict) -> str:
    payload = json.dumps({"tool": tool, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def get(conn: sqlite3.Connection, action_id: int) -> Action | None:
    row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    return Action.from_row(row) if row else None


def pending(conn: sqlite3.Connection, limit: int = 10) -> list[Action]:
    """Cards waiting for an answer, but not one for an account that is off:
    turning it off withdraws its cards (`core/modules.withdraw_cards`), and
    this keeps one asked in the moment between out of the list."""
    rows = conn.execute(
        f"SELECT * FROM actions WHERE status = 'pending' AND {modules.live(conn, 'actions')}"
        " ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    return [Action.from_row(r) for r in rows]


def holding(conn: sqlite3.Connection, limit: int = 10) -> list[Action]:
    """Actions the user has approved that have not gone yet.

    The undo window, as a query. Without it the intercept stops recognising a
    card the moment it stops being pending -- which is exactly the half-minute
    UNDO exists for, so "UNDO MAPLE" would reach the model and the mail would
    go anyway.
    """
    rows = conn.execute(
        "SELECT * FROM actions WHERE status IN ('approved', 'executing')"
        " ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    return [Action.from_row(r) for r in rows]


def withdrawn(conn: sqlite3.Connection, hours: int = 24, limit: int = 10) -> list[Action]:
    """Cards withdrawn in the last `hours` because their account was switched
    off or removed (`core/modules.withdraw_cards`), or the chat they were
    asked in unlinked (`forget_chat`): a late reply to one is told why, not
    that it "may have expired"."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM actions WHERE status = 'withdrawn' AND decided_at > ?"
        " ORDER BY id DESC LIMIT ?", (since, limit)).fetchall()
    return [Action.from_row(r) for r in rows]


@dataclass
class TakenBack:
    """What unlinking a chat took back (`forget_chat`): the ids of the
    cards withdrawn, of the routines and goals paused, by holder, and of
    the yeses undone: still in their hold, or the chat's approvals of what
    was paused."""

    withdrawn: list[int]
    paused: dict[str, list[int]]
    undone: list[int] = field(default_factory=list)

    def says(self, name: str) -> str:
        """For the app, after the goodbye: what stopped, and how to undo it."""
        said = []
        held = [f"{len(ids)} {holder}{'' if len(ids) == 1 else 's'}"
                for holder, ids in self.paused.items() if ids]
        if held:
            one = sum(map(len, self.paused.values())) == 1
            said.append(f"{' and '.join(held)} approved from {name} "
                        f"{'is' if one else 'are'} paused. If you turn "
                        f"{'it' if one else 'them'} on again in the app, "
                        f"{'it asks' if one else 'they ask'} you before each send.")
        if n := len(self.withdrawn):
            said.append(f"{n} card{'' if n == 1 else 's'} asked there "
                        f"{'was' if n == 1 else 'were'} withdrawn.")
        return " ".join(said)


def forget_chat(conn: sqlite3.Connection, channel: str, why: str) -> TakenBack:
    """Take back what a chat could set going, as it is unlinked or unpaired:
    the cards asked there and still waiting are withdrawn, saying `why`; the
    routines and goals it approved are paused; and its yeses are undone --
    those still in their hold, as UNDO would, and its approvals of what was
    paused, so they no longer count (`core/monitor.approved_envelope`). A
    yes whose call has begun is not undone (`_calling`): what it does is
    done, whatever the card says.

    Until step 11's audit (D6), unlinking stopped only the chat's outbox. A
    chat someone else had paired, or a lost phone's, kept what it had
    approved: a routine sending each Friday inside its envelope, a goal
    working to its budget, and its cards waiting for a click in the app by a
    user who never asked for them.

    "Approved from" the chat is whoever's yes set it going: its card's, when
    it has one (`decided_by`); with no envelope it has none, and being set
    up in the chat's conversation was its approval. Paused, not cancelled:
    the user's own are one press in the app from running again. That press
    is not their yes to the envelope, since the app's resume takes no
    sign-in, so the chat's approval is undone, not left to come back with
    it (step 11's review): a resumed run asks before each send. A yes in
    its hold was missed the same way: `approve_routine` is irreversible, so
    its card sat `executing` for the hold with the routine not yet started.
    A withdrawn or undone routine or goal card cancels or drops what it
    would have approved (migration 20)."""
    from core import monitor

    # `decided_at` is the local time, as `decide` writes it.
    stamp = datetime.now().isoformat(timespec="seconds")
    with db.transaction(conn):
        withdrawn = [r[0] for r in conn.execute(
            "UPDATE actions SET status = 'withdrawn', withdrawn_reason = ?, decided_at = ?"
            " WHERE status = 'pending' AND channel = ? RETURNING id",
            (why, stamp, channel)).fetchall()]
        paused = {}
        for holder, (table, card, key, _) in monitor.HOLDERS.items():
            its_cards = (f"FROM actions a WHERE a.tool = ?"
                         f" AND json_extract(a.args_json, '$.{key}') = {table}.id")
            paused[holder] = sorted(r[0] for r in conn.execute(
                f"UPDATE {table} SET status = 'paused' WHERE status = 'active' AND ("
                f" EXISTS (SELECT 1 {its_cards} AND a.decided_by = ?"
                "   AND a.status IN ('approved', 'executing', 'done'))"
                f" OR (NOT EXISTS (SELECT 1 {its_cards}) AND session_id IN"
                "   (SELECT id FROM sessions WHERE channel = ?))) RETURNING id",
                (card, channel, card, channel)).fetchall())
        # After the pause, which reads them. Approved, or executing with its
        # call not begun: as `cancel_during_hold` takes them, so `claim`, and
        # `_calling` after the hold, find it undone. An approval of a
        # routine or a goal is undone however far it got, since what it
        # approved is still to come, and `_finish` keeps its undo.
        cards = tuple(card for _, card, _, _ in monitor.HOLDERS.values())
        undone = [r[0] for r in conn.execute(
            "UPDATE actions SET status = 'undone', undone_at = datetime('now')"
            f" WHERE decided_by = ? AND (status = 'approved' OR {_UNCALLED}"
            f" OR (status IN ('executing', 'done') AND tool IN ({','.join('?' * len(cards))})))"
            " RETURNING id", (channel, _held_since(), *cards)).fetchall()]
    return TakenBack(sorted(withdrawn), paused, sorted(undone))


def recent(conn: sqlite3.Connection, limit: int = 25) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [Action.from_row(r).as_dict() for r in rows]


# ---------------------------------------------------------------------------
# Recording what happened anyway
# ---------------------------------------------------------------------------


def record_free(
    conn: sqlite3.Connection,
    *,
    tool: str,
    args: dict,
    consent: Consent,
    preview: str = "",
    result: dict | None = None,
    session_id: int | None = None,
    channel: str | None = None,
) -> int:
    """Log an action that did not need asking: `done`, or `failed` if the
    executor said so -- see `reported_failure`. Everything that reads the
    ledger, like a goal's account of what its sessions did, reads only what
    happened; when the phone counted it, a call refused for quiet hours used
    up the day's allowance.

    With the session it came from, as a card has: a goal finds what its
    sessions did by session (agent/goals.after_session), and the first live
    goal's draft, logged without one, was invisible to its next session."""
    failure = reported_failure(result)
    with db.transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO actions (tool, args_json, args_hash, preview, reach,
                                 reversal, reasons, status, executed_at, result_json,
                                 error, session_id, channel, account)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)
            """,
            (
                tool,
                json.dumps(args, default=str),
                _hash(tool, args),
                preview or tool,
                str(consent.reach),
                str(consent.reversal),
                json.dumps(consent.reasons),
                "failed" if failure else "done",
                json.dumps(result, default=str) if result is not None else None,
                failure[:300] if failure else None,
                session_id,
                channel,
                account_key(args),
            ),
        )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


async def request(
    conn: sqlite3.Connection,
    *,
    tool: str,
    args: dict,
    preview: str,
    consent: Consent,
    session_id: int | None = None,
    channel: str = "tray",
    routine_module: str | None = None,
) -> dict:
    """Queue an action and ask. Returns the receipt the tool hands back.

    The card is `preview`, whole. One that quotes the body of the message it
    would send is not asked at all (`quoted_body`).
    """
    receipt, made = queue(conn, tool=tool, args=args, preview=preview, consent=consent,
                          session_id=session_id, channel=channel,
                          routine_module=routine_module)
    if made is None:
        return receipt
    if (turn := _open_turn()) is not None:
        turn.made.append(made.id)  # asked when the turn ends (`held`)
        _HELD.add(made.id)
        return receipt
    _ask_soon(made)
    return receipt


def queue(
    conn: sqlite3.Connection,
    *,
    tool: str,
    args: dict,
    preview: str,
    consent: Consent,
    session_id: int | None = None,
    channel: str = "tray",
    routine_module: str | None = None,
) -> tuple[dict, Action | None]:
    """`request` without the asking: the receipt, and the card if a new one
    was made (None for the same request again, or one refused). Blocking
    callers use it -- a briefing written on its own thread carries its
    questions in its own text (`closing.briefing_questions`).

    `routine_module` is the key of the module whose routine asks, kept in its
    own column so that turning the module off or removing it withdraws the
    card (`core/modules.withdraw_cards`): a card for one of core's tools
    names no account of the module's."""
    from core import monitor

    if quoted := quoted_body(preview, args):
        log.error("a %s card quoted its message's body; not asked", tool)
        return {"status": "refused",
                "error": f"a card never quotes the message it would send (it had "
                         f"{quoted[:40]!r}...): the message belongs in the user's Drafts",
                "note": "This is a fault in the tool, not something the user did. Tell "
                        "them it could not be asked, in one sentence."}, None

    if (turn := _open_turn()) is not None and (joined := _join(
            conn, turn, tool=tool, args=args, preview=preview, consent=consent)) is not None:
        return joined, None

    existing = conn.execute(
        "SELECT * FROM actions WHERE args_hash = ? AND status = 'pending'",
        (_hash(tool, args),),
    ).fetchone()
    if existing is not None:
        # The same request twice is one card. `defer_to_background` grew this
        # guard because one request became three jobs; here it would be three
        # emails.
        action = Action.from_row(existing)
        return _receipt(action, repeated=True), None
    if why := monitor.may_ask(conn, tool):
        return {"status": "refused", "error": why,
                "note": "Tell the user this has to wait, and why, in one sentence."}, None

    expires = (datetime.now() + TTL).isoformat(timespec="seconds")

    with db.transaction(conn):
        # A card the user has said yes to keeps its word through the undo
        # window (`holding`), and `parse_reply` looks there first: a new card
        # given the same word had its "YES SABLE" answered "SABLE is already
        # going" and its "NO SABLE" stop the other action (a gate test failing
        # 1 run in 48, 8 October 2026).
        # The words are read from the table, not through `pending`, which
        # hides a card whose account is off while the unique index still
        # counts it: a second card drew such a card's word 1 time in 48, and
        # its turn failed (the persona eval's Outlook day, 17 October 2026).
        # And they are read inside the write's own transaction: a briefing
        # makes its cards on a thread and connection of its own, and two
        # writers that each read before either wrote drew the same word.
        taken = {row[0] for row in conn.execute(
            "SELECT token FROM actions WHERE token IS NOT NULL"
            " AND status IN ('pending', 'approved', 'executing')")}
        last_used = dict(conn.execute(
            "SELECT UPPER(token), MAX(id) FROM actions WHERE token IS NOT NULL"
            " GROUP BY UPPER(token)").fetchall())
        token = token_for(taken, last_used)
        cur = conn.execute(
            """
            INSERT INTO actions (tool, args_json, args_hash, preview, reach,
                                 reversal, reasons, spend_usd, session_id,
                                 channel, status, token, asked_at, expires_at, account,
                                 routine_module)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, datetime('now'), ?, ?, ?)
            """,
            (
                tool,
                json.dumps(args, default=str),
                _hash(tool, args),
                preview,
                str(consent.reach),
                str(consent.reversal),
                json.dumps(consent.reasons),
                consent.spend_usd,
                session_id,
                channel,
                token,
                expires,
                account_key(args),
                routine_module,
            ),
        )

    action = get(conn, int(cur.lastrowid))
    return _receipt(action), action


def _receipt(action: Action, *, repeated: bool = False, joined: int = 0) -> dict:
    """What the model is told of a card: that it waits, and what it shows.
    Not its word, which only the card says (agent/cardwords.py): a model
    that knew it could write a card of its own around it (step 11's third
    review)."""
    note = ("The user has to approve this before it happens: the card is on their phone "
            "and in the app. Say in one sentence what you queued and stop. Do not call "
            "this again.")
    if joined:
        note = (f"This joined the card for the same thing, which now asks about {joined}; "
                f"one yes approves them all. Say in one sentence what is waiting, and stop.")
    return {
        "status": "already_queued" if repeated else "awaiting_approval",
        "action_id": action.id,
        "preview": action.preview,
        "note": note,
    }


def waiting(result: object) -> bool:
    """Whether what `act` returned is a card waiting for their yes, rather than
    something done or refused (`_receipt`)."""
    return isinstance(result, dict) and result.get("status") in ("awaiting_approval",
                                                                  "already_queued")


def card(action: Action, word: str | None = None) -> str:
    """What lands on their phone; with `word` in place of the card's own,
    what the conversation records of it (`_ask`)."""
    word = word or action.token
    lines = [action.preview, ""]
    if action.reasons and action.reasons[0]:
        lines.append(action.reasons[0])
        lines.append("")
    if action.spend_usd:
        lines.append(f"Spends ${action.spend_usd:.2f}.")
    from core.consent import high_risk

    if high_risk(action.tool):
        # Not "Reply YES MAPLE to go ahead": a thumbs-up matches that line
        # (`channels/bridge.CARD_ANSWER`) and cannot carry a code.
        lines.append(f"High-risk: reply YES {word} and the 6-digit code from your "
                     f"authenticator app. NO {word} drops it.")
    else:
        lines.append(f"Reply YES {word} to go ahead, NO {word} to drop it.")
    return "\n".join(lines)


async def _ask(action: Action | None) -> None:
    """Put the card in the outbox, which shows it in the app at once and
    sends it to their phone until it goes, or until it is answered.

    It used to be sent and forgotten: an undelivered card sat pending,
    unseen, until it expired twelve hours later. Then it was tried twice,
    ten seconds apart, and with the relay gone a card Signal did not take
    was never asked again.

    The conversation records it without its word (`noted`): the model
    reads the conversation, and a model that knew a waiting card's word
    could write a card of its own around it (agent/cardwords.py).
    """
    if action is None or _notify is None:
        if action is not None:
            log.warning("no notifier; action %s is waiting unseen", action.id)
        return
    try:
        await _notify(card(action), action_id=action.id,
                      noted=card(action, word=cardwords.PLACEHOLDER))
    except Exception:  # noqa: BLE001 - the card is still in Actions, and expires
        log.exception("the card for action %s could not be written to the outbox", action.id)


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    reply: str
    action_id: int | None = None
    approved: bool | None = None


def decide(
    conn: sqlite3.Connection, action_id: int, *, approve: bool, by: str, reason: str = "",
    only: list[int] | None = None, seen: str | None = None,
) -> dict:
    """Record a yes or a no. Execution is a separate step. Whether it may be
    recorded is `core/monitor.may_decide`'s: pending, not expired, and for a
    high-risk action, their code bound to it.

    `only` approves part of a grouped card: the items by their numbers on
    it, from 1. The rest are dropped, and named in the record. `seen` is
    the card's `args_hash` as the app read it: a yes to a card that has
    changed since is refused, not stretched over items the user never saw."""
    from core import monitor

    action = get(conn, action_id)
    if action is None:
        return {"ok": False, "error": "no such action"}
    # Asked as the answer is recorded, not as it arrived: a "YES MAPLE
    # 482913" still waiting on its code when Telegram was unpaired was
    # recorded as Telegram's after everything the chat set going had been
    # taken back (step 11's second review).
    if _answers_from is not None and not _answers_from(by):
        return {"ok": False, "error": f"{by.capitalize()} is no longer linked, so an answer "
                                      "from it does not count: answer it in the app"}
    if approve and seen is not None and seen != action.args_hash:
        return {"ok": False, "error": "the card changed since you read it: read it again"}
    if why := monitor.may_decide(conn, action, approve=approve, by=by):
        # Re-checked here rather than left to a sweeper: a sweeper lags, and
        # the check on the approve path is the one that decides whether it runs.
        if action.status == "pending" and action.expired:
            _set(conn, action_id, status="expired")
        return {"ok": False, "error": why}

    fields: dict = {}
    if approve and only is not None:
        part = _part(action, only)
        if isinstance(part, str):
            return {"ok": False, "error": part}
        left_out = part.pop("left_out")
        fields, reason = part, reason or left_out
    _set(
        conn,
        action_id,
        status="approved" if approve else "rejected",
        decided_at=datetime.now().isoformat(timespec="seconds"),
        decided_by=by,
        error=reason or None,
        **fields,
    )
    return {"ok": True, "action_id": action_id, "approved": approve}


def _part(action: Action, only: list[int]) -> dict | str:
    """The columns of a grouped card cut to the items `only` numbers, or why
    it cannot be."""
    from core.consent import high_risk

    group = items(action)
    chosen = sorted({n for n in only if 1 <= n <= len(group)})
    if not chosen or len(chosen) != len(set(only)):
        return f"the card has items 1 to {len(group)}; choose among them"
    if len(chosen) == len(group):
        return {"left_out": ""}
    if high_risk(action.tool):
        # Its code was bound to everything on the card (`core/monitor._bind`).
        return "a high-risk card is approved whole, with its code, or not at all"
    kept = [group[n - 1] for n in chosen]
    args = kept[0]["args"] if len(kept) == 1 else {GROUPED: kept}
    preview = kept[0]["preview"] if len(kept) == 1 else listing(kept)
    dropped = [str(n) for n in range(1, len(group) + 1) if n not in chosen]
    return {"args_json": json.dumps(args, default=str), "args_hash": _hash(action.tool, args),
            "preview": preview, "left_out": f"left out item {', '.join(dropped)}"}


def _set(conn: sqlite3.Connection, action_id: int, **fields) -> None:
    if not fields:
        return
    columns = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction(conn):
        conn.execute(
            f"UPDATE actions SET {columns} WHERE id = ?", (*fields.values(), action_id)
        )


def claim(conn: sqlite3.Connection, action_id: int) -> Action | None:
    """Take an approved action for execution, or return None.

    The whole exactly-once story. Two callers race; one gets the row.
    """
    with db.transaction(conn):
        row = conn.execute(
            """
            UPDATE actions SET status = 'executing', executed_at = datetime('now')
             WHERE id = ? AND status = 'approved'
            RETURNING *
            """,
            (action_id,),
        ).fetchone()
    return Action.from_row(row) if row else None


# ---------------------------------------------------------------------------
# Spending
# ---------------------------------------------------------------------------


def spent_since(conn: sqlite3.Connection, days: int, *, besides: int | None = None) -> float:
    """What actions spent in the last `days`: those running or done, and
    any whose call began, however it ended. A call that failed may have
    spent all the same, and one taken back mid-call did, uncounted, until
    step 11's third review. Leaving out `besides`: the action being checked
    is `executing` by then (`claim`), and was counted twice against its own
    cap."""
    row = conn.execute(
        "SELECT COALESCE(SUM(spend_usd), 0) AS total FROM actions"
        " WHERE (status IN ('executing', 'done') OR called_at IS NOT NULL)"
        " AND datetime(executed_at) > datetime('now', ?)"
        " AND id IS NOT ?",
        (f"-{int(days)} days", besides),
    ).fetchone()
    return float(row["total"] if row else 0)


def over_cap(conn: sqlite3.Connection, amount: float, *,
             action_id: int | None = None) -> str | None:
    """Why this must not run, or None. Approval does not override these.

    The amount is the model's argument (`core/consent`). NaN passed every
    comparison, and SQLite's SUM skips it as NULL; a negative one lowered
    the totals. Neither is a price."""
    if not math.isfinite(amount) or amount < 0:
        return f"{amount} is not an amount a cap can check"
    if amount == 0:
        return None
    caps = spend_caps()
    if caps["per_action_usd"] and amount > caps["per_action_usd"]:
        return f"${amount:.2f} is over the ${caps['per_action_usd']:.0f} single-action cap"
    day, week = caps["per_day_usd"], caps["per_week_usd"]
    if day and spent_since(conn, 1, besides=action_id) + amount > day:
        return f"that would put today over the ${day:.0f} daily cap"
    if week and spent_since(conn, 7, besides=action_id) + amount > week:
        return f"that would put this week over the ${week:.0f} cap"
    return None


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------


async def execute(conn: sqlite3.Connection, action_id: int, *, hold: bool = True) -> dict:
    """Run an approved action. Safe to call twice; the second does nothing."""
    from core import monitor

    action = claim(conn, action_id)
    if action is None:
        return {"ok": False, "error": "not approved, or already running"}
    # The second check (core/monitor.py): a high-risk action approved without
    # their code does not run, whatever let the approval through.
    if refused := monitor.may_run(conn, action):
        _finish(conn, action_id, "failed", error=refused)
        return {"ok": False, "error": refused}

    blocked = over_cap(conn, action.spend_usd, action_id=action.id)
    if blocked:
        _finish(conn, action_id, "failed", error=blocked)
        return {"ok": False, "error": blocked}

    if hold and action.reversal == "irreversible" and HOLD_SECONDS:
        # The window in which UNDO still works. Cheap, and it converts the
        # one class of mistake that cannot be taken back.
        await asyncio.sleep(HOLD_SECONDS)

    if _execute_tool is None:
        _finish(conn, action_id, "failed", error="no executor registered")
        return {"ok": False, "error": "no executor registered"}

    if not _calling(conn, action_id):
        return {"ok": False, "error": "cancelled during the hold"}
    try:
        with approved():
            result = await _execute_tool(action.tool, action.args)
    except Exception as exc:  # noqa: BLE001 - a failed action is an outcome
        log.exception("action %s failed", action_id)
        _finish(conn, action_id, "failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if isinstance(result, dict) and result.get("again"):
        _ask_again(conn, action, result["again"])

    if (failure := reported_failure(result)) is not None:
        _finish(conn, action_id, "failed", error=failure[:300],
                result_json=json.dumps(result, default=str))
        return {"ok": False, "error": failure, "result": result}
    _finish(conn, action_id, "done", result_json=json.dumps(result, default=str))
    return {"ok": True, "action_id": action_id, "result": result}


def _calling(conn: sqlite3.Connection, action_id: int) -> bool:
    """Mark the card's call begun, if it is still executing; whether it was.

    From here on nothing takes the card back (`cancel_during_hold`,
    `forget_chat`): what the call does is done, whatever the card says. A
    yes undone mid-call left an email sent but recorded undone, which the
    mailbox, keyed on done, could send again, and money spent but not
    counted (step 11's third review). Checked and marked in one write: a
    read and then a write would let an undo land between them. Nor is a
    card taken back past its hold and `CALL_MARGIN_SECONDS`, marked or not:
    a worker from before the mark calls without it (`_UNCALLED`)."""
    with db.transaction(conn):
        row = conn.execute(
            "UPDATE actions SET called_at = datetime('now')"
            " WHERE id = ? AND status = 'executing' RETURNING id", (action_id,)).fetchone()
    return row is not None


def _finish(conn: sqlite3.Connection, action_id: int, status: str, **fields) -> None:
    """A run's outcome, kept. Its status is written while the card is
    executing, and once its call began (`_calling`), since what the call did
    is done.

    One undo stands: an approval of a routine or a goal undone mid-call, by
    unlinking its chat (`forget_chat`), stays undone. Written over, it
    counted after all (`core/monitor.approved_envelope` needs a done card;
    step 11's second review). What it did is still kept beside the undo. An
    undo before the call is kept too: nothing ran."""
    from core import monitor

    cards = tuple(card for _, card, _, _ in monitor.HOLDERS.values())
    columns = "".join(f", {k} = ?" for k in fields)
    with db.transaction(conn):
        conn.execute(
            "UPDATE actions SET status = CASE WHEN status = 'executing' OR (called_at IS NOT NULL"
            f" AND tool NOT IN ({','.join('?' * len(cards))})) THEN ? ELSE status END"
            f"{columns} WHERE id = ?", (*cards, status, *fields.values(), action_id))


def _ask_again(conn: sqlite3.Connection, action: Action, again: dict | list) -> None:
    """A new card for what changed after its card was shown: the item as it
    is now, asked the same way (`core/writes.py`: "any change asks again").
    Asked at once, never held: the turn that made the first card is over."""
    from core.consent import Reach, Reversal

    group = again if isinstance(again, list) else [again]
    args = group[0]["args"] if len(group) == 1 else {GROUPED: group}
    preview = group[0]["preview"] if len(group) == 1 else listing(group)
    row = conn.execute("SELECT channel FROM actions WHERE id = ?", (action.id,)).fetchone()
    consent = Consent(gated=True, reach=Reach(action.reach), reversal=Reversal(action.reversal),
                      reasons=list(action.reasons), spend_usd=action.spend_usd)
    token = _TURN.set(None)
    try:
        _, made = queue(conn, tool=action.tool, args=args, consent=consent,
                        preview=f"Asked again: it changed after you said yes.\n\n{preview}",
                        session_id=action.session_id, channel=(row[0] if row else None) or "tray")
    finally:
        _TURN.reset(token)
    if made is not None:
        _ask_soon(made)


def reported_failure(result: object) -> str | None:
    """The error an executor reported, or None if it succeeded.

    Executors follow the rule "return a dict, never raise", so a failure
    arrives as `{"ok": False, "error": ...}` -- and only an exception used to
    count as one. An email the provider refused was recorded "done" and
    announced to the user as "Done — Email Bob".
    """
    if not isinstance(result, dict):
        return None
    if result.get("ok") is False:
        return str(result.get("error") or "it reported a failure without saying why")
    if result.get("ok") is not True and result.get("error"):
        return str(result["error"])
    return None


def cancel_during_hold(conn: sqlite3.Connection, action_id: int) -> bool:
    """Stop an action that is inside its hold window, or approved and
    waiting its turn. Only `executing` counted, so UNDO of an action
    queued behind another answered "too late", and it then ran; undone
    first, `claim` never takes it. Not once its call has begun (`_calling`),
    or may have (`_UNCALLED`): the email went, and recorded undone it could
    go again."""
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE actions SET status = 'undone', undone_at = datetime('now')"
            f" WHERE id = ? AND (status = 'approved' OR {_UNCALLED})",
            (action_id, _held_since()),
        )
    return cur.rowcount > 0


def expire_stale(conn: sqlite3.Connection) -> int:
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE actions SET status = 'expired'"
            " WHERE status = 'pending' AND expires_at IS NOT NULL"
            # `expires_at` is written by Python as a *local* time, so the
            # comparison has to be local too. Against SQLite's UTC `now` this
            # ran four hours fast in Eastern: a twelve-hour TTL retired after
            # eight, and the sweep disagreed with `Action.expired` -- two
            # answers to "is this still live", differing by the UTC offset.
            " AND datetime(expires_at) < datetime('now', 'localtime')"
        )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Reading a reply
# ---------------------------------------------------------------------------

# Verbs that answer a card *when the card's word is also there*. Broad on
# purpose: "ok MAPLE" and "go MAPLE" can only mean one thing.
_YES = {"yes", "y", "yep", "yeah", "ok", "okay", "send", "go", "do it", "approve", "approved",
        "sure", "👍", "✅"}
_NO = {"no", "n", "nope", "stop", "cancel", "don't", "dont", "reject", "not", "never",
       "👎", "❌"}

# A reply's words: runs of letters, digits and apostrophes, and each other
# mark that is not punctuation (an emoji is a word here). Splitting on
# spaces alone left "no!" and "no." words of their own, which are not "no",
# so "no! go maple" read only "go" and approved (step 11's review).
_WORD = re.compile(r"[\w']+|[^\w\s',.!?;:]")
# Apostrophes a keyboard or a font puts in "don't", each read as a straight
# one: phone keyboards curl it, and "Don’t send MAPLE" was no refusal.
_APOSTROPHES = str.maketrans(dict.fromkeys(
    "\u2019\u2018\u201b\u02bc\u02bb\u0060\u00b4\u2032\uff07", "'"))

# The other words a yes naming its card may carry. A yes approves only when
# every word in it is the card's word, a yes, one of these, or the code. It
# was the other way round: a yes approved unless a word of refusal was
# found, and each review found another way to refuse that was not on the
# list -- "Shouldn’t send MAPLE", "Don´t", "ＮＯ send maple", "🚫 send
# maple", "did you send maple?" (step 11's second review). Now a reply that
# says more is asked about.
_FILLER = {"please", "pls", "ahead", "it", "go", "ok", "okay", "thanks", "thank", "now",
           "just", "the", "that", "one"}
# Fillers only as a pair. Each alone made "Do you send maple" a yes (step
# 11's third review).
_PAIRS = re.compile(r"\b(?:do it|thank you)\b")
_DO_IT = re.compile(r"\bdo it\b")
# Said around a bare answer without changing it: "go ahead please".
_POLITE = {"please", "pls", "thanks"}
# Bare yeses that take an object, which a polite word beside them may be:
# "send thanks" approved the only card waiting (step 11's third review).
_LONE_VERBS = {"send", "approve"}


def _dressing(ch: str) -> bool:
    """What only dresses a reply: an emoji's variation selector or skin
    tone, and the format characters no one sees."""
    return ("\ufe00" <= ch <= "\ufe0f" or "\U0001f3fb" <= ch <= "\U0001f3ff"
            or unicodedata.category(ch) == "Cf")


def _reading(text: str) -> str:
    """A reply as it is read: apostrophes straightened, then NFKC, so that
    "ＮＯ" is "no", in lower case, without its dressing.

    Dressing between two letters or digits stays, and the word it is in is
    then not a card's word. Dropped there too, "YES CINDER" with a
    zero-width space or a word joiner inside the word read as "yes cinder"
    and approved, though no one types such a character into a word read
    off a card: it comes pasted, or from a program (step 11's fifth
    review). Kept, the word is asked about, as one in look-alike letters
    is (`parse_reply`)."""
    folded = unicodedata.normalize("NFKC", (text or "").translate(_APOSTROPHES))
    runs = [(dressing, "".join(run)) for dressing, run in groupby(folded, _dressing)]
    kept = "".join(run for n, (dressing, run) in enumerate(runs)
                   if not dressing or (0 < n < len(runs) - 1 and runs[n - 1][1][-1].isalnum()
                                       and runs[n + 1][1][0].isalnum()))
    return kept.strip().lower().strip(".!")


def _asks(text: str) -> bool:
    """Whether a reply holds a question mark, of any script: "?", "？",
    "¿", "⁉", "❓", Arabic's, and Greek's, which NFKC turns into a semicolon,
    so that "Send MAPLE;" in Greek approved (step 11's third review). Read
    before NFKC, by the characters' names."""
    return any("QUESTION" in unicodedata.name(ch, "") for ch in text)


def _only_yes(words: list[str], token: str) -> bool:
    """Whether a yes naming `token` says nothing else (`_FILLER`). A code
    is ASCII digits: NFKC makes full-width ones so, and others are not a
    code."""
    rest = _PAIRS.sub(" ", " ".join(words)).split()
    return all(w == token or w in _YES or w in _FILLER
               or (w.isascii() and w.isdigit() and len(w) in (3, 6)) for w in rest)


def _trimmed(words: list[str]) -> list[str]:
    """`words` without the polite words at either end: "thanks, go ahead"."""
    start, end = 0, len(words)
    while start < end and words[start] in _POLITE:
        start += 1
    while end > start and words[end - 1] in _POLITE:
        end -= 1
    return words[start:end]


def _which(action: Action) -> str:
    """What a reply that may be about `action`, but does not plainly answer
    it, is asked back: which card, what it is -- its whole first line,
    which a yes would approve -- and how to answer."""
    return (cardwords.which(action.token, cardwords.first_line(action.preview))
            + "\nIf you meant something else, say a little more.")


def _listing(waiting: list[Action]) -> str:
    """The cards waiting, a line each: its word and its whole first line."""
    return "\n".join(cardwords.listed(a.token, cardwords.first_line(a.preview))
                     for a in waiting)


def _pending(waiting: list[Action]) -> str:
    """The answer to "pending"."""
    if not waiting:
        return "Nothing is waiting for your yes."
    return "Waiting for your yes:\n" + _listing(waiting)

# Whole messages that answer a card with no word at all. Narrow on purpose,
# and matched against the entire message, never a word inside it. The first
# version matched any of `_YES` anywhere in a message of up to six words, so
# with one card waiting "send me the notes", "what time do we go" and "ok
# thanks" each sent it, and "no worries" dropped it. "ok", "sure" and "go"
# are missing here for the same reason: they are how the user acknowledges things,
# not how the user approves them.
BARE_YES = {
    "yes", "y", "yep", "yes please", "send", "send it", "go ahead", "do it",
    "approve", "approved", "👍", "✅", "✔",
}
BARE_NO = {
    "no", "n", "nope", "no thanks", "don't", "dont", "cancel", "reject",
    "stop", "drop it", "👎", "❌", "🚫",
}

# Only a short message is treated as an answer. "yes, and also what's the
# weather" is a turn, not an approval, and must reach the model untouched.
MAX_REPLY_WORDS = 6


def moved_on_since(conn: sqlite3.Connection, action: Action, channel: str) -> bool:
    """Is this card no longer the last thing in `channel`'s chat?

    A bare "yes" answers the most recent question in the chat it is typed
    in. This used to ask only whether the user had written anything since
    the card, which missed Steve's own words: a card at 10:00, then "want me
    to put it in your calendar too?" at 10:05, and a bare yes meant for that
    question approved the card (step 11's audit).

    So the card has to be the last thing there. It is in the chat once the
    outbox has sent it to this channel (`links.send_due`), and its line
    there is the card itself, stamped as it went. Moved on: anything else
    said in that chat after it, by either side, or anything the user said
    anywhere since it was asked. A card never sent to this channel -- the
    app's alone, or the other channel's -- is not in this chat at all.
    All the stamps are SQLite's UTC text, so they compare directly.

    Steve's own words since the card was asked count too, even those
    stored before it went out. A turn that asks a card stores its reply
    before the card is sent, but on the phone the two race: the card goes
    out at the turn's end, and the reply after the chat's typing and
    reaction calls. Ordered by the card's send stamp alone, a turn that
    asked a card and ended "shall I add it to your calendar too?" left
    the card looking like the last thing said, and the yes meant for the
    question approved it (step 11's fifth review). Which of the two the
    phone shows last cannot be known here, so the reply means asking
    which: a bare yes after it gets "Is that about <word>?".
    """
    told = conn.execute(
        "SELECT sent_at, COALESCE(noted, text) AS said FROM outbox"
        " WHERE action_id = ? AND remote = ? AND sent_at IS NOT NULL"
        " ORDER BY id DESC LIMIT 1",
        (action.id, channel),
    ).fetchone()
    if told is None:
        return True
    row = conn.execute(
        "SELECT 1 FROM messages m JOIN sessions s ON s.id = m.session_id"
        " JOIN actions a ON a.id = ?"
        " WHERE (m.role = 'user' AND m.created_at > a.asked_at)"
        "    OR (s.channel = ?"
        "        AND (m.created_at > ?"
        "             OR (m.role = 'assistant' AND m.created_at >= a.asked_at))"
        "        AND NOT (m.role = 'assistant' AND m.content = ?))"
        " LIMIT 1",
        (action.id, channel, told["sent_at"], told["said"]),
    ).fetchone()
    return row is not None


def parse_reply(
    text: str,
    waiting: list[Action],
    holding: list[Action] | None = None,
    *,
    bare_ok: bool = True,
    withdrawn: list[Action] | None = None,
    card: int | None = None,
) -> Decision | None:
    """Is this an answer to a card? None means it is an ordinary message.

    Two ways to answer, deliberately unequal. Naming the card's word with a
    verb -- "YES MAPLE", "ok maple", "maple no" -- is unmistakable in any
    short phrasing, as long as its verbs agree: "OK, no MAPLE" is asked
    about, not decided. A bare answer with no word is only an answer when the
    whole message is one (`BARE_YES`, `BARE_NO`), exactly one card is
    waiting, and `bare_ok`: the card is still the last thing in the chat
    (see `moved_on_since`). Anything else that merely contains a yes
    is a message, and reaches the model untouched.

    `holding` is the other population: actions the user has already said yes to that
    are still inside their hold window. They are answerable but not askable --
    "UNDO MAPLE" has to find one, a bare "yes" must never resolve to one, and
    they take no part in the ambiguity count, because the user is not being asked
    about them.

    `withdrawn` is a third: cards taken back because their account was turned
    off, or the chat they were asked in unlinked. Naming one is told why, and
    nothing else.

    `card` is the card a press or a reaction was on, when the channel knows
    it: that card is the only one it can decide. Its word may since have
    gone to a newer card, and a late thumbs-up on the old one decided that
    newer card, unseen (step 11's audit); now it is told the word is not
    waiting.
    """
    holding = holding or []
    withdrawn = withdrawn or []
    if card is not None:
        waiting, holding, withdrawn = ([a for a in group if a.id == card]
                                       for group in (waiting, holding, withdrawn))
    cleaned = _reading(text)
    if not cleaned or len(cleaned.split()) > MAX_REPLY_WORDS:
        return None

    # Quotes around a word leave it the word: "‘no’ send maple".
    words = [w for w in (w.strip("'") for w in _WORD.findall(cleaned)) if w]
    phrase = " ".join(words)
    # Each word as the redaction folds it, and each run between spaces too:
    # a character no one sees, kept inside a word (`_reading`), splits it
    # into pieces none of which is the card's word.
    spelled = [*map(cardwords.folded, words), *map(cardwords.folded, cleaned.split())]
    if words == ["pending"] and card is None:
        # Answered here: the model reads no waiting card's word
        # (agent/cardwords.py), and its answer had each taken out.
        return Decision(reply=_pending(waiting))
    if card is None and not waiting and not holding and not withdrawn:
        return None
    asked = _asks(text) or "?" in cleaned
    tokens = {a.token.lower(): a for a in waiting + holding if a.token}
    in_flight = {a.id for a in holding}

    named = next((tokens[w] for w in words if w in tokens), None)
    # Two cards' words in one reply. Read by its first word, "yes cobalt
    # undo maple" was an undo of COBALT, so the card the user approved was
    # dropped and the one they meant to stop went on (step 11's fifth
    # review). Which verb goes with which word is a guess however it is
    # read, and each guess acts; so none is decided, and the reply asks
    # for one at a time. A look-alike spelling names its card too.
    several = {tokens[f].id: tokens[f] for f in spelled if f in tokens}
    if len(several) > 1:
        return Decision(reply="That names more than one card, so nothing was done:\n"
                              + _listing(list(several.values()))
                              + "\n\nAnswer one at a time: YES <word> or NO <word>,"
                              " or UNDO <word> for one already going.")
    # What the reply's verbs say: True for each yes, False for each no. All
    # of them, not the first: "OK, no MAPLE" took "ok" and approved, and "no,
    # go maple" would have dropped it (step 11's audit). A reply that says
    # both is asked about, since either reading may be the wrong one.
    says = {w in _YES for w in words if w in _YES or w in _NO}
    if _DO_IT.search(phrase):
        says.add(True)  # a yes of two words, with the card's word or not: "do it maple"

    # Already going. A second yes is them repeating themselves; only a stop means
    # anything now, and it means it for about thirty seconds.
    if named is not None and named.id in in_flight:
        if any(w in _NO or w == "undo" for w in words):
            return Decision(reply="", action_id=named.id, approved=False)
        return Decision(
            reply=f"{named.token} is already going -- say UNDO {named.token} to stop it."
        )

    if named is not None:
        if "undo" in words:
            return Decision(reply="", action_id=named.id, approved=False)
        if not says:
            # The word with no yes or no that is known: "maple", "what is
            # maple", "yeah maple" before "yeah" was a yes. It went to the
            # model, and an answer the verbs did not know went with it (step
            # 11's third review). Asked, saying what the card is, which
            # answers a question about it too; more words reach the model.
            return Decision(reply=_which(named))
        # Both a yes and a no, a question ("Send MAPLE?"), or a yes that
        # says anything more ("Shouldn't send MAPLE"): either reading may be
        # the wrong one, and one of them acts.
        if len(says) > 1 or asked or (says == {True}
                                      and not _only_yes(words, named.token.lower())):
            return Decision(reply="Yes or no?\n" + _which(named))
        return Decision(reply="", action_id=named.id, approved=says == {True})

    # A waiting card's word in look-alike letters ("yes maple" with a
    # Cyrillic e) is not the word, and decides nothing; but it is about the
    # card, and it reached the model, and with it the word (step 11's fourth
    # review). Asked here, as the word with no plain answer is; and so is
    # one with a character no one sees inside it (step 11's fifth review).
    dressed = next((tokens[f] for f in spelled if f in tokens), None)
    if dressed is not None:
        if dressed.id in in_flight:
            return Decision(reply=f"{dressed.token} is already going -- "
                                  f"say UNDO {dressed.token} to stop it.")
        return Decision(reply=_which(dressed))

    # The user named a card that is not waiting -- expired, or already answered.
    # Falling through to the bare-yes path here would approve a *different*
    # action than the one the user named, which is precisely the silent wrong
    # action this whole design exists to prevent. Only when it is plainly an
    # answer, though -- a verb with it, or the word alone -- or an ordinary
    # sentence that happens to contain one of the words is swallowed.
    vocabulary = {w.lower() for w in TOKEN_WORDS}
    stale = next((w for w in words if w in vocabulary), None)
    taken_back = next((a for a in withdrawn if a.token and a.token.lower() == stale), None)
    if stale and taken_back is not None and (says or phrase == stale):
        return Decision(reply=f"{stale.upper()} was withdrawn: "
                              f"{taken_back.withdrawn_reason or 'its account was turned off'}. "
                              "Nothing was done.")
    if stale and (says or phrase == stale):
        return Decision(
            reply=(
                f"I don't have a {stale.upper()} waiting -- it may have "
                "expired or been answered already. "
                + (
                    "The one still open:\n" + _listing(waiting)
                    if len(waiting) == 1
                    else "Say `pending` to see what is."
                )
            ),
            action_id=None,
            approved=None,
        )

    # Polite words at the ends leave a bare answer the answer: "yes please",
    # "thanks, go ahead". Anywhere, they made "go please ahead" one, and
    # beside a lone verb they may be its object: "send thanks" approved the
    # only card waiting (step 11's third review). That is asked about.
    core = _trimmed(words)
    bare = " ".join(core) or phrase
    if bare not in BARE_YES and bare not in BARE_NO:
        return None
    unsure = asked or (len(core) < len(words) and bare in _LONE_VERBS)
    if not waiting:
        # Only in-flight actions exist, and the user named none of them. A bare
        # "stop" is too blunt to spend an irreversible action on; let it reach
        # the model, which can ask what the user means.
        return None
    if len(waiting) == 1:
        only = waiting[0]
        if not bare_ok or unsure:
            # The conversation has moved on, so this yes may answer the
            # model rather than the card; or it is a question ("yes?"), or
            # "send thanks". Ask; never guess, and never hand an
            # approval-shaped message to a model that could claim to have
            # acted on it.
            return Decision(reply=_which(only), action_id=None, approved=None)
        # A bare yes has to work, or the whole thing feels bureaucratic.
        return Decision(reply="", action_id=only.id, approved=bare in BARE_YES)

    # More than one waiting and no token: do not guess. Approving the wrong
    # thing is silent until it is not.
    return Decision(
        reply=f"{len(waiting)} things are waiting:\n{_listing(waiting)}\n\nReply YES <word>.",
        action_id=None,
        approved=None,
    )
