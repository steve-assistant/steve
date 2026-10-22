"""What the night knows when it chooses its work.

Until 30 September 2026 the night chose its changes from its rubric scores
and the first 80 characters of their last twelve messages -- the night's own
research prompts among them -- and its prompt said an empty list was the
right answer on most nights. On the first two nights of the shadow week that
is what it gave: nothing, so there was nothing for the user to rate. Meanwhile
the failure ledger held a tool that had refused the same call five times,
their thumbs-down on the drafts it wrote them, and their own words that drafts
could not CC anyone.

So the proposal model is now given, in this order: the open issues worth
fixing, each with its evidence (`issues.candidates`); what the user has been
asking for, from their own channels only; the tools and modules it already
has, so it does not build one twice; the module builds tried lately, so it
does not propose one again; and its rubric scores. Since step 6 what it
proposes is a module build (agent/night/tinkering.py), and it runs only
while `self_upgrade.agent_initiated` is on.

**A slow or broken turn comes with what the turn did** (`trace`). Its
excerpt is only what was asked, which says nothing about why. On the night
of 30 September the night read "A reply took 2993s" beside a research
question and built a broader search and two progress messages to their phone.
The turn had spent 2,993 of those seconds in one model call, during 47
minutes of which nothing on the laptop ran at all, after two attempts had
died on connection errors: the laptop asleep or offline, which no change
to search makes faster. Its own prompt says to skip an outage; it could not
see one.

**Everything drawn from conversations is redacted before it leaves the
laptop** (`agent/redact.py`), here and in the evidence handed on to the
coder. Redaction is slow and blocking, so it runs in a thread; the database
reads happen first, on the caller's connection, because a connection never
crosses threads (docs/protocols.md section 3).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from agent import heard, issues, reflect, sqltime
from agent.redact import redact

# What the user asked lately (`heard.user_messages`), newest first.
ASKED = 40
ASKED_DAYS = 14
ASKED_CHARS = 220
TRIED = 12

# The kinds that are a whole turn misbehaving, so the turn's own record says
# more than the excerpt does.
TRACED = ("slow_turn", "turn_broke", "turn_loop", "model_retry")
# A stretch this long inside one model call with nothing else on the laptop
# running is the machine, not the code: replies take seconds, and the
# night's own loops run every few minutes.
QUIET_S = 600
# How far either side of the turn other trouble counts as the same weather.
NEARBY = timedelta(minutes=30)
TRACE_CHARS = 1_000


@dataclass
class Evidence:
    """The night's picture of itself, redacted and ready to send."""

    issues: list[dict] = field(default_factory=list)  # summary and excerpt redacted
    asked: str = ""  # their recent requests, one per line, redacted
    tools: list[str] = field(default_factory=list)  # and modules
    tried: str = ""  # recent builds, one per line, redacted
    scores: dict[str, float] = field(default_factory=dict)

    def by_id(self) -> dict[int, dict]:
        return {int(i["id"]): i for i in self.issues}

    def brief(self) -> str:
        """The text the proposal model reads."""
        lines = ["## Open issues in your failure ledger -- fix what code can fix"]
        if self.issues:
            for i in self.issues:
                lines.append(
                    f"[#{i['id']}] {i['kind']} ({issues.KINDS.get(i['kind'], '')}): "
                    f"{i['lately']} times in the last two weeks, {i['count']} in all, "
                    f"last {i['last_seen']} UTC, tried {i['tries']} times before.\n"
                    f"    {i['summary']}\n"
                    f"    evidence: {' '.join((i['excerpt'] or '(none)').split())[:400]}"
                    + (f"\n    what the turn did: {' '.join(i['trace'].split())}"
                       if i.get("trace") else ""))
        else:
            lines.append("Nothing open: nothing has gone wrong that the ledger saw. "
                         "Tonight is for building.")
        lines += ["", "## What the user has asked you for lately, in their own words "
                  "(names and details redacted)", self.asked or "(nothing recent)"]
        lines += ["", "## Tools and modules you already have (do not build one twice)",
                  ", ".join(self.tools) or "(unknown)"]
        lines += ["", "## Modules built or tried lately (do not propose these again)",
                  self.tried or "(none)"]
        if self.scores:
            weakest = min(self.scores, key=lambda k: self.scores[k])
            lines += ["", "## Your recent rubric scores, out of 5",
                      ", ".join(f"{k} {v}" for k, v in sorted(self.scores.items()))
                      + f" -- weakest: {weakest}"]
        return "\n".join(lines)


async def gather(conn: sqlite3.Connection) -> Evidence:
    """Read everything on the caller's connection, then redact in a thread."""
    found = [{**i, "trace": trace(conn, i)} for i in issues.candidates(conn)]
    asked = heard.user_messages(conn, hours=ASKED_DAYS * 24, latest=ASKED)[::-1]
    tried = conn.execute(
        "SELECT id, state, request FROM module_builds ORDER BY id DESC LIMIT ?", (TRIED,)
    ).fetchall()
    installed = [f"module {r[0]}" for r in conn.execute("SELECT id FROM modules ORDER BY id")]

    asked_text = "\n".join(
        f"- {r['created_at'][:10]} {r['channel']}: "
        f"{' '.join((r['content'] or '').split())[:ASKED_CHARS]}"
        for r in asked)
    tried_text = "\n".join(
        f"- #{r['id']} {r['state']}: {' '.join((r['request'] or '').split())[:140]}"
        for r in tried)

    def scrub() -> tuple[list[dict], str, str]:
        cleaned = [{**i, "summary": redact(i["summary"] or "").text,
                    "excerpt": redact(i["excerpt"] or "").text,
                    "trace": redact(i["trace"]).text if i["trace"] else ""} for i in found]
        return cleaned, redact(asked_text).text, redact(tried_text).text

    cleaned, asked_clean, tried_clean = await asyncio.to_thread(scrub)
    return Evidence(issues=cleaned, asked=asked_clean, tools=[*_tools(), *installed],
                    tried=tried_clean, scores=reflect.averages(conn))


def trace(conn: sqlite3.Connection, issue: dict) -> str:
    """What the turn behind a turn-level issue did, from what the laptop
    recorded of it: its model calls, its tools and what they reported, the
    job it ran for, any stretch of that turn in which nothing on the laptop
    ran, and what else went wrong around it. Empty for other kinds, or when
    the turn is not on record. Unredacted: `gather` redacts it."""
    session = issue.get("session_id")
    if issue.get("kind") not in TRACED or not session:
        return ""
    calls = conn.execute(
        "SELECT id, latency_ms, created_at, job_id FROM usage_events"
        " WHERE session_id = ? ORDER BY id", (session,)).fetchall()
    if not calls:
        return ""
    lines: list[str] = []

    seconds = sorted(((c["latency_ms"] or 0) / 1000 for c in calls), reverse=True)
    slowest = max(calls, key=lambda c: c["latency_ms"] or 0)
    ends = sqltime.moment(slowest["created_at"])
    starts = ends - timedelta(milliseconds=slowest["latency_ms"] or 0)
    rest = ", ".join(f"{s:.0f}" for s in seconds[1:7]) + (" ..." if len(seconds) > 7 else "")
    lines.append(f"{len(calls)} model call{'s' if len(calls) != 1 else ''}; the slowest took "
                 f"{seconds[0]:.0f}s, {sqltime.stamp(starts)} to {slowest['created_at']} UTC"
                 + (f"; the others {rest}s." if rest else "."))

    used: Counter[str] = Counter()
    failed: list[str] = []
    for row in conn.execute("SELECT tool_name, content FROM messages"
                            " WHERE session_id = ? AND role = 'tool'", (session,)):
        used[row["tool_name"] or "?"] += 1
        if why := _tool_error(row["content"]):
            failed.append(f"{row['tool_name']}: {why}")
    if used:
        lines.append("Tools: " + ", ".join(f"{name} x{n}" for name, n in used.most_common())
                     + (f". Failed: {'; '.join(dict.fromkeys(failed))}" if failed else
                        ". None reported an error."))

    job_ids = {c["job_id"] for c in calls if c["job_id"]}
    for job in (conn.execute("SELECT id, kind, status, attempts, max_attempts, error FROM jobs"
                             " WHERE id = ?", (j,)).fetchone() for j in sorted(job_ids)):
        if job is not None:
            lines.append(f"It ran job #{job['id']} ({job['kind']}): {job['status']} after "
                         f"attempt {job['attempts']} of {job['max_attempts']}"
                         + (f"; the last error on it: "
                            f"{' '.join(job['error'].split())[:200].rstrip('.')}"
                            if job["error"] else "") + ".")

    if (quiet := _longest_quiet(conn, starts, ends)) and quiet[1] - quiet[0] >= timedelta(
            seconds=QUIET_S):
        minutes = (quiet[1] - quiet[0]).total_seconds() / 60
        lines.append(f"For {minutes:.0f} minutes of that call ({sqltime.stamp(quiet[0])} to "
                     f"{sqltime.stamp(quiet[1])} UTC) nothing on the laptop called a model or "
                     "wrote a message: most likely it was asleep or offline, which is the "
                     "machine, not this code.")

    first = min(sqltime.moment(c["created_at"]) - timedelta(milliseconds=c["latency_ms"] or 0)
                for c in calls)
    last = max(sqltime.moment(c["created_at"]) for c in calls)
    nearby = conn.execute(
        "SELECT i.kind, i.summary, COUNT(*) AS n FROM issue_events e"
        "  JOIN issues i ON i.id = e.issue_id"
        " WHERE e.at BETWEEN ? AND ? AND i.id != ? AND i.kind != 'feedback_up'"
        " GROUP BY i.id ORDER BY n DESC LIMIT 5",
        (sqltime.stamp(first - NEARBY), sqltime.stamp(last + NEARBY), issue.get("id") or 0),
    ).fetchall()
    if nearby:
        lines.append("Around it on the laptop: " + "; ".join(
            f"{r['kind']} -- {' '.join((r['summary'] or '').split())[:120]} (x{r['n']})"
            for r in nearby) + ".")
    return " ".join(lines)[:TRACE_CHARS]


def _tool_error(content: str | None) -> str:
    """What a tool result reported as its failure, briefly; "" if none. The
    error field only: a tool's other output is outside text (a page, a
    mail), and the night is no place to carry it."""
    try:
        result = json.loads(content or "")
    except (TypeError, ValueError):
        return ""
    if not isinstance(result, dict) or not (result.get("error") or result.get("ok") is False):
        return ""
    return " ".join(str(result.get("error") or "failed").split())[:120]


def _longest_quiet(conn: sqlite3.Connection, starts, ends):
    """The longest stretch between `starts` and `ends` in which nothing on
    the laptop -- any conversation, any job -- called a model or wrote a
    message, as a (from, to) pair. Moments are SQLite's UTC text."""
    since, until = sqltime.stamp(starts), sqltime.stamp(ends)
    marks = [sqltime.moment(r[0]) for r in conn.execute(
        "SELECT created_at FROM usage_events WHERE created_at > ? AND created_at < ?"
        " UNION SELECT created_at FROM messages WHERE created_at > ? AND created_at < ?",
        (since, until, since, until))]
    points = sorted({starts.replace(microsecond=0), *marks, ends})
    gaps = list(zip(points, points[1:], strict=False))
    return max(gaps, key=lambda g: g[1] - g[0]) if gaps else None


def _tools() -> list[str]:
    """Every tool the conversation can call. Each one is classified in
    core/consent.yaml -- an unclassified tool is gated -- so that is the
    list, without building an agent to ask."""
    try:
        from core.consent import load_rules

        rules = load_rules()
        return sorted({*rules.free, *rules.gated})
    except Exception:  # noqa: BLE001 - a hint, not a requirement
        return []
