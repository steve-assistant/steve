"""Scoring a finished conversation, and keeping what is worth keeping.

Runs after a session ends, on the cheap tier at the flex service level --
latency is irrelevant here and the bill is not, so this is the one path that
should never be in a hurry.

The ordering constraint that shapes the whole module: **nothing reaches the
judge until it has been redacted.** That is enforced by type rather than by
discipline -- `judge()` accepts a `Redacted`, not a `str`, so a caller cannot
pass a raw transcript by forgetting a step. A comment saying "remember to
redact" would have been the alternative, and comments do not fail the build.

What comes out is three things, in descending order of how often they happen:
a row of scores, sometimes a durable lesson written to the memories repo, and
rarely a proposal filed in the failure ledger (agent/issues.py), where the
night chooses its work from.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel, Field

from agent import jobs, offloop
from agent.models import Role
from agent.redact import Redacted, redact
from core import db
from core.rubric import DIMENSIONS, KEYS, POOR, RUBRIC_VERSION, as_prompt

log = logging.getLogger("reflect")

# Enough of a conversation to judge it. Beyond this the head is truncated: a
# rubric score does not get better for having read 40k tokens of transcript,
# and this runs on every session.
MAX_TRANSCRIPT_CHARS = 24_000

# How long a conversation has to go quiet before it counts as over. Long
# enough that stepping away from a voice session and coming back does not
# split one conversation into two; short enough that a session reflects the
# same day it happened.
IDLE_MINUTES = 30

# A conversation the user keeps coming back to is graded a part at a time. Signal
# continues one session for as long as the user writes within a day of their last
# message (`answering.recent_session`), so a session ran for days; it was
# graded once, the first time it went quiet, and everything after that was
# in a session already marked graded. In the two weeks to 6 October 2026,
# 118 of their Signal messages sat in 3 sessions and 3 reflections. Now a
# session with messages since it was last graded is graded again once it is
# quiet -- what was said since, after this many earlier lines for context.
CONTEXT_LINES = 4
EARLIER = "(earlier, already graded)"

JUDGE_PROMPT = """You are grading a transcript of an AI assistant talking to its user.

You are not the assistant and you are not continuing the conversation. You are
scoring what already happened, strictly and without flattery. A 5 across the
board should be rare; so should a 1. If the transcript does not show enough to
judge a dimension, score it 3 and say so in the notes.

A transcript may open with lines marked "(earlier, already graded)": the end
of the conversation before this part, there for context. Grade only what
follows them.

Each of the user's messages is stamped with the local time they sent it. The
assistant is told the date, the time and their timezone with every message, so
a date, weekday or time it works out from that stamp is grounded; it needs no
tool.

The most recent tool results are shown inline, compacted, as "[tool: name
returned ...]"; older calls show only the name. Judge `grounded` from those
results first: a claim is grounded if a tool result or the conversation supports
it, however it is worded -- "9am Friday" is supported by a start of
"2026-09-25T09:00:00-04:00". A claim of having done something (added, sent,
scheduled) is grounded only if the matching tool was called.

A transcript may also end with "Evidence notes": a mechanical word-overlap
check of the last answer against the raw tool output. It measures quotation,
not truth, and it misses reworded dates, times and names, so treat its
"unsupported" as a prompt to look, not as a verdict.
"""


class Verdict(BaseModel):
    """What the judge returns. Scores are required; the rest usually is not.

    The notes come first: the fields are written in this order, and a judge
    at effort none that scores before it has said what it saw is guessing.
    """

    notes: str = Field(default="", max_length=1200)
    answered: int = Field(ge=1, le=5)
    grounded: int = Field(ge=1, le=5)
    efficient: int = Field(ge=1, le=5)
    safe: int = Field(ge=1, le=5)
    clear: int = Field(ge=1, le=5)
    lesson: str = Field(default="", max_length=600)
    proposal: str = Field(default="", max_length=800)

    def scores(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in KEYS}

    def weakest(self) -> tuple[str, int]:
        scores = self.scores()
        key = min(scores, key=lambda k: scores[k])
        return key, scores[key]


@dataclass
class Reflection:
    """The outcome of one reflection pass."""

    session_id: int
    scores: dict[str, int]
    notes: str
    lesson_path: str | None = None
    proposal_issue_id: int | None = None
    redacted_entities: int = 0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Building the transcript
# ---------------------------------------------------------------------------


# The most recent tool results are shown to the judge, compacted, up to these
# limits; older ones by name only.
SHOWN_TOOL_RESULTS = 8
# A week's agenda runs to a few thousand characters; cut at 1500, the judge
# saw two events and called the rest of a correct week unsupported.
TOOL_RESULT_CHARS = 4000
SHOWN_TOOL_CHARS = 12000
# Fields that identify rather than inform: an id grounds nothing.
_ID_FIELDS = {"id", "threadId", "thread_id", "etag", "iCalUID", "historyId", "series",
              "draft_id", "message_id", "event_id", "job_id", "action_id", "labelIds"}


def _compact(content: str) -> str:
    """A tool result, as little of it as grounds a claim."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items()
                    if k not in _ID_FIELDS and v not in (None, "", [], {})}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    try:
        text = json.dumps(strip(json.loads(content)), ensure_ascii=False,
                          separators=(",", ":"))
    except (ValueError, TypeError):
        text = " ".join(content.split())
    return text[:TOOL_RESULT_CHARS] + ("…" if len(text) > TOOL_RESULT_CHARS else "")


def _local(created_at: str) -> str:
    """When the user sent a message, on their clock, as the assistant was told it.

    Without it the judge marked correct dates down -- "the transcript does
    not establish the current date", "no time/date tool result supports
    '8:01 PM EDT'": six of 200 grades by 6 October 2026, each a lesson the
    night could learn the wrong way round."""
    from agent.history import clock

    return clock(created_at, "%a %d %b %Y %H:%M %Z") or created_at


def transcript(conn: sqlite3.Connection, session_id: int) -> str:
    """Render a session as text for the judge.

    The most recent tool results are shown, compacted; older ones by name.
    They were once all shown by name only, and `grounded` -- "were claims
    supported by tool results?" -- was scored from a word-overlap check of
    the last answer against raw payloads. That check cannot see that "9am"
    is "09:00:00-04:00", so correct answers read as unsupported, and the
    judge had nothing else to go on (2.75/5 in their sessions, 25 September).
    """
    rows = conn.execute(
        """
        SELECT role, content, tool_name, model, effort, created_at
        FROM messages WHERE session_id = ? ORDER BY id
        """,
        (session_id,),
    ).fetchall()
    # Only what was said since the session was last graded (CONTEXT_LINES).
    lines = []
    if since := _graded_until(conn, session_id):
        lines = [f"{EARLIER} {r['role']}: {' '.join((r['content'] or '').split())[:300]}"
                 for r in rows if r["created_at"] <= since
                 and r["role"] in ("user", "assistant")][-CONTEXT_LINES:]
        rows = [r for r in rows if r["created_at"] > since]

    tool_rows = [i for i, r in enumerate(rows) if r["role"] == "tool" and r["content"]]
    shown = set(tool_rows[-SHOWN_TOOL_RESULTS:])
    budget = SHOWN_TOOL_CHARS
    for i, row in enumerate(rows):
        if row["role"] == "tool":
            name = row["tool_name"] or "unknown"
            if i in shown and budget > 0:
                body = _compact(row["content"])[:budget]
                budget -= len(body)
                lines.append(f"[tool: {name} returned {body}]")
            else:
                lines.append(f"[tool: {name}]")
            continue
        who = row["role"]
        if row["role"] == "user" and row["created_at"]:
            who = f"user [{_local(row['created_at'])}]"
        if row["role"] == "assistant" and row["model"]:
            effort = f"/{row['effort']}" if row["effort"] else ""
            who = f"assistant({row['model']}{effort})"
        lines.append(f"{who}: {row['content']}")

    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        # Keep the end: the outcome of a conversation is where the failures
        # show, and the opening is usually pleasantries.
        text = "[...earlier turns truncated...]\n" + text[-MAX_TRANSCRIPT_CHARS:]
    return text


# A claim short enough to be a greeting is not a factual assertion, and
# grounding "sure thing" against a search result is noise in both directions.
MIN_CLAIM_WORDS = 5

# Only the last few exchanges. Grounding the whole session would mostly
# re-check answers whose sources are no longer in the tool rows anyway.
MAX_CLAIMS = 12

# Lines as well as sentences. The first live run made this obvious: a week's
# agenda came back as a markdown list with no full stops in it, so the whole
# answer was one "claim", matched nothing, and was reported as unsupported --
# a loud false negative on an answer that was entirely grounded.
_CLAIM_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# Leading bullet, number or heading marker. Stripped so the claim is the
# assertion rather than its decoration.
_MARKUP = re.compile(r"^[\s>*#\-–—·+]*(?:\d+[.)]\s*)?|\*\*")


def _claims(answer: str) -> list[str]:
    """The assertions in an answer, one per line or sentence.

    Deduplicated, because a weekly agenda repeats the same three lectures on
    four days and grounding each copy separately spends the whole budget
    re-checking one fact.
    """
    found: list[str] = []
    seen: set[str] = set()
    for piece in _CLAIM_SPLIT.split(answer or ""):
        claim = _MARKUP.sub("", piece).strip()
        key = claim.casefold()
        if len(claim.split()) < MIN_CLAIM_WORDS or key in seen:
            continue
        # A line ending in a colon introduces the claims rather than making
        # one. "This week, Monday through Sunday:" is not something a
        # calendar payload can support, and marking it unsupported is a false
        # negative on an answer that was fine.
        if claim.endswith(":"):
            continue
        seen.add(key)
        found.append(claim)
    return found[:MAX_CLAIMS]


def _sources_were_truncated(conn: sqlite3.Connection, session_id: int) -> bool:
    """Did any tool row hit the storage cap?

    Worth saying out loud to the judge: past the cap, "unsupported" stops
    meaning "invented" and starts meaning "further down a payload nobody
    kept", and those two want opposite responses.
    """
    from agent.loop import MAX_TOOL_RESULT_CHARS

    row = conn.execute(
        "SELECT 1 FROM messages WHERE session_id = ? AND role = 'tool'"
        " AND length(content) >= ? LIMIT 1",
        (session_id, MAX_TOOL_RESULT_CHARS),
    ).fetchone()
    return row is not None


def grounding(conn: sqlite3.Connection, session_id: int) -> object | None:
    """Check the last answer's claims against what the tools actually returned.

    This is the half of `grounded` the judge could never see. The rubric asks
    whether factual claims were "supported by tool results", the transcript
    handed to the judge carries no tool results at all, and `grounded` has
    accordingly been the lowest-scoring dimension by a distance -- a question
    asked of an input that could not answer it.

    Deterministic and lexical on purpose. A model asked to grade its own
    citations is the self-grading problem in miniature; a token-overlap match
    against the quoted span is something a human can check. It cannot judge
    truth, only whether the supplied sources say it, which is exactly what
    the rubric's wording asks.

    Returns None when there is nothing to check -- a conversation with no tool
    calls is not ungrounded, it is a conversation.
    """
    from tools.evidence import Source, ground_claims

    rows = conn.execute(
        "SELECT role, content, tool_name FROM messages"
        " WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()

    sources = [
        Source(url=f"tool:{r['tool_name'] or 'unknown'}", text=r["content"])
        for r in rows
        if r["role"] == "tool" and r["content"]
    ]
    if not sources:
        return None

    answers = [r["content"] for r in rows if r["role"] == "assistant"]
    if not answers:
        return None

    claims = _claims(answers[-1])
    if not claims:
        return None

    return ground_claims(claims, sources)


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------


async def judge(clean: Redacted) -> tuple[Verdict, object]:
    """Score a *redacted* transcript.

    Takes a `Redacted` rather than a `str` on purpose: it makes sending an
    unredacted transcript a type error instead of an oversight. Every caller
    has to go through `redact()` to get one.
    """
    from agent.llm import ask_once

    if not isinstance(clean, Redacted):
        raise TypeError("judge() takes a Redacted transcript, not raw text")

    return await ask_once(Role.BULK, JUDGE_PROMPT + "\n" + as_prompt(), clean.text,
                          output_type=Verdict, retries=2)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


async def reflect(conn: sqlite3.Connection, session_id: int) -> Reflection | None:
    """Score one finished session -- or what was said in it since it was
    last graded -- and keep what is worth keeping."""
    # Read first, so a message the user sends while the judge is at work is in the
    # next part, not marked graded unread.
    through = _last_said(conn, session_id)
    if not _worth_judging(conn, session_id):
        _mark_reflected(conn, session_id, through)
        return None

    raw = transcript(conn, session_id)

    # Appended before redaction, not after: the notes quote the tool output
    # verbatim, so they carry exactly the personal detail redaction exists to
    # strip. Running both through one pass makes that impossible to get wrong.
    report = grounding(conn, session_id)
    if report is not None:
        notes = report.render()
        if _sources_were_truncated(conn, session_id):
            notes += (
                "\n\n(Some tool output was too long to keep in full, so "
                "'unsupported' here can also mean 'further down a payload "
                "that was trimmed'.)"
            )
        raw = f"{raw}\n\n{notes}"

    # Off the loop: the spaCy pipeline is CPU-bound and takes seconds.
    clean = await asyncio.to_thread(redact, raw)
    verdict, record = await judge(clean)

    reflection = Reflection(
        session_id=session_id,
        scores=verdict.scores(),
        notes=verdict.notes,
        redacted_entities=clean.count,
        cost_usd=getattr(record, "cost_usd", 0.0) or 0.0,
    )

    _store(conn, reflection, verdict)

    if verdict.lesson.strip() and _theirs(conn, session_id):
        # Off the loop, as the remember tool is: four git commands and an
        # embedding, the first of which loads a model.
        reflection.lesson_path = await offloop.with_own_conn(conn, _keep_lesson, verdict)
    if verdict.proposal.strip():
        reflection.proposal_issue_id = _file_proposal(conn, session_id, verdict)

    _mark_reflected(conn, session_id, through)
    return reflection


def _worth_judging(conn: sqlite3.Connection, session_id: int) -> bool:
    """Is there anything here to grade?

    Measured by structure rather than by length. The first version of this
    skipped transcripts under 40 characters, which quietly threw away every
    short real exchange -- "what's on my calendar today?" and its answer come
    to 38 -- while a rambling aborted turn sailed through. What actually makes
    a session ungradeable is the agent never having replied: every dimension
    of the rubric asks about a reply.

    Only what was said since it was last graded counts, and it has to hold
    something the user said: a run of the agent's own pings and briefings into their
    thread is not a conversation to grade.
    """
    since = _graded_until(conn, session_id)
    row = conn.execute(
        """
        SELECT COUNT(*) AS turns,
               SUM(role = 'assistant') AS replies,
               SUM(role = 'user') AS asked
        FROM messages WHERE session_id = ? AND (? IS NULL OR created_at > ?)
        """,
        (session_id, since, since),
    ).fetchone()
    return (bool(row) and (row["replies"] or 0) > 0 and (row["asked"] or 0) > 0
            and (row["turns"] or 0) >= 2)


def _store(conn: sqlite3.Connection, reflection: Reflection, verdict: Verdict) -> None:
    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO reflections (session_id, rubric, scores_json, notes, redacted)
            VALUES (?, ?, ?, ?, 1)
            """,
            (
                reflection.session_id,
                RUBRIC_VERSION,
                json.dumps(reflection.scores),
                verdict.notes or None,
            ),
        )


def _graded_until(conn: sqlite3.Connection, session_id: int) -> str | None:
    """The moment the session was last graded through, or None if never."""
    row = conn.execute("SELECT reflected_at FROM sessions WHERE id = ?",
                       (session_id,)).fetchone()
    return row[0] if row else None


def _last_said(conn: sqlite3.Connection, session_id: int) -> str | None:
    row = conn.execute("SELECT MAX(created_at) FROM messages WHERE session_id = ?",
                       (session_id,)).fetchone()
    return row[0] if row else None


def _mark_reflected(conn: sqlite3.Connection, session_id: int,
                    through: str | None = None) -> None:
    """Graded through `through`, the last message the judge saw: the next
    part starts after it (CONTEXT_LINES)."""
    with db.transaction(conn):
        conn.execute(
            "UPDATE sessions SET reflected_at = COALESCE(?, datetime('now')) WHERE id = ?",
            (through, session_id),
        )


def _theirs(conn: sqlite3.Connection, session_id: int) -> bool:
    """Whether the user was in this session, so a lesson from it is about
    working with them.

    Lessons go into the memory every `recall` searches, as guidance on how
    to work. On 6 October 2026 it held 112 of them, and 83% of the sessions
    reflected on in the fortnight before were the agent's own -- the night's
    research, the doer, background jobs -- so a recall about them came back
    crowded with "when asked to research and record findings, cite only..."
    from sessions the user never saw. Those sessions are still scored; they only
    no longer write lessons.
    """
    from core.call_policy import is_autonomous

    row = conn.execute("SELECT channel FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row is not None and not is_autonomous(row[0])


def _keep_lesson(conn: sqlite3.Connection, verdict: Verdict) -> str | None:
    """Write a durable lesson to the memories repo.

    The lesson is already redacted -- it came out of a model that only ever
    saw placeholders -- so nothing personal can reach the repo through here.
    """
    from tools import memory

    key, score = verdict.weakest()
    lesson = verdict.lesson.strip()
    # "feedback" in the memory taxonomy is guidance on how to work, with the
    # reason attached -- which is exactly what a lesson is. Reusing the kind
    # means recall surfaces these alongside guidance the user gave directly,
    # rather than in a category nothing ever searches.
    body = f"{lesson}\n\n**Why:** reflection scored {key} at {score}/5."
    try:
        stored = memory.remember(
            conn,
            kind="feedback",
            summary=f"{key}: {lesson[:80]}",
            body=body,
        )
    except Exception:  # noqa: BLE001 - a lesson is worth less than the pass
        log.exception("could not store lesson")
        return None
    return getattr(stored, "path", None)


def _file_proposal(
    conn: sqlite3.Connection, session_id: int, verdict: Verdict
) -> int | None:
    """File a proposed code change in the failure ledger, for the night.

    Filed, never applied: this is a model that just graded a transcript, and
    the distance from "noticed a weakness" to "may edit the codebase" is the
    whole of phase 6. It was queued as an `upgrade` job until 30 September
    2026, but the job worker claims every queued job at once, in the day, so
    the night's "reflection's proposals first" almost never saw one. In the
    ledger it counts up with everything else, one issue per rubric
    dimension -- "grounded marked poor four times" -- and the night weighs
    it against the rest.

    The session is context in the ledger, not a requester: the night's
    attempts are always the agent's own, never "the user asked for this".
    """
    from agent import issues

    key, score = verdict.weakest()
    if score > POOR:
        # Only a genuinely poor dimension earns a code change. Otherwise every
        # session files a proposal and the ledger becomes noise.
        return None
    proposal = verdict.proposal.strip()
    return issues.record(
        conn, "reflection", key,
        f"Reflection scored {key} {score}/5 on session {session_id}: {proposal}",
        excerpt=f"{proposal}\n\n{verdict.notes or ''}".strip(), session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Finding work
# ---------------------------------------------------------------------------


def unreflected(conn: sqlite3.Connection, limit: int = 10) -> list[int]:
    """Sessions that have ended and not yet been scored, and sessions the user
    came back to that have gone quiet again since they were last scored
    (CONTEXT_LINES)."""
    rows = conn.execute(
        """
        SELECT s.id FROM sessions s
        WHERE s.ended_at IS NOT NULL
          AND (s.reflected_at IS NULL
               OR (EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id
                           AND m.created_at > s.reflected_at)
                   -- quiet again: `close_idle` saw to that the first time
                   AND NOT EXISTS (SELECT 1 FROM messages q WHERE q.session_id = s.id
                                   AND q.created_at >= datetime('now', ?))))
        ORDER BY COALESCE(s.reflected_at, s.ended_at) LIMIT ?
        """,
        (f"-{int(IDLE_MINUTES)} minutes", limit),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def close_idle(conn: sqlite3.Connection, idle_minutes: int = IDLE_MINUTES) -> list[int]:
    """End sessions that have gone quiet.

    Nothing else ever sets `ended_at`. A voice session has no hang-up, a
    Signal thread has no close button, and the tray app can sit open for days
    -- so there is no event that means "the conversation is over" and the only
    honest signal is silence. Without this, reflection has nothing to pick up
    and `unreflected()` is permanently empty.
    """
    rows = conn.execute(
        """
        SELECT s.id FROM sessions s
        WHERE s.ended_at IS NULL
          AND COALESCE(
                (SELECT MAX(created_at) FROM messages m WHERE m.session_id = s.id),
                s.started_at
              ) < datetime('now', ?)
        """,
        (f"-{int(idle_minutes)} minutes",),
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    if ids:
        with db.transaction(conn):
            conn.executemany(
                "UPDATE sessions SET ended_at = datetime('now') WHERE id = ?",
                [(i,) for i in ids],
            )
    return ids


def sweep(conn: sqlite3.Connection, limit: int = 10) -> list[int]:
    """Close what has gone quiet and queue the reflecting.

    Queued as jobs rather than run inline so reflection competes for the same
    worker as everything else: it is the lowest-priority thing the agent does,
    and it should never be the reason a real request waits.
    """
    close_idle(conn)
    queued = []
    for session_id in unreflected(conn, limit=limit):
        if _already_queued(conn, session_id):
            continue
        queued.append(
            jobs.enqueue(
                conn,
                kind="reflect",
                prompt=str(session_id),
                why="session went quiet",
                session_id=session_id,
                priority=-2,  # below even an upgrade proposal
            )
        )
    return queued


def _already_queued(conn: sqlite3.Connection, session_id: int) -> bool:
    """A sweep every few minutes would otherwise file the same session again
    each time, until the queue is nothing but duplicate reflections."""
    row = conn.execute(
        """
        SELECT 1 FROM jobs
        WHERE kind = 'reflect' AND session_id = ?
          AND status IN ('queued', 'running')
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    return row is not None


def averages(conn: sqlite3.Connection, limit: int = 50) -> dict[str, float]:
    """Recent scores per dimension. The point of keeping them at all."""
    rows = conn.execute(
        "SELECT scores_json FROM reflections ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        return {}
    totals: dict[str, list[int]] = {d.key: [] for d in DIMENSIONS}
    for row in rows:
        for key, value in json.loads(row["scores_json"]).items():
            if key in totals:
                totals[key].append(int(value))
    return {k: round(sum(v) / len(v), 2) for k, v in totals.items() if v}
