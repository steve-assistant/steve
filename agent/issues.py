"""What went wrong, and what the user thought of it: the failure ledger.

Until 28 September 2026 the agent noticed nothing about itself. A reply
that waited out a 300-second stall, and a "both complete" read as the week's
gym reminders, were each found by a person reading logs and the database by
hand. Reflection graded whole conversations with a rubric; nothing counted
the specific things that break -- a turn that fell over, an answer that
claimed what no tool did, a tool that errored, a reply that took minutes,
the user saying "wait, why did you...", or their thumbs-down on a message.

This is where each is written the moment it happens, by the code that sees
it. One row per kind of thing (`issues`, keyed by a fingerprint, so the
same failure counts up rather than piles up) and one per occurrence
(`issue_events`), so "how often, lately" is a count and not a guess. It is
the raw material the night will choose its fixes from, and what the morning
briefing summarises.

**It never breaks what it watches.** Every write is guarded: a ledger that
could fail a turn would be worse than no ledger.

**It stays on the laptop's database.** Excerpts are short and unredacted,
like the conversations they come from; anything that later sends one to a
model redacts it first.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3

from core import db

log = logging.getLogger("issues")

# What can be recorded. A name outside these is a bug in the caller, and
# refused rather than stored under a typo nobody will ever query.
KINDS = {
    "turn_broke": "a turn fell over",
    "turn_loop": "a turn went round in tool calls until stopped",
    "unbacked_claim": "an answer claimed what no tool did",
    "tool_error": "a tool reported an error",
    "slow_turn": "a reply took over a minute",
    "model_retry": "a model call failed and was retried",
    "correction": "The user corrected it",
    "feedback_down": "The user gave a reply a thumbs-down",
    "feedback_up": "The user gave a reply a thumbs-up",
    "feedback_note": "The user said what was wrong",
    "source_failed": "an attention source failed",
    "worker_crashed": "the worker process died or stopped answering",
    "reflection": "reflection graded a conversation poorly",
    "action_unverified": "an action said it worked, and reading it back said otherwise",
    "mail_unverified": "mail to the agent's mailbox claimed to be from the user and failed "
                       "authentication",
    "rehearsal_failed": "a scenario of the desk's nightly rehearsal failed",
}

# A turn this slow is one the user noticed. Replies usually take a few seconds.
SLOW_TURN_MS = 60_000

EXCERPT_CHARS = 600


def fingerprint(kind: str, key: str) -> str:
    """The same failure, however its numbers and ids vary: "HTTP 502 after
    41s" and "HTTP 502 after 7s" are one issue."""
    cleaned = re.sub(r"\d+", "#", (key or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:120]
    return f"{kind}:{cleaned}"


def record(conn: sqlite3.Connection | None, kind: str, key: str, summary: str, *,
           excerpt: str = "", session_id: int | None = None,
           message_id: int | None = None) -> int | None:
    """Count one occurrence. Returns the issue's id, or None if it could not
    be written -- which is logged and otherwise ignored.

    A fixed issue that happens again is open again: the fix did not hold.
    One the user chose to ignore stays ignored."""
    if conn is None:
        return None
    if kind not in KINDS:
        log.warning("not recording an issue of unknown kind %r", kind)
        return None
    try:
        with db.transaction(conn):
            row = conn.execute(
                """
                INSERT INTO issues (kind, fingerprint, summary, excerpt, session_id, message_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    count = count + 1,
                    status = CASE WHEN status = 'fixed' THEN 'open' ELSE status END,
                    last_seen = datetime('now'),
                    summary = excluded.summary,
                    excerpt = excluded.excerpt,
                    session_id = excluded.session_id,
                    message_id = excluded.message_id
                RETURNING id
                """,
                (kind, fingerprint(kind, key), summary[:300], (excerpt or "")[:EXCERPT_CHARS],
                 session_id, message_id),
            ).fetchone()
            conn.execute("INSERT INTO issue_events (issue_id) VALUES (?)", (row[0],))
        return int(row[0])
    except Exception:  # noqa: BLE001 - the ledger must never be what breaks a turn
        log.warning("could not record a %s issue", kind, exc_info=True)
        return None


def recent(conn: sqlite3.Connection, *, hours: int = 24, limit: int = 20) -> list[dict]:
    """Issues seen in the last `hours`, with how often in that window, the
    most frequent first."""
    rows = conn.execute(
        """
        SELECT i.id, i.kind, i.summary, i.excerpt, i.count, i.first_seen, i.last_seen,
               COUNT(e.id) AS lately
          FROM issues i JOIN issue_events e ON e.issue_id = i.id
         WHERE e.at >= datetime('now', ?)
         GROUP BY i.id
         ORDER BY lately DESC, i.last_seen DESC
         LIMIT ?
        """,
        (f"-{int(hours)} hours", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def briefing_line(conn: sqlite3.Connection, *, hours: int = 24) -> str | None:
    """One line for the morning: what it noticed going wrong in the last
    day, by kind. None on a clean day."""
    counts = conn.execute(
        """
        SELECT i.kind, COUNT(e.id) AS n
          FROM issue_events e JOIN issues i ON i.id = e.issue_id
         WHERE e.at >= datetime('now', ?) AND i.kind != 'feedback_up'
         GROUP BY i.kind ORDER BY n DESC
        """,
        (f"-{int(hours)} hours",),
    ).fetchall()
    if not counts:
        return None
    said = {
        "turn_broke": "{n} broken repl{y}", "turn_loop": "{n} repl{y} stuck in a loop",
        "unbacked_claim": "{n} claim{s} it had to correct", "tool_error": "{n} tool error{s}",
        "slow_turn": "{n} slow repl{y}", "model_retry": "{n} model retr{y}",
        "correction": "{n} correction{s} from you", "feedback_down": "{n} thumbs-down",
        "feedback_note": "{n} note{s} from you on what was wrong",
        "source_failed": "{n} source failure{s}",
        "worker_crashed": "{n} crash{es}",
        "reflection": "{n} poor grade{s} from reflection",
    }
    parts = []
    for row in counts:
        n = row["n"]
        template = said.get(row["kind"], "{n} " + row["kind"])
        parts.append(template.format(n=n, s="" if n == 1 else "s", y="y" if n == 1 else "ies",
                                     es="" if n == 1 else "es"))
    return "noticed about itself: " + ", ".join(parts)


# --------------------------------------------------------------------------
# What the night works on (agent/night/tinkering.py)
# --------------------------------------------------------------------------

# How much one occurrence counts when choosing what to fix. The user's own
# signals count most: they noticed, and said so. A thumbs-up is not work.
WEIGHT = {"feedback_down": 3, "feedback_note": 3, "correction": 3,
          "turn_broke": 2, "worker_crashed": 2, "unbacked_claim": 2}
NOT_WORK = ("feedback_up",)
# An issue tried and not fixed waits this long before it is offered again,
# and after this many tries it is not offered at all: the pipeline cannot
# do it, and a person should look.
RETRY_AFTER_DAYS = 3
MAX_TRIES = 3


def candidates(conn: sqlite3.Connection, *, days: int = 14, limit: int = 8) -> list[dict]:
    """The open issues worth a night's attempt, the most pressing first:
    seen in the last `days`, not tried in the last few, not given up on.
    Pressing is how often lately, weighted by who noticed."""
    not_work = ", ".join(f"'{kind}'" for kind in NOT_WORK)  # constants, not input
    rows = conn.execute(
        f"""
        SELECT i.id, i.kind, i.summary, i.excerpt, i.count, i.first_seen, i.last_seen,
               i.session_id,
               (SELECT COUNT(*) FROM issue_events e
                 WHERE e.issue_id = i.id AND e.at >= datetime('now', :window)) AS lately,
               (SELECT COUNT(*) FROM issue_attempts a WHERE a.issue_id = i.id) AS tries
          FROM issues i
         WHERE i.status = 'open'
           AND i.kind NOT IN ({not_work})
           AND i.last_seen >= datetime('now', :window)
           AND NOT EXISTS (SELECT 1 FROM issue_attempts a WHERE a.issue_id = i.id
                            AND a.at >= datetime('now', :retry))
        """,
        {"window": f"-{int(days)} days", "retry": f"-{RETRY_AFTER_DAYS} days"},
    ).fetchall()
    found = [dict(r) for r in rows if r["tries"] < MAX_TRIES]
    found.sort(key=lambda r: (WEIGHT.get(r["kind"], 1) * max(r["lately"], 1), r["last_seen"]),
               reverse=True)
    return found[:limit]


def attempted(conn: sqlite3.Connection, issue_id: int, build_id: int, status: str) -> None:
    """Note that the night tried to fix it, and what came of it, so it waits
    a few nights before it is offered again. Until step 6 a change that
    merged marked it fixed; a fix is a module now, and an issue stays open
    until the user's card turns the module on."""
    with db.transaction(conn):
        conn.execute("INSERT INTO issue_attempts (issue_id, build_id, status) VALUES (?, ?, ?)",
                     (issue_id, build_id or 0, status))


# Windows' codes for how a process was ended, as the supervisor notes them.
_FAULTS = {
    "0xc0000005": "an access violation",
    "0xc0000374": "heap corruption",
    "0xc00000fd": "a stack overflow",
    "0xc0000409": "a stack buffer overrun",
}
# faulthandler's frames: `File "C:\\...\\lxml\\etree.py", line 12 in parse`.
_FRAME = re.compile(r'File "([^"]+)", line (\d+) in (\S+)')
# A frame in Python's own library or an installed package, not this repo's.
_NOT_OURS = re.compile(r"site-packages|[\\/]Lib[\\/]", re.IGNORECASE)


def _frame(match: re.Match) -> str:
    path, line, func = match.groups()
    return f"{func} ({re.split(r'[\\/]', path)[-1]}:{line})"


def _crash(note: dict) -> tuple[str, str, str]:
    """(what happened, where, the evidence) from one death note.

    Where is the innermost frame of the thread that faulted, which
    faulthandler (enabled in agent/server.py) prints first under "Current
    thread", and the innermost frame of this repo's own code beneath it --
    the call that is ours to change. On 30 September the first was `read
    (ssl.py)`, which says little; the second was `_post (jev.py)`. A crash
    before faulthandler was on, or a worker that stopped answering, has no
    frame: its key is what happened alone.

    The evidence is the whole stack, one short line per frame, since the
    frame that matters sat eighteen deep and the raw text of the first four
    already filled the excerpt."""
    tail = str(note.get("tail") or "")
    if note.get("how") == "unresponsive":
        what = "stopped answering its health checks"
    else:
        what = _FAULTS.get(str(note.get("exit") or "").lower(),
                           f"exit {note.get('exit')}" if note.get("exit") else "an unknown end")
    where, evidence = "", tail[-EXCERPT_CHARS:]
    current = tail.find("Current thread")
    if current >= 0:
        frames = list(_FRAME.finditer(tail[current:]))
        if frames:
            where = _frame(frames[0])
            ours = next((f for f in frames if not _NOT_OURS.search(f.group(1))), None)
            if ours is not None and ours is not frames[0]:
                where += f" from {_frame(ours)}"
            evidence = " < ".join(_frame(f) for f in frames)[:EXCERPT_CHARS]
    return what, where, evidence


def record_deaths(conn: sqlite3.Connection, path=None) -> int:
    """The supervisor's notes of workers that died, into the ledger, each
    once. Run by a worker as it takes up its duties: the supervisor keeps no
    database, and a dead worker cannot write down its own death.

    The note file is moved aside before it is read, so a death noted while
    this runs lands in a fresh file for the next worker, not in the gap."""
    import os

    if path is None:
        from core import supervisor

        path = supervisor.deaths_path()
    taking = path.with_name(path.name + ".taking")
    try:
        os.replace(path, taking)
    except OSError:
        return 0  # no deaths since the last worker took up its duties
    recorded = 0
    try:
        for line in taking.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                note = json.loads(line)
            except ValueError:
                continue
            what, where, evidence = _crash(note)
            summary = (f"The worker ({note.get('worker', '?')}) {what}"
                       if note.get("how") == "unresponsive"
                       else f"The worker ({note.get('worker', '?')}) crashed: {what}")
            if where:
                summary += f", in {where}"
            if record(conn, "worker_crashed", f"{what} {where}", summary,
                      excerpt=f"{note.get('at', '')}\n{evidence}") is not None:
                recorded += 1
    finally:
        with contextlib.suppress(OSError):
            taking.unlink()
    return recorded


# Their message, read as a correction of what the agent just did. A false
# positive is a stray row, but "no" alone is also an answer to a question,
# so it needs company.
#
# Two halves. The openings are only corrections at the start ("wait", "I
# meant"); the phrases are corrections wherever they fall. It used to be the
# openings alone, and with ASCII apostrophes only: on 7 October 2026 it had
# caught 0 of 10 of their real corrections -- "That’s who Alex is, not
# Alexandra Wren", "It’s in the personal inbox", "tomorrow isn't actually
# light i'm heading out camping", "that's literally your linkedin login" --
# and 36 of their 293 messages use a curly apostrophe. One correction was
# ever recorded.
CORRECTION = re.compile(
    r"^\s*(wait\b|hold on\b|why did you\b|that'?s (wrong|not (right|what))|"
    r"you (didn'?t|forgot|misread|misunderstood)\b|not what i\b|i (said|meant|asked)\b|"
    r"no[,.!]+ (i|that|it|you)\b|"
    r"not (my|the|that|this|meeting)\b)",
    re.IGNORECASE,
)
CORRECTION_ANYWHERE = re.compile(
    r"\b(isn'?t|aren'?t|wasn'?t|is|are|was) actually\b|"
    r"\bthat'?s literally\b|"
    r"\bi (just|already) (had|asked|told) you\b|"
    r"\byou (just|already) (did|wrote|made|sent|drafted|added|said)\b|"
    r"\byou shouldn'?t (be|have)\b|"
    r"\bnot my\b|\bwrong\b|\bincorrect\b|\bnot (correct|true)\b|\bnot right\b(?!\s+now)|"
    r"\bdon'?t do that\b|"
    r"\bit'?s in the\b|"
    r"\bthat'?s (who|what|where|which|when)\b[^.?!]*\bnot\b",
    re.IGNORECASE,
)


def straight(text: str) -> str:
    """Their words with curly quotes made straight and in lower case."""
    return (text or "").translate(_STRAIGHT).lower()


# Curly single quotes and the modifier apostrophe phones type, and curly
# double quotes, as their ASCII selves.
_STRAIGHT = str.maketrans("‘’ʼ“”", "'''\"\"")


def is_correction(text: str) -> bool:
    said = straight(text)
    return bool(CORRECTION.search(said) or CORRECTION_ANYWHERE.search(said))
