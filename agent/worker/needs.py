"""What needs the user: the app's notes (docs/design/notes.md), with their
"Done", "Later" and "Not now".

A note is for something with a cost if it is ignored. What needed the user
was spread over the app: cards on the Approvals page, a reminder in the
inbox among the morning's updates, a cap reached in a banner on Spend, a
sign-in ended on the accounts page, Signal unlinked on its own panel. A
stranger had to know where to look for each, and a reminder going off
scrolled away under updates that cost nothing to miss. So one list
(`needs`), four kinds:

- **yes** and **code**: a card waiting (`approvals.pending`, so never one
  for an account that is off); `code` when it is high-risk. Answered
  through /actions/decide, as before.
- **now**: a reminder going off, or a critical notice (one that is sent
  even at night), for `NOW_FOR`, until "Done"; "Later" books the reminder
  again.
- **stopped**: what stops Steve working until the user acts: a spending
  cap reached, OpenAI's balance used up, an account's sign-in ended,
  Signal unlinked. Each with the one action that fixes it, and "Not now"
  for an hour. "The backend keeps stopping" is the app's own note: only
  the app knows it.

What is answered or put off is kept (`put_off`, db/migrations/0015_needs.sql)
by the note's id: without it, a note dismissed came back at the next poll.
OpenRouter's credit running out is not a note: it pays only for Jev, and
Steve keeps working without it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import approvals, caps, offloop, reminders, runtime, sqltime
from agent.attention import settings
from agent.cut import cut
from agent.worker import bodies, links
from core import clock, db, health, modules, spend

# How long a reminder gone off, or a critical notice, stays a note unanswered.
# Twelve hours was too short: one that went off at eight while the user was
# out all day was gone by evening, never answered. A week covers a day or a
# weekend away; no time at all would pile up, for someone who answers on the
# phone, every reminder since the app was last opened, since the phone has
# no "Done".
NOW_FOR = timedelta(days=7)
# "Not now": a stopped note comes back after this.
NOT_NOW = timedelta(hours=1)
# "Later"'s choices, besides the evening's and the morning's briefings.
LATER_BY = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1)}
LATER = (*LATER_BY, "evening", "morning")
SHORT = 200
# The design's order (notes.md, Order): stopped first, since nothing else
# works until it is fixed; then what is going off now; then cards, the
# soonest to expire first. Cards came before "now" at first, for expiring;
# but a reminder is due this minute and a card has hours.
_RANK = {"stopped": 0, "now": 1, "code": 2, "yes": 2}
_NEVER = "9999"
# Each note's id says which kind it is, and so which answers it takes.
_NOW_ID = re.compile(r"(reminder|notice):\d+")
_STOPPED_ID = re.compile(r"cap:\w+:[\w-]+|account:\S+|signal")
SIGNAL_LOST = ("Signal isn't linked, so nothing reaches your phone. Messages wait here"
               " in the app until it is linked again.")


def _local(moment: datetime) -> datetime:
    """A moment as the user's wall time, naive: how reminders keep theirs."""
    return moment.astimezone(clock.local_zone()).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def _cards(conn) -> list[dict]:
    asked = {r["id"]: r["created_at"] for r in conn.execute(
        "SELECT id, created_at FROM actions WHERE status = 'pending'"
        f" AND {modules.live(conn, 'actions')}")}
    out = []
    held = approvals.held_now()
    for card in approvals.pending(conn, limit=50):
        if card.id in held:
            continue  # its turn may still add to it; a note comes when it ends
        lines = [line.strip() for line in card.preview.strip().splitlines() if line.strip()]
        lines = lines or [card.tool]
        expires = None
        if card.expires_at:  # local wall time, as approvals writes it
            ends = datetime.fromisoformat(card.expires_at)
            expires = sqltime.iso(ends if ends.tzinfo else ends.replace(tzinfo=clock.local_zone()))
        # The card whole, as the phone gets it (`approvals.card`): the yes
        # agrees to all of it. The note showed its second line alone, which
        # was a mail's subject until a send's card named its account first;
        # then the app's yes was asked without the subject, a Cc or a Bcc.
        # A body is never on a card (`approvals.quoted_body`).
        note = {"kind": "code" if card.high_risk else "yes", "id": f"card:{card.id}",
                "action_id": card.id, "text": cut(lines[0], SHORT),
                "detail": "\n".join(cut(line, SHORT) for line in lines[1:]) or None,
                "word": card.token, "at": sqltime.iso_of(asked.get(card.id)), "expires": expires,
                "actions": ["yes", "no"], "seen": card.args_hash}
        group = approvals.items(card)
        if len(group) > 1:
            # A turn's cards joined into one (`approvals.held`). Its text
            # promised "some of them in the app", and the note showed the
            # first item alone, with a yes for all: each is listed, so a yes
            # can leave some out (`only`), unless a code bound all of them.
            note["text"] = (f"{len(group)} on one card, approved together" if card.high_risk
                            else f"{len(group)} on one card: untick any to leave out")
            # Each item whole too: by its first line, two emails from one
            # account read the same, and the user could not tell which to
            # untick.
            note["detail"] = None
            note["items"] = [_whole(item["preview"]) or card.tool for item in group]
        out.append(note)
    return out


def _whole(text: str) -> str:
    """A card's lines, each cut to `SHORT`, without the blank ones."""
    return "\n".join(cut(line.strip(), SHORT) for line in text.splitlines() if line.strip())


def _going_off(conn, now: datetime) -> list[dict]:
    """Reminders gone off, and critical notices, of the last `NOW_FOR`. A
    check's answer is an update, not a note: nothing is due."""
    out = []
    for r in conn.execute(
            "SELECT o.id, o.text, o.created_at, o.reminder_id, r.kind AS reminding"
            " FROM outbox o LEFT JOIN reminders r ON r.id = o.reminder_id"
            " WHERE o.created_at >= ? AND ((o.kind = 'reminder' AND r.kind = 'remind')"
            "  OR EXISTS (SELECT 1 FROM told t JOIN observations b ON b.id = t.obs_id"
            f"   WHERE t.outbox_id = o.id AND b.critical = 1"
            f"   AND {modules.live(conn, 'observations', 'b')}))"
            " ORDER BY o.id", (sqltime.stamp(now - NOW_FOR),)):
        reminder = r["reminding"] == "remind"
        out.append({"kind": "now", "id": f"{'reminder' if reminder else 'notice'}:{r['id']}",
                    "text": r["text"], "detail": None, "word": None,
                    "at": sqltime.iso_of(r["created_at"]),
                    "actions": ["done", "later"] if reminder else ["done"]})
    return out


def _stopped(conn) -> list[dict]:
    out = []

    def stop(note_id: str, text: str, at: str | None, fix: str) -> None:
        out.append({"kind": "stopped", "id": note_id, "text": text, "detail": None,
                    "word": None, "at": at, "actions": [fix, "not_now"]})

    for notice in caps.standing(conn):
        if notice["kind"] == "stopped":
            stop(f"cap:{notice['cap']}:{notice['period']}", notice["text"],
                 sqltime.iso_of(notice["at"]),
                 "top_up" if notice["cap"] == spend.BALANCE else "raise_cap")
    names = {a.key: a.name for a in modules.accounts(conn)}
    for key, found in sorted(health.every(conn).items()):
        if key in names and found.state == health.NEEDS_YOU:
            stop(f"account:{key}", found.says(names[key]), sqltime.iso(found.since), found.act())
    # A link that worked and was lost (`links.start_signal` says which), not
    # one the user removed, while no other channel reaches the phone. By
    # setup's answer, a link made later from Channels and lost said nothing,
    # and one removed on purpose for Telegram said every hour that nothing
    # reached the phone.
    if runtime.signal_state.get("lost") and links.remote_channel() != "telegram":
        stop("signal", SIGNAL_LOST, None, "link")
    return out


def _away(conn, now: datetime) -> set[tuple[str, str]]:
    return {(r["kind"], r["ref"]) for r in conn.execute(
        "SELECT kind, ref FROM put_off WHERE until IS NULL OR until > ?",
        (sqltime.stamp(now),))}


def needs(conn, now: datetime | None = None) -> list[dict]:
    """Every note, stopped first, then what is going off, then cards, the
    soonest to expire first; otherwise oldest first."""
    now = now or datetime.now(UTC)
    away = _away(conn, now)
    found = [n for n in (*_stopped(conn), *_cards(conn), *_going_off(conn, now))
             if (n["kind"], n["id"]) not in away]
    return sorted(found, key=lambda n: (_RANK[n["kind"]], n.get("expires") or _NEVER,
                                        n["at"] or ""))


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


def _put(conn, kind: str, note_id: str, until: datetime | None) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO put_off (kind, ref, until) VALUES (?, ?, ?)"
            " ON CONFLICT(kind, ref) DO UPDATE SET until = excluded.until, at = datetime('now')",
            (kind, note_id, sqltime.stamp(until) if until else None))


def done(conn, note_id: str) -> bool:
    """A reminder or notice seen to: its note goes, for good."""
    if not _NOW_ID.fullmatch(note_id):
        return False
    _put(conn, "now", note_id, None)
    return True


def not_now(conn, note_id: str, now: datetime | None = None) -> bool:
    """A stopped note put away for `NOT_NOW`. Then it comes back, if what
    stopped is still stopped: putting it off fixes nothing."""
    if not _STOPPED_ID.fullmatch(note_id):
        return False
    _put(conn, "stopped", note_id, (now or datetime.now(UTC)) + NOT_NOW)
    return True


def later_at(when: str, now: datetime) -> datetime:
    """When "Later" means: in 15 minutes or an hour, or at the next evening
    or morning briefing (`settings.EVENING`, `MORNING`, which the user may
    have moved)."""
    if when in LATER_BY:
        return now + LATER_BY[when]
    at = {"evening": settings.EVENING, "morning": settings.MORNING}[when]
    local = now.astimezone(clock.local_zone())
    then = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return then if then > local else then + timedelta(days=1)


def later(conn, note_id: str, when: str, now: datetime | None = None) -> str | None:
    """A reminder gone off, booked again at `when` as a one-off: a repeating
    reminder's series isn't moved. Its note is answered. Returns when it
    goes off again, or None for a note that isn't a reminder's."""
    now = now or datetime.now(UTC)
    if when not in LATER or not note_id.startswith("reminder:") or not _NOW_ID.fullmatch(
            note_id):
        return None
    row = conn.execute(
        "SELECT r.text, r.session_id FROM outbox o JOIN reminders r ON r.id = o.reminder_id"
        " WHERE o.id = ? AND r.kind = 'remind'", (int(note_id.split(":")[1]),)).fetchone()
    if row is None:
        return None
    at = later_at(when, now)
    reminders.schedule(conn, kind="remind", text=row["text"], session_id=row["session_id"],
                       when=_local(at).isoformat(timespec="seconds"), now=_local(now))
    _put(conn, "now", note_id, None)
    return sqltime.iso(at)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _listed(conn) -> dict:
    return {"ok": True, "needs": needs(conn)}


async def get_needs(_: Request) -> JSONResponse:
    return JSONResponse(await offloop.with_own_conn(runtime.conn, _listed))


async def post_done(request: Request) -> JSONResponse:
    """`{"id": "reminder:12"}`: a reminder or notice seen to."""
    note_id = str((await bodies.body(request)).get("id") or "")
    if not await offloop.with_own_conn(runtime.conn, done, note_id):
        return bodies.bad(f"{note_id!r} is not a reminder's or a notice's note")
    return JSONResponse({"ok": True})


async def post_later(request: Request) -> JSONResponse:
    """`{"id": "reminder:12", "when": "15m" | "1h" | "evening" | "morning"}`."""
    body = await bodies.body(request)
    note_id, when = str(body.get("id") or ""), str(body.get("when") or "")
    if when not in LATER:
        return bodies.bad(f"later is one of {', '.join(LATER)}")
    at = await offloop.with_own_conn(runtime.conn, later, note_id, when)
    if at is None:
        return bodies.bad(f"{note_id!r} is not a reminder's note")
    return JSONResponse({"ok": True, "at": at})


async def post_not_now(request: Request) -> JSONResponse:
    """`{"id": "account:gmail"}`: a stopped note, away for an hour."""
    note_id = str((await bodies.body(request)).get("id") or "")
    if not await offloop.with_own_conn(runtime.conn, not_now, note_id):
        return bodies.bad(f"{note_id!r} is not a stopped note")
    return JSONResponse({"ok": True})


ROUTES = [
    Route("/control/needs", get_needs),
    Route("/control/needs/done", post_done, methods=["POST"]),
    Route("/control/needs/later", post_later, methods=["POST"]),
    Route("/control/needs/not-now", post_not_now, methods=["POST"]),
]
