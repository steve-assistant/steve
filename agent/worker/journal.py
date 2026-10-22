"""The journal's backend: one day of everything, as the day page reads it
(docs/design/journal.md, "What the backend needs to supply").

The app showed what happened in three places, and none could say what
happened on a day: a transcript of one conversation at a time, an inbox of
one table, and a feed of the last 48 hours. A reply on Signal, a card
answered from the phone and a briefing were each in a different one, or in
none. So:

- **One day, every source, in order** (`entries`): the user's
  conversations on every channel, the outbox, actions with what came of
  them, background tasks, listening notes and the computer's sleeps. A day
  is the user's, from 4 a.m. to 4 a.m. in their zone (`PAGE_TURNS_AT`), so
  a talk at 1 a.m. stays on the day it belongs to. Spend's day still turns
  at midnight.
- **Each message once.** A message from the outbox joins the conversation
  once the user has it (agent/outbox.py), stored as an unprompted reply.
  Read from both, a briefing showed twice. The outbox's row is the entry;
  its copy in the conversation is not.
- **One clock.** An action's asking and deciding are written in local wall
  time (agent/approvals.py), and so is a reminder's next firing; the rest
  is SQLite's UTC. Sorted together as text, a card decided in the morning
  sat hours from where it happened. Every moment here leaves as UTC, with
  its zone.
- **A poll that sends only what changed** (`since`). The open page asks
  every few seconds, and an entry that changed (an action decided, a spoken
  exchange grown) comes again under the same key, to be replaced.
- **A spoken exchange is summed up in a line** (`sum_up_spoken`) once it
  ends, by one small model call under the caps. Folded under the user's
  first words, often only "Hey", it said nothing of what was talked about.
- **The night's work folds the same way** (`_night`), under two sentences
  read from its report, with no model call: "The night's work" and "done"
  said nothing of what it did.
- **New since you last looked:** one moment (`looked`), not read state per
  entry: the design needs nothing finer.
- **Search** (`search`): nothing searched the journal, so a thing said
  weeks ago was found by scrolling through every day between. The words
  choose the pages in SQL; the pages' own entries are then matched, so a
  hit is exactly what the page shows, at the key the page opens to.
- **What Steve is doing** goes over the socket as a short phrase for each
  tool (`doing`) and each background task taken up, so the page's working
  line says "Checking your calendar" rather than showing dots.

Every route is the app's, behind the local-only check and this start's
token.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import approvals, calendars, issues, offloop, runtime, sqltime
from agent.attention.briefing import key_for
from agent.cut import cut
from agent.history import UNPROMPTED
from agent.worker import bodies, links
from agent.worker.control import FEED_JOBS, job_line
from core import clock, db, spend
from core.call_policy import PRESENT_CHANNELS
from voice.speaking import preamble_for

# The page turns here, in the user's zone (the plan's Question 6).
PAGE_TURNS_AT = time(4, 0)
# Voice turns closer than this are one spoken exchange.
SPOKEN_GAP = timedelta(minutes=5)
# What's next: the next event and reminder within this.
NEXT_WITHIN = timedelta(hours=3)
# The calendar is asked at most this often for what's next: each ask reaches
# every calendar account, and the sidebar asks every minute.
NEXT_FRESH_S = 60.0
# A poll's `since` reaches this far back. A row written as the last poll
# read can commit after it, stamped a moment before it.
SINCE_SLACK = timedelta(seconds=5)
# Folded days asked for at once.
MOST_DAYS = 31
# A first line, cut.
SHORT = 200

_FIRST_SENTENCE = re.compile(r"\S.*?[.!?](?=\s|$)")
_VERDICTS = {"right": ("feedback_up", "thumbs-up"), "not right": ("feedback_down", "thumbs-down")}
_BY = {"signal": " on Signal", "voice": " aloud"}


# ---------------------------------------------------------------------------
# Days and moments
# ---------------------------------------------------------------------------


def page_of(moment: datetime) -> date:
    """The page a moment is on: its day in the user's zone, turned at 4 a.m."""
    local = moment.astimezone(clock.local_zone())
    turned = timedelta(hours=PAGE_TURNS_AT.hour, minutes=PAGE_TURNS_AT.minute)
    return (local - turned).date()


def bounds(day: date) -> tuple[str, str]:
    """A page's first and last moments, as SQLite's UTC text: [start, end)."""
    zone = clock.local_zone()
    start = datetime.combine(day, PAGE_TURNS_AT, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), PAGE_TURNS_AT, tzinfo=zone)
    return sqltime.stamp(start), sqltime.stamp(end)


def _parsed(text: str) -> datetime:
    """ISO with a zone as given; without one, the user's wall time."""
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=clock.local_zone())


def _from_local(text: str | None) -> str | None:
    """Local wall time (an action's decision) in that form."""
    if not text:
        return None
    try:
        return sqltime.iso(_parsed(text))
    except ValueError:
        return None


def _latest(*moments: str | None) -> str | None:
    return max(filter(None, moments), default=None)


def _clock_time(iso: str | None) -> str:
    """"10:42" in the user's zone."""
    if not iso:
        return ""
    return _parsed(iso).astimezone(clock.local_zone()).strftime("%H:%M")


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


# The line a message's photos are recorded under (channels/photos.py `note`).
_ATTACHED = re.compile(r"\A\[\d+ photos? attached\]\n?")


def _conversation(conn, start: str, end: str) -> list[dict]:
    """The user's words and Steve's replies, on every channel the user talks
    on; a voice session's turns gathered into spoken exchanges.

    Only those: Steve's own conversations -- a night's research, a routine's
    run, a background task -- open with words Steve wrote for itself, and
    the page drew them as the user's ("Research this and write down what
    you learn: ..."). What came of them is on the page already, from the
    outbox and the tasks."""
    present = sorted(PRESENT_CHANNELS)
    rows = conn.execute(
        "SELECT m.id, m.session_id, m.role, m.content, m.model, m.created_at, s.channel,"
        " r.verdict, r.at AS reacted, ss.summary, ss.made_at AS summed,"
        " (SELECT COUNT(*) FROM message_photos p WHERE p.message_id = m.id) AS photos"
        " FROM messages m JOIN sessions s ON s.id = m.session_id"
        " LEFT JOIN reactions r ON r.message_id = m.id"
        " LEFT JOIN spoken_summaries ss ON ss.first_id = m.id"
        " WHERE m.created_at >= ? AND m.created_at < ? AND m.role IN ('user', 'assistant')"
        f" AND s.channel IN ({', '.join('?' * len(present))})"
        " ORDER BY m.created_at, m.id", (start, end, *present)).fetchall()
    out: list[dict] = []
    talking: dict[int, dict] = {}
    for r in rows:
        text = (r["content"] or "").strip()
        # A told outbox row, kept so a reply finds what it answers: the
        # outbox's own entry shows it.
        if not text or (r["role"] == "assistant" and r["model"] == UNPROMPTED):
            continue
        at = sqltime.iso_of(r["created_at"])
        if r["channel"] == "voice":
            spoken = talking.get(r["session_id"])
            if spoken is None or (_parsed(at) - _parsed(spoken["ended"])) >= SPOKEN_GAP:
                # Its summary is keyed by its first message, as it is named.
                spoken = {"key": f"spoken:{r['id']}", "kind": "spoken", "at": at,
                          "channel": "voice", "session_id": r["session_id"], "id": r["id"],
                          "lines": [], "first_words": None, "summary": r["summary"] or None,
                          "summed": sqltime.iso_of(r["summed"])}
                talking[r["session_id"]] = spoken
                out.append(spoken)
            spoken["lines"].append({"role": r["role"], "text": text, "at": at})
            spoken["ended"] = spoken["changed"] = at
            if spoken["first_words"] is None and r["role"] == "user":
                spoken["first_words"] = cut(text, SHORT)
            continue
        said = r["role"] == "user"
        entry = {"key": f"{'said' if said else 'reply'}:{r['id']}",
                 "kind": "said" if said else "reply", "at": at, "changed": at,
                 "channel": r["channel"], "session_id": r["session_id"], "id": r["id"],
                 "text": text}
        if said:
            # Shown on the message (app_photos.get_photo), in place of the
            # line the conversation records for them (photos.note).
            entry["photos"] = r["photos"]
            if r["photos"]:
                entry["text"] = _ATTACHED.sub("", text, count=1).strip()
        else:
            entry["reaction"] = r["verdict"]
            entry["changed"] = _latest(at, sqltime.iso_of(r["reacted"]))
        out.append(entry)
    for spoken in out:
        if spoken["kind"] == "spoken":
            seconds = (_parsed(spoken["ended"]) - _parsed(spoken["at"])).total_seconds()
            spoken["minutes"] = max(1, -int(-seconds // 60))
            # A summary made later changes the entry, so the open page's
            # poll brings it.
            spoken["changed"] = _latest(spoken["ended"], spoken.pop("summed"))
    return out


def _outbox(conn, start: str, end: str) -> list[dict]:
    """Briefings, and the updates Steve sent unasked. A card is left out:
    its action's entry carries it."""
    out = []
    for r in conn.execute(
            "SELECT o.id, o.kind, o.text, o.remote, o.created_at, o.sent_at, o.stopped,"
            " o.changed_at,"
            " (SELECT t.how FROM told t WHERE t.outbox_id = o.id LIMIT 1) AS how"
            " FROM outbox o WHERE o.created_at >= ? AND o.created_at < ? AND o.kind != 'card'",
            (start, end)):
        at, sent = sqltime.iso_of(r["created_at"]), sqltime.iso_of(r["sent_at"])
        briefing = r["kind"] == "briefing"
        out.append({"key": f"outbox:{r['id']}", "kind": "briefing" if briefing else "update",
                    "of": r["kind"], "at": at,
                    "changed": _latest(at, sent, sqltime.iso_of(r["changed_at"])),
                    "channel": r["remote"], "id": r["id"], "text": r["text"],
                    "which": r["how"] if briefing and r["how"] in ("morning", "evening") else None,
                    "sent": sent, "stopped": r["stopped"]})
    return out


def _outcome(r, what: str, executed: str | None) -> str:
    """What the stream says came of an action."""
    status, by = r["status"], r["decided_by"]
    if status == "pending":
        return f"Asked you about: {what}"
    if status == "expired":
        return "Expired · nothing was done"
    if status == "withdrawn":
        return f"Withdrawn · {r['withdrawn_reason'] or 'nothing was done'}"
    if by == "auto":
        return {"failed": f"Couldn't: {what}", "undone": f"{what} · then undone"}.get(status, what)
    where = _BY.get(by or "", "")
    if status == "rejected":
        return f"You said no{where}"
    yes = f"You said yes{where}"
    return {"approved": f"{yes} · under way", "executing": f"{yes} · under way",
            "done": f"{yes} · done {_clock_time(executed)}".rstrip(),
            "failed": f"{yes} · it failed", "undone": f"{yes} · then undone"}.get(status, yes)


def _actions(conn, start: str, end: str) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT id, tool, preview, status, reversal, channel, decided_by, error, created_at,"
            " decided_at, executed_at, undone_at, withdrawn_reason, changed_at FROM actions"
            " WHERE created_at >= ? AND created_at < ?", (start, end)):
        at, executed = sqltime.iso_of(r["created_at"]), sqltime.iso_of(r["executed_at"])
        decided = _from_local(r["decided_at"])
        what = cut(next(iter((r["preview"] or "").strip().splitlines()), r["tool"]), SHORT)
        out.append({"key": f"action:{r['id']}", "kind": "action", "at": at,
                    "changed": _latest(at, decided, executed, sqltime.iso_of(r["undone_at"]),
                                       sqltime.iso_of(r["changed_at"])),
                    "channel": r["channel"], "id": r["id"], "status": r["status"],
                    "asked": r["decided_by"] != "auto", "by": r["decided_by"], "what": what,
                    "line": _outcome(r, what, executed), "error": r["error"],
                    "decided": decided, "executed": executed,
                    "undo_until": _undo_until(r, decided)})
    return out


def _undo_until(r, decided: str | None) -> str | None:
    """When the hold before an irreversible action ends (agent/approvals.py
    `HOLD_SECONDS`): until then the page offers Undo. Counted from the yes,
    which queues the action at once; the hold starts when it runs, so the
    page's window closes a little before the real one, never after."""
    if r["status"] != "executing" or r["reversal"] != "irreversible" or not decided:
        return None
    return sqltime.iso(_parsed(decided) + timedelta(seconds=approvals.HOLD_SECONDS))


def _tasks(conn, start: str, end: str) -> list[dict]:
    """Background tasks, lined as the activity feed lines them."""
    out = []
    marks = ",".join("?" * len(FEED_JOBS))
    for r in conn.execute(
            f"SELECT id, kind, prompt, why, status, result, error, created_at, started_at,"
            f" finished_at FROM jobs WHERE kind IN ({marks}) AND created_at >= ?"
            f" AND created_at < ?", (*FEED_JOBS, start, end)):
        title, detail = job_line(r)
        at, finished = sqltime.iso_of(r["created_at"]), sqltime.iso_of(r["finished_at"])
        out.append({"key": f"task:{r['id']}", "kind": "task", "of": r["kind"], "at": at,
                    "changed": _latest(at, sqltime.iso_of(r["started_at"]), finished),
                    "channel": None,
                    "id": r["id"], "title": title, "detail": detail, "status": r["status"],
                    "finished": finished})
        if r["kind"] == "night" and r["status"] == "done":
            out[-1].update(_night(r["result"]))
    return out


def _night(result: str | None) -> dict:
    """A night's entry, folded as a spoken exchange is: a summary of two
    sentences and the parts it opens to, read from the night's report.
    It said "The night's work" and "done", and nothing of what the night
    did. A report it cannot read leaves the plain line."""
    from agent.night.report import NightReport, sections, summary

    try:
        report = NightReport.from_dict(json.loads(result or "null"))
    except (ValueError, TypeError, AttributeError):
        return {}
    return {"summary": summary(report), "minutes": max(1, round(report.seconds / 60)),
            "parts": [{"title": title, "items": items} for title, items in sections(report)]}


def _listening(conn, start: str, end: str) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT id, kind, title, body, doc_url, status, started_at, ended_at, changed_at"
            " FROM notes"
            " WHERE started_at >= ? AND started_at < ?", (start, end)):
        at, ended = sqltime.iso_of(r["started_at"]), sqltime.iso_of(r["ended_at"])
        # The text and the summary's link: the day is the only place the app
        # shows a listening note, so it is where one is read and deleted.
        out.append({"key": f"listening:{r['id']}", "kind": "listening", "of": r["kind"],
                    "at": at, "changed": _latest(at, ended, sqltime.iso_of(r["changed_at"])),
                    "channel": None,
                    "id": r["id"],
                    "title": r["title"], "text": r["body"], "doc_url": r["doc_url"],
                    "status": r["status"], "ended": ended})
    return out


def _sleeps(conn, start: str, end: str) -> list[dict]:
    """Where the computer slept, or Steve wasn't running, on the page it
    woke on: "Asleep 23:50 – 07:12" opens the morning it ended."""
    out = []
    for r in conn.execute(
            "SELECT id, since, gap_from, gap FROM awake"
            " WHERE gap IS NOT NULL AND since >= ? AND since < ?", (start, end)):
        began, woke = sqltime.iso_of(r["gap_from"]), sqltime.iso_of(r["since"])
        out.append({"key": f"asleep:{r['id']}", "kind": "asleep", "at": began or woke,
                    "changed": woke, "channel": None, "id": r["id"], "gap": r["gap"],
                    "from": began, "to": woke})
    return out


TALK = ("said", "reply", "spoken")


def _order(entry: dict) -> tuple:
    """An entry's place on the page: by its moment, and within one second
    the conversation first, in the order it was said (its message ids),
    then the rest by key. Moments are whole seconds, and sorted by key
    alone "reply:9" came before "said:8": a quick reply sat above the
    message it answered (19 October 2026, step 10's end-to-end chat)."""
    talk = entry["kind"] in TALK
    return (entry["at"] or "", not talk, entry["id"] if talk else 0, entry["key"])


def entries(conn, day: date, *, since: str | None = None) -> list[dict]:
    """One page's entries in order. With `since` (ISO), only those that
    changed from a little before it (`SINCE_SLACK`): the page replaces each
    by its key."""
    start, end = bounds(day)
    found = [*_conversation(conn, start, end), *_outbox(conn, start, end),
             *_actions(conn, start, end), *_tasks(conn, start, end),
             *_listening(conn, start, end), *_sleeps(conn, start, end)]
    looked = looked_until(conn)
    for entry in found:
        # The user's own words are never news to them. A card decided on the
        # phone, or a task finished, after they looked is.
        entry["new"] = bool(looked) and entry["kind"] not in ("said", "spoken") and (
            entry["changed"] or "") > looked
    found.sort(key=_order)
    if since:
        cutoff = sqltime.iso(_parsed(since) - SINCE_SLACK)
        found = [entry for entry in found if (entry["changed"] or "") >= cutoff]
    return found


def fold_line(conn, day: date, found: list[dict]) -> str:
    """A folded day's one line: that evening's briefing's first sentence,
    or, without one, the count of exchanges and things done. No model."""
    # Known by the day it looks ahead to, as `briefing.send` records it: by
    # its own day, a page showed the evening before's look-ahead.
    row = conn.execute(
        "SELECT o.text FROM told t JOIN outbox o ON o.id = t.outbox_id"
        " WHERE t.message = ? ORDER BY t.id LIMIT 1",
        (key_for("evening", day + timedelta(days=1)),)).fetchone()
    first = next((line.strip() for line in (row["text"] if row else "").splitlines()
                  if line.strip()), "")
    if first:
        sentence = _FIRST_SENTENCE.match(first)
        return cut(sentence.group(0) if sentence else first, SHORT)
    talks = sum(1 for e in found if e["kind"] in ("said", "spoken"))
    done = sum(1 for e in found if e["kind"] in ("action", "task") and e["status"] == "done")
    parts = [f"{n} {one if n == 1 else many}" for n, one, many in (
        (talks, "exchange", "exchanges"), (done, "thing done", "things done")) if n]
    return ", ".join(parts) or "A quiet day"


def days(conn, before: date, n: int) -> list[dict]:
    """The `n` pages before `before`, folded: date, line and count."""
    out = []
    for back in range(1, max(1, min(n, MOST_DAYS)) + 1):
        day = before - timedelta(days=back)
        found = entries(conn, day)
        out.append({"date": day.isoformat(), "line": fold_line(conn, day, found),
                    "count": len(found)})
    return out


# ---------------------------------------------------------------------------
# Looked, reactions, what's next
# ---------------------------------------------------------------------------


def looked_until(conn) -> str | None:
    row = conn.execute("SELECT until FROM looked WHERE id = 1").fetchone()
    return sqltime.iso_of(row["until"]) if row else None


def look(conn, until: datetime | None = None, *, now: datetime | None = None) -> str | None:
    """The user has seen the page up to `until` (now by default, never
    later). It never moves back: a window behind another says nothing."""
    now = now or datetime.now(UTC)
    moment = min(until, now) if until else now
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO looked (id, until) VALUES (1, ?)"
            " ON CONFLICT(id) DO UPDATE SET until = max(until, excluded.until)",
            (sqltime.stamp(moment),))
    return looked_until(conn)


def unseen_by_look(conn) -> list[int]:
    """The outbox's messages written by the moment the user looked, not yet
    marked seen: cards too, which the day draws as their actions."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM outbox WHERE seen_at IS NULL"
        " AND created_at <= (SELECT until FROM looked WHERE id = 1) ORDER BY id")]


def react(conn, message_id: int, verdict: str | None) -> bool:
    """The user's mark on a reply: kept for the page to draw, and counted in
    the failure ledger (agent/issues.py) as Signal's thumbs are. The same
    mark twice is one opinion, counted once; None takes it off."""
    reply = conn.execute("SELECT id, session_id, content FROM messages"
                         " WHERE id = ? AND role = 'assistant'", (message_id,)).fetchone()
    if reply is None or (verdict is not None and verdict not in _VERDICTS):
        return False
    held = conn.execute("SELECT verdict FROM reactions WHERE message_id = ?",
                        (message_id,)).fetchone()
    with db.transaction(conn):
        if verdict is None:
            conn.execute("DELETE FROM reactions WHERE message_id = ?", (message_id,))
        else:
            conn.execute(
                "INSERT INTO reactions (message_id, verdict) VALUES (?, ?)"
                " ON CONFLICT(message_id) DO UPDATE SET verdict = excluded.verdict,"
                " at = datetime('now')", (message_id, verdict))
    if verdict is not None and (held is None or held["verdict"] != verdict):
        kind, word = _VERDICTS[verdict]
        on = reply["content"]
        issues.record(conn, kind, on[:SHORT], f"The user gave a {word} to: {on[:140]}",
                      excerpt=on, session_id=reply["session_id"], message_id=reply["id"])
    return True


# The next event, as last asked: when (by `monotonic`), and what it was.
_next_event: dict = {"at": None, "event": None}


def next_up(conn, now: datetime | None = None) -> dict:
    """The next timed event and the next reminder within `NEXT_WITHIN`."""
    now = now or datetime.now(UTC)
    return {"event": _event_next(conn, now), "reminder": _reminder_next(conn, now)}


def _event_next(conn, now: datetime) -> dict | None:
    if _next_event["at"] is not None and monotonic() - _next_event["at"] < NEXT_FRESH_S:
        return _next_event["event"]
    event = None
    for e in calendars.agenda(conn, 1, limit=10).get("events", []):
        start = _starts(e.get("start"))
        if e.get("all_day") or start is None:
            continue
        if now <= start <= now + NEXT_WITHIN:
            event = {"summary": e.get("summary"), "start": sqltime.iso(start),
                     "end": sqltime.iso(_starts(e.get("end")) or start),
                     "location": e.get("location")}
            break
    _next_event.update(at=monotonic(), event=event)
    return event


def _starts(text) -> datetime | None:
    """A timed event's start; None for a date alone."""
    if not isinstance(text, str) or "T" not in text:
        return None
    try:
        return _parsed(text)
    except ValueError:
        return None


def _reminder_next(conn, now: datetime) -> dict | None:
    # A reminder's next firing is local wall time, as `reminders.due` reads it.
    local = now.astimezone(clock.local_zone()).replace(tzinfo=None)
    row = conn.execute(
        "SELECT id, text, next_fire_at FROM reminders WHERE status = 'active'"
        " AND next_fire_at >= ? AND next_fire_at <= ? ORDER BY next_fire_at LIMIT 1",
        (local.isoformat(timespec="seconds"),
         (local + NEXT_WITHIN).isoformat(timespec="seconds"))).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "text": row["text"], "at": sqltime.iso(_parsed(row["next_fire_at"]))}


# ---------------------------------------------------------------------------
# Spoken exchanges' summaries
# ---------------------------------------------------------------------------

# The most of an exchange the summary's model reads.
SPOKEN_READ = 6000
SUM_UP = ("Below is a spoken exchange between a person and Steve, their assistant. "
          "Write one line of at most twelve words for the person's own journal: what "
          "it was about, and what came of it. No preamble and no quotation marks.")


def _ended(conn, now: datetime) -> list[dict]:
    """The spoken exchanges on today's page and yesterday's that have ended,
    with no turn for `SPOKEN_GAP`, and have no summary yet. Gathered as the
    page gathers them, so each summary's key is the one the page shows. An
    exchange in which the user said one thing needs none: its first words
    are its summary."""
    today = page_of(now)
    found = [e for day in (today - timedelta(days=1), today)
             for e in _conversation(conn, *bounds(day)) if e["kind"] == "spoken"]
    if not found:
        return []
    tried = {r["first_id"] for r in conn.execute(
        "SELECT first_id FROM spoken_summaries WHERE first_id >= ?",
        (min(e["id"] for e in found),))}
    return [e for e in found
            if e["id"] not in tried and _parsed(e["ended"]) <= now - SPOKEN_GAP
            and sum(line["role"] == "user" for line in e["lines"]) > 1]


def _keep(conn, first_id: int, summary: str) -> None:
    # Stamped as it is written, not with the sweep's moment: the model's
    # answer can take longer than the open page's poll, which would then
    # have passed the summary by.
    with db.transaction(conn):
        conn.execute("INSERT OR IGNORE INTO spoken_summaries (first_id, summary) VALUES (?, ?)",
                     (first_id, summary))


async def sum_up_spoken(conn, now: datetime | None = None) -> int:
    """Sum each ended spoken exchange up in a line, one small model call
    each, under the caps; how many were summed up.

    The exchange's words go to the model service they were spoken to, as
    they were then. Redacted, as reflection's are, the line would name no
    one, and it is for the user's own page, not a lesson kept."""
    now = now or datetime.now(UTC)
    made = 0
    for exchange in await offloop.with_own_conn(conn, _ended, now):
        said = "\n".join(f"{'Steve' if line['role'] == 'assistant' else 'Person'}: {line['text']}"
                         for line in exchange["lines"])
        with spend.spending("background", part="work", conn=conn):
            if spend.may_spend(conn):
                break
            written = await _write(SUM_UP, cut(said, SPOKEN_READ))
        line = next((t.strip().strip('"“”') for t in written.splitlines() if t.strip()), "")
        await offloop.with_own_conn(conn, _keep, exchange["id"], cut(line, SHORT))
        made += 1
    return made


async def _write(system: str, prompt: str) -> str:
    """The one model call here. The tests replace it."""
    # Here, not at the top: agent.llm brings the model libraries, which the
    # journal's routes never need.
    from agent.llm import ask_once
    from agent.models import Role

    output, _ = await ask_once(Role.BULK, system, prompt)
    return str(output or "")


# ---------------------------------------------------------------------------
# What Steve is doing
# ---------------------------------------------------------------------------


def doing(tool: str) -> str | None:
    """A tool as the working line says it: the voice's own words for it
    (voice/speaking.py), or None for one that acts, which is reported
    when it is done rather than announced."""
    said = preamble_for(tool)
    return said.rstrip(".") if said else None


class TurnLine:
    """A chat turn's working line: each tool's phrase as the model reaches
    for it, never the same twice running, and the line taken down when the
    turn ends (`done`).

    The window that asked hears it as "turn". Every other open window hears
    it too, under a key of this turn's own: the desk card says what Steve is
    doing whichever window was asked, and it had nothing to say while the
    user talked in the quick bar, since only the asking socket heard."""

    _turns = itertools.count(1)

    def __init__(self, ws) -> None:
        self.ws = ws
        self.said: str | None = None
        self.of = f"turn:{next(self._turns)}"

    def __call__(self, tool: str) -> None:
        text = doing(tool)
        if text and text != self.said:
            self.said = text
            self._send(text)

    def done(self) -> None:
        if self.said is not None:
            self.said = None
            self._send(None)

    def _send(self, text: str | None) -> None:
        with contextlib.suppress(Exception):
            asyncio.create_task(self.ws.send_json({"type": "doing", "text": text, "of": "turn"}))
        for ws in [w for w in runtime.listeners if w is not self.ws]:
            with contextlib.suppress(Exception):
                asyncio.create_task(ws.send_json({"type": "doing", "text": text, "of": self.of}))


def tell(text: str | None, of: str) -> None:
    """Every open window hears what Steve is doing (`of`: "turn" or
    "job:<id>"), and None once it stops."""
    for ws in list(runtime.listeners):
        with contextlib.suppress(Exception):
            asyncio.create_task(ws.send_json({"type": "doing", "text": text, "of": of}))


def job_started(job) -> None:
    if job.kind in FEED_JOBS:
        title, _ = job_line({"kind": job.kind, "why": job.why, "prompt": job.prompt,
                             "result": None, "error": None, "status": "running"})
        tell(f"Working on: {cut(title, 80)}", f"job:{job.id}")


def job_stopped(job) -> None:
    if job.kind in FEED_JOBS:
        tell(None, f"job:{job.id}")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

SEARCH_WORDS = 6
SEARCH_DAYS = 20
# Lines shown a day; the rest are counted, and the day holds them.
SEARCH_HITS = 8
# The distinct minutes each table offers: a common word finds its most
# recent days, not every minute it was ever said.
SEARCH_MINUTES = 1000
SNIPPET = 140

# Where the words are kept, and when, a minute at a time. The journal's own
# entries are what is matched: these only say which pages to read, so a row
# found here whose entry does not hold every word shows nothing. "!" escapes
# LIKE's wildcards, where a backslash would need escaping itself.
_WHERE = (
    "SELECT DISTINCT substr(m.created_at, 1, 16) FROM messages m"
    " LEFT JOIN spoken_summaries ss ON ss.first_id = m.id"
    " WHERE m.content LIKE :w ESCAPE '!' OR ss.summary LIKE :w ESCAPE '!'",
    "SELECT DISTINCT substr(created_at, 1, 16) FROM outbox"
    " WHERE kind != 'card' AND text LIKE :w ESCAPE '!'",
    "SELECT DISTINCT substr(created_at, 1, 16) FROM actions"
    " WHERE preview LIKE :w ESCAPE '!' OR tool LIKE :w ESCAPE '!' OR error LIKE :w ESCAPE '!'",
    "SELECT DISTINCT substr(created_at, 1, 16) FROM jobs"
    " WHERE prompt LIKE :w ESCAPE '!' OR why LIKE :w ESCAPE '!' OR error LIKE :w ESCAPE '!'",
    "SELECT DISTINCT substr(started_at, 1, 16) FROM notes"
    " WHERE title LIKE :w ESCAPE '!' OR body LIKE :w ESCAPE '!'",
)


def _pages_with(conn, word: str) -> list[date]:
    """The most recent pages where `word` was written, newest first. LIKE
    ignores case in ASCII only: "Åsa" asked chooses the pages where it is
    written so, and not those with "åsa" alone."""
    like = "%" + re.sub(r"([!%_])", r"!\1", word) + "%"
    found: set[date] = set()
    for sql in _WHERE:
        for (minute,) in conn.execute(f"{sql} ORDER BY 1 DESC LIMIT :n",
                                      {"w": like, "n": SEARCH_MINUTES}):
            moment = sqltime.moment(minute) if minute else None
            if moment:
                found.add(page_of(moment))
    return sorted(found, reverse=True)[:SEARCH_DAYS]


def _shown(entry: dict) -> list[str]:
    """The words an entry shows, each where it can stand as a line."""
    if entry["kind"] == "spoken":
        return [entry["summary"] or "", *(line["text"] for line in entry["lines"])]
    return [entry.get(k) or "" for k in ("text", "title", "what", "line", "detail", "error")]


# Markdown's marks at a line's start and around words: the page draws them,
# and a found line showed them as typed ("Morning. - Dentist at 3").
_MARKS = re.compile(r"^[ \t]*(?:[-*+>]|#+|\d+[.)])[ \t]+|\*\*|__|`", re.MULTILINE)


def _snippet(text: str, words: list[re.Pattern]) -> str:
    """The line from a little before its first match, its lines run
    together without their marks, cut at words, marked where cut."""
    text = " · ".join(" ".join(line.split()) for line in _MARKS.sub("", text).splitlines()
                      if line.strip())
    if len(text) <= SNIPPET:
        return text
    first = min((hit.start() for w in words if (hit := w.search(text))), default=0)
    start = max(0, first - SNIPPET // 3)
    if start:
        start = text.find(" ", start) + 1 or start
    return ("…" if start else "") + cut(text[start:], SNIPPET, mark="…")


def search(conn, q: str) -> dict:
    """The entries holding every word of `q`, by page, newest page first:
    each hit's key, kind, moment and the line it was found in."""
    words = [re.compile(re.escape(w), re.IGNORECASE) for w in q.split()[:SEARCH_WORDS]]
    today = page_of(datetime.now(UTC))
    out: list[dict] = []
    if words:
        # The longest word is the rarest, likely: it chooses the pages.
        for day in _pages_with(conn, max(q.split()[:SEARCH_WORDS], key=len)):
            hits = []
            for entry in entries(conn, day):
                shown = [t for t in _shown(entry) if t]
                if not all(any(w.search(t) for t in shown) for w in words):
                    continue
                # The line that holds the most of the words.
                best = max(shown, key=lambda t: sum(bool(w.search(t)) for w in words))
                hits.append({"key": entry["key"], "kind": entry["kind"], "at": entry["at"],
                             "line": _snippet(best, words)})
            if hits:
                out.append({"date": day.isoformat(), "hits": hits[:SEARCH_HITS],
                            "more": max(0, len(hits) - SEARCH_HITS)})
    return {"ok": True, "q": q, "today": today.isoformat(), "days": out}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _day(conn, day: date | None, since: str | None) -> dict:
    now = datetime.now(UTC)
    today = page_of(now)
    day = day or today
    return {"ok": True, "date": day.isoformat(), "today": today.isoformat(),
            "turns_at": PAGE_TURNS_AT.strftime("%H:%M"), "as_of": sqltime.iso(now),
            "looked": looked_until(conn), "entries": entries(conn, day, since=since)}


async def get_day(request: Request) -> JSONResponse:
    """`?date=YYYY-MM-DD` (today's page by default), and `?since=` the last
    answer's `as_of` for only what changed."""
    query = request.query_params
    try:
        day = date.fromisoformat(query["date"]) if query.get("date") else None
        since = query.get("since") or None
        if since:
            _parsed(since)
    except ValueError as exc:
        return bodies.bad(str(exc))
    return JSONResponse(await offloop.with_own_conn(runtime.conn, _day, day, since))


def _folded(conn, before: date | None, n: int) -> dict:
    return {"ok": True, "days": days(conn, before or page_of(datetime.now(UTC)), n)}


async def get_days(request: Request) -> JSONResponse:
    """`?before=YYYY-MM-DD&n=7`: the pages before it, folded; before today's
    by default."""
    query = request.query_params
    try:
        before = date.fromisoformat(query["before"]) if query.get("before") else None
        n = int(query.get("n") or 7)
    except ValueError as exc:
        return bodies.bad(str(exc))
    return JSONResponse(await offloop.with_own_conn(runtime.conn, _folded, before, n))


async def post_looked(request: Request) -> JSONResponse:
    """`{"until": ISO}`, or nothing for now."""
    given = (await bodies.body(request)).get("until")
    try:
        until = _parsed(given) if given else None
    except (TypeError, ValueError, AttributeError) as exc:
        return bodies.bad(str(exc))
    looked = await offloop.with_own_conn(runtime.conn, look, until)
    # What the page showed up to then, the user has seen: each message joins
    # the app's conversation, so a reply typed there finds what it answers.
    # The inbox did this for the rows it drew; the day draws them all now,
    # and without this the inbox's going left every message unseen, its
    # lines in `told` unsaid and the next briefing repeating them.
    links.seen_in_app(await offloop.with_own_conn(runtime.conn, unseen_by_look))
    return JSONResponse({"ok": True, "looked": looked})


async def post_react(request: Request) -> JSONResponse:
    """`{"message_id": n, "verdict": "right" | "not right" | null}`."""
    body = await bodies.body(request)
    message_id, verdict = body.get("message_id"), body.get("verdict")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return bodies.bad("message_id must be a number")
    if not await offloop.with_own_conn(runtime.conn, react, message_id, verdict):
        return bodies.bad(f"no reply {message_id}, or {verdict!r} is not a mark")
    return JSONResponse({"ok": True})


async def get_next(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, **await offloop.with_own_conn(runtime.conn, next_up)})


async def get_search(request: Request) -> JSONResponse:
    """`?q=` the words: the entries holding every one, by page. `q` comes
    back as it was sent, so the box can drop an answer to older words."""
    q = request.query_params.get("q") or ""
    return JSONResponse(await offloop.with_own_conn(runtime.conn, search, q))


ROUTES = [
    Route("/control/day", get_day),
    Route("/control/days", get_days),
    Route("/control/looked", post_looked, methods=["POST"]),
    Route("/control/react", post_react, methods=["POST"]),
    Route("/control/next", get_next),
    Route("/control/search", get_search),
]
