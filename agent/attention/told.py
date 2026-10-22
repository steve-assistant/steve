"""What the user was told, line by line, and what each line came from.

At 00:24 on 9 October the user asked "why do you think I'm in Ridgeview?"
about a ping sent at 23:03, and nothing could answer: the ping was a string
handed to Signal, the observation behind it was one of 4,454, and the
judge's reason had been thrown away (`store.judged` kept the verdict, not
the why). So every line that leaves for their phone -- a ping, a pre-event
brief, a morning or evening briefing -- is written here with its
observation, source, place, the judge's reason code and its sentence
(`record`), and `explain` reads it back.

A turn of theirs reads where each ping in its history came from
(`context._came_from`), and the judge reads their thumbs on what was sent them
unasked, with these lines (`reactions`). `explain` answers the question for
code and for a person at the console.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass

from agent import sqltime
from agent.attention.store import Observation
from core import db


@dataclass(frozen=True)
class Line:
    """One line as the user read it, and where it came from. `obs_id` is None for
    a line built from something other than an observation: an agenda entry,
    a forecast, a commitment due."""

    line: str
    obs_id: int | None = None
    source: str | None = None
    place: str | None = None
    reason: str | None = None
    why: str | None = None


def of(obs: Observation, line: str) -> Line:
    """A line said about an observation, with its verdict's provenance."""
    from agent.attention import vet

    return Line(line=line, obs_id=obs.id, source=obs.source,
                place=obs.place or vet.place_of(obs), reason=obs.reason,
                why=obs.why or None)


def record(conn: sqlite3.Connection, message: str, how: str, lines: list[Line], *,
           outbox_id: int | None = None, said_at: str | None = None) -> None:
    """Write what one message said. Its `said_at` (SQLite UTC) is written
    when the user has it -- the outbox row `outbox_id` sent, or seen in the
    app (agent/outbox.py) -- unless given here."""
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO told (message, how, said_at, outbox_id, obs_id, line, source, place,"
            " reason, why) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(message, how, said_at, outbox_id, ln.obs_id, ln.line, ln.source, ln.place,
              ln.reason, ln.why) for ln in lines if ln.line.strip()],
        )


# Their thumbs on what was sent them unasked, as the judge reads them: how far
# back, and how many. A thumb carries the text the user reacted to (its first 600
# characters, `answering.note_feedback`), not which message; the message is found by
# the opening of its lines, which `deliver.compose` sends word for word.
REACTION_DAYS = 30
REACTIONS_MAX = 8
OPENING_CHARS = 60


@dataclass(frozen=True)
class Reaction:
    """Their thumb on one ping or pre-event brief, and what that message said."""

    verdict: str  # down | up
    how: str  # ping | before
    said_at: str  # SQLite UTC
    lines: tuple[Line, ...]


def reactions(conn: sqlite3.Connection, *, days: int = REACTION_DAYS,
              limit: int = REACTIONS_MAX) -> list[Reaction]:
    """Their thumbs lately on the pings and pre-event briefs, newest first, each
    with the lines of the message and where they came from.

    The user gave 18 thumbs-down from 29 September to 9 October, one of them to the
    hurricane warning for Ridgeview at 23:03 on 8 October, and the judge
    that decides what to send them read none: they went to the failure
    ledger only. A briefing is left out -- a thumb on one does not say which
    of its lines it means -- and a thumb on a reply in conversation matches
    no line here."""
    thumbs = conn.execute(
        "SELECT kind, excerpt, last_seen FROM issues"
        " WHERE kind IN ('feedback_down', 'feedback_up') AND last_seen >= datetime('now', ?)"
        " ORDER BY last_seen DESC, id DESC", (f"-{days} days",)).fetchall()
    out: list[Reaction] = []
    for thumb in thumbs:
        found = conn.execute(
            "SELECT * FROM told WHERE how IN ('ping', 'before') AND said_at IS NOT NULL"
            " AND said_at <= ? AND instr(?, substr(line, 1, ?)) > 0"
            " ORDER BY said_at DESC, id",
            (thumb["last_seen"], thumb["excerpt"] or "", OPENING_CHARS)).fetchall()
        if not found:
            continue
        message = found[0]["message"]
        out.append(Reaction(
            verdict="down" if thumb["kind"] == "feedback_down" else "up",
            how=found[0]["how"], said_at=found[0]["said_at"],
            lines=tuple(Line(line=r["line"], obs_id=r["obs_id"], source=r["source"],
                             place=r["place"], reason=r["reason"], why=r["why"])
                        for r in found if r["message"] == message)))
        if len(out) >= limit:
            break
    return out


# Words too common to say which line is meant.
_COMMON = frozenset([
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "from", "with", "you",
    "your", "me", "my", "did", "why", "tell", "told", "say", "said", "that", "this", "about",
    "what",
])


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'-]+", (text or "").lower())
            if w not in _COMMON]


def explain(conn: sqlite3.Connection, about: str | int, *, days: int = 14,
            limit: int = 5) -> list[str]:
    """Why the user was told something: "why did you tell me X?".

    `about` is an observation id, or words from the line ("Ridgeview",
    "Trey Research"); every word must appear in the line or the observation's
    title. Most recent first, at most `limit`, each a short paragraph: when
    and how it was said, the source and the observation, the place, the
    judge's reason and sentence, and what the checks in code made of it."""
    query = (
        "SELECT t.*, o.key AS key, o.title AS title, o.first_seen AS first_seen,"
        " v.judge_say AS judge_say, v.judge_critical AS judge_critical,"
        " v.vetted AS vetted, v.vet_why AS vet_why, v.sender AS sender,"
        " v.sender_why AS sender_why"
        " FROM told t LEFT JOIN observations o ON o.id = t.obs_id"
        " LEFT JOIN verdicts v ON v.obs_id = t.obs_id"
        " WHERE t.written_at >= datetime('now', ?)")
    args: list = [f"-{days} days"]
    if isinstance(about, int) or str(about).strip().isdigit():
        query += " AND t.obs_id = ?"
        args.append(int(about))
    else:
        for word in _words(str(about)):
            query += " AND (lower(t.line) LIKE ? OR lower(COALESCE(o.title, '')) LIKE ?)"
            args += [f"%{word}%", f"%{word}%"]
    query += " ORDER BY COALESCE(t.said_at, t.written_at) DESC, t.id DESC LIMIT ?"
    args.append(limit)
    return [_paragraph(row) for row in conn.execute(query, args).fetchall()]


def _when(stamp: str | None) -> str:
    if not stamp:
        return "not yet sent"
    moment = sqltime.moment(stamp).astimezone()
    return f"{moment:%a %d %b %H:%M}"


def _paragraph(row: sqlite3.Row) -> str:
    how = {"ping": "a ping", "before": "a pre-event brief", "morning": "the morning briefing",
           "evening": "the evening briefing"}.get(row["how"], row["how"])
    parts = [f"{_when(row['said_at'])}, in {how}: \"{row['line'].strip()[:160]}\"."]
    if row["obs_id"] is not None:
        parts.append(f"From {row['source']} (observation {row['obs_id']}, {row['key']}).")
    elif row["source"]:
        parts.append(f"From {row['source']}, not an observation.")
    if row["place"]:
        parts.append(f"About {row['place']}.")
    if row["why"] or row["reason"]:
        parts.append(f"The judge: {row['reason'] or 'no reason code'}"
                     + (f" -- \"{row['why']}\"" if row["why"] else "") + ".")
    if row["sender"]:
        parts.append(f"Sender: {row['sender']} ({row['sender_why']}).")
    if row["vetted"]:
        parts.append(f"The checks: {row['vetted']} -- {row['vet_why']}.")
    return " ".join(parts)


def now_stamp(now: dt.datetime | None = None) -> str:
    """A local naive moment (or now) as SQLite's UTC text, for `said_at`."""
    return sqltime.stamp(now or dt.datetime.now())
