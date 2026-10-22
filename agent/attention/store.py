"""The `observations` table: what was noticed, judged, done and said.

Every stage of the pipeline reads and writes through here, so the life of
one thing -- an email, an event a day out, a forecast that changed -- is one
row, and the question "have I already dealt with this?" has one answer.

Two rules, both earned by the sweeps this replaces:

**A key is seen once.** `record_new` inserts by key and ignores what is
already there, so a source that returns the same mail every three minutes
adds nothing, and the judge is never asked about it twice. The model is
never asked to remember what it has already handled -- it will get that
right four times and then not.

**Written before any model sees it.** A row exists before the judge runs,
and a task is marked started before the doer runs. A crash between the doer
creating an event and the row saying so would otherwise hand the same mail
to the next tick, which would create the event again.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from agent import sqltime
from core import db, modules

log = logging.getLogger("attention")


def is_mail(source: str) -> bool:
    """Whether a source is a mail account's (`sources.mail`). Each is named
    `mail:<account key>`; the checks that named the three providers
    ("gmail", "outlook" and the password mailbox's) would have missed every account a
    module adds."""
    return source.startswith("mail:")


@dataclass
class Observation:
    """Something a source noticed, before anyone has judged it.

    `when` is the local time of the thing itself -- the event's start, the
    mail's arrival -- not of noticing it. `ref` holds the ids a tool needs to
    act on it (a message id, an event id), passed through to the doer as
    the source gave them -- a long one as a handle (agent/handles.py).
    """

    source: str
    key: str
    title: str
    detail: str = ""
    when: str | None = None
    ref: dict = field(default_factory=dict)
    id: int | None = None
    # Filled in once judged.
    say: str | None = None
    critical: bool = False
    headline: str = ""
    task: str = ""
    outcome: str | None = None
    first_seen: str | None = None  # SQLite UTC
    deliver_at: str | None = None  # SQLite UTC, for a `before`
    triaged_at: str | None = None  # SQLite UTC
    # From `verdicts`, once judged and checked (`vet.py`): the judge's
    # sentence, the reason code after the checks, who sent it, where it is
    # about, and what the checks made of it. None for a row judged before
    # reasons were kept.
    why: str = ""
    reason: str | None = None
    sender: str | None = None
    place: str | None = None
    vetted: str | None = None

    def as_prompt_line(self) -> str:
        """One item for the judge: key first, so the verdict can name it."""
        line = f"[{self.key}] ({self.source}) {self.title}"
        if self.when:
            line += f" -- {self.when}"
        if self.detail:
            line += f"\n    {self.detail[:400]}"
        return line

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Observation:
        keys = row.keys()

        def joined(name: str):
            return row[name] if name in keys else None

        return cls(
            id=row["id"],
            source=row["source"],
            key=row["key"],
            title=row["title"],
            detail=row["detail"] or "",
            when=row["happens_at"],
            ref=json.loads(row["ref"] or "{}"),
            say=row["say"],
            critical=bool(row["critical"]),
            headline=row["headline"] or "",
            task=row["task"] or "",
            outcome=row["outcome"],
            first_seen=row["first_seen"],
            deliver_at=row["deliver_at"],
            triaged_at=row["triaged_at"],
            why=joined("why") or "",
            reason=joined("reason"),
            sender=joined("sender"),
            place=joined("place"),
            vetted=joined("vetted"),
        )


# Every read of observations that something downstream acts on carries its
# verdict: the briefing sorts by the reason, the night asks for it, and the
# record of what the user was told keeps it.
_WITH_VERDICT = (
    "SELECT observations.*, v.why AS why, v.reason AS reason, v.sender AS sender,"
    " v.place AS place, v.vetted AS vetted"
    " FROM observations LEFT JOIN verdicts v ON v.obs_id = observations.id"
)


def _observations(conn: sqlite3.Connection, sql: str, args=()) -> list[Observation]:
    """The rows a query finds, as observations."""
    return [Observation.from_row(r) for r in conn.execute(sql, args).fetchall()]


def get(conn: sqlite3.Connection, ids: list[int]) -> list[Observation]:
    """These observations, with their verdicts, in id order."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return _observations(
        conn, f"{_WITH_VERDICT} WHERE observations.id IN ({marks}) ORDER BY observations.id",
        list(ids))


def known(conn: sqlite3.Connection, keys: list[str]) -> set[str]:
    """Which of these keys are already recorded.

    For sources that pay per item fetched: Gmail lists ids for free and
    charges a call per message's headers, so it asks here first and fetches
    only what is new.
    """
    if not keys:
        return set()
    found: set[str] = set()
    # SQLite caps bound parameters; batches keep a long listing legal.
    for start in range(0, len(keys), 500):
        batch = keys[start : start + 500]
        found.update(
            row["key"]
            for row in conn.execute(
                f"SELECT key FROM observations WHERE key IN ({','.join('?' * len(batch))})",
                batch,
            )
        )
    return found


def record_new(conn: sqlite3.Connection, observations: list[Observation]) -> list[Observation]:
    """Store what has not been seen before, and return exactly those.

    A new look at an event whose guests differ from the ones a pre-meeting
    brief not yet sent was written for sends that brief back to the doer
    (`_restale_briefs`)."""
    fresh: list[Observation] = []
    with db.transaction(conn):
        for obs in observations:
            cur = conn.execute(
                "INSERT OR IGNORE INTO observations"
                " (source, key, title, detail, happens_at, ref)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    obs.source, obs.key, obs.title[:300], obs.detail[:2000],
                    obs.when, json.dumps(obs.ref, default=str),
                ),
            )
            if cur.rowcount:
                obs.id = int(cur.lastrowid)
                fresh.append(obs)
    try:
        _restale_briefs(conn, fresh)
    except Exception:  # noqa: BLE001 - what was just recorded is still returned
        log.exception("could not send a stale pre-meeting brief back")
    return fresh


def _guests(ref: dict) -> set[str]:
    """Who an event lists, by address: "Alex Arden <aarden@alum.example>"
    and the bare address are one guest, so a name filled in later is not a
    new person."""
    return {str(who).rsplit("<", 1)[-1].rstrip(">").strip().lower()
            for who in ref.get("people") or []}


def _restale_briefs(conn: sqlite3.Connection, fresh: list[Observation]) -> int:
    """A brief written before its event's guests were known is written again.

    The brief for the 19 October prep call was prepared on 5 October, when
    the event listed nobody, and said "Alex appears to be Alexandra Wren";
    its twin for 7 October listed the real Alex (aarden@alum.example) by the
    7th, and nothing looked again. When a new look at an event lists
    different people from a brief not yet sent, the brief goes back to the
    doer with the new list, and its old text -- written without them -- is
    dropped rather than sent. It waits for the doer like a task just judged
    (`_PREPARED`), so it is not sent in the meantime with nothing in it.
    Returns how many went back."""
    redone = 0
    for obs in fresh:
        event, guests = obs.ref.get("event_id"), _guests(obs.ref)
        if not event or not guests:
            continue  # a look that lists nobody says nothing new
        rows = conn.execute(
            "SELECT id, ref FROM observations WHERE say = 'before'"
            " AND source IN ('calendar', 'horizon') AND task IS NOT NULL"
            " AND prepared_at IS NOT NULL AND delivered_at IS NULL"
            " AND deliver_at > datetime('now') AND id <> ?"
            " AND json_extract(ref, '$.event_id') = ?", (obs.id, event)).fetchall()
        for row in rows:
            ref = json.loads(row["ref"] or "{}")
            if _guests(ref) == guests:
                continue
            ref["people"] = obs.ref["people"]
            with db.transaction(conn):
                conn.execute(
                    "UPDATE observations SET ref = ?, outcome = NULL, prepared_at = NULL,"
                    " triaged_at = datetime('now') WHERE id = ?",
                    (json.dumps(ref, default=str), row["id"]),
                )
            redone += 1
    return redone


# A pre-meeting brief written more than this long before its moment is
# written again inside it: what was true of an event two weeks out -- who is
# coming, what the user has said to them -- is not what is true the day before.
BRIEF_FRESH_S = 24 * 60 * 60


def refresh_briefs(conn: sqlite3.Connection) -> int:
    """Send stale pre-meeting briefs back to the doer: those with text, due
    within a day, written more than a day before they are due.

    A brief comes back once: written again it is no longer stale. It waits
    for the doer like a task just judged (`_PREPARED`), and until then its
    old text stays; a doer that finds nothing this time replaces the text
    with nothing, because no brief is better than one that may be wrong.
    Returns how many went back."""
    with db.transaction(conn):
        return conn.execute(
            "UPDATE observations SET prepared_at = NULL, triaged_at = datetime('now')"
            " WHERE say = 'before' AND source IN ('calendar', 'horizon')"
            "   AND task IS NOT NULL AND outcome IS NOT NULL"
            "   AND prepared_at IS NOT NULL AND delivered_at IS NULL"
            "   AND deliver_at > datetime('now')"
            f"  AND deliver_at <= datetime('now', '+{BRIEF_FRESH_S} seconds')"
            f"  AND prepared_at < datetime(deliver_at, '-{BRIEF_FRESH_S} seconds')"
        ).rowcount


def untriaged(conn: sqlite3.Connection, limit: int) -> list[Observation]:
    """The queue the judge reads, without what came from an account that is
    off (`core/modules.live`): kept, and judged if it comes back on."""
    return _observations(
        conn, "SELECT * FROM observations WHERE triaged_at IS NULL"
        f" AND {modules.live(conn, 'observations')} ORDER BY id LIMIT ?", (limit,))


def note_attempt(conn: sqlite3.Connection, ids: list[int], *, give_up_after: int) -> None:
    """Count a failed judgement; after enough, stop asking about these."""
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    with db.transaction(conn):
        conn.execute(
            f"UPDATE observations SET triage_attempts = triage_attempts + 1 WHERE id IN ({marks})",
            ids,
        )
        conn.execute(
            "UPDATE observations SET triaged_at = datetime('now'), say = 'never',"
            " headline = '(could not be judged)', delivered_at = datetime('now'),"
            " delivered_in = 'dropped'"
            f" WHERE id IN ({marks}) AND triage_attempts >= ?",
            [*ids, give_up_after],
        )


def judged(
    conn: sqlite3.Connection,
    obs_id: int,
    *,
    say: str,
    critical: bool,
    headline: str,
    task: str,
    deliver_at: str | None,
) -> None:
    """Record the verdict. A `never` is delivered nowhere, and says so."""
    with db.transaction(conn):
        conn.execute(
            "UPDATE observations SET say = ?, critical = ?, headline = ?, task = ?,"
            " deliver_at = ?, triaged_at = datetime('now'),"
            " delivered_at = CASE WHEN ? = 'never' THEN datetime('now') END,"
            " delivered_in = CASE WHEN ? = 'never' THEN 'dropped' END"
            " WHERE id = ?",
            (say, int(critical), headline, task or None, deliver_at, say, say, obs_id),
        )


def record_verdict(
    conn: sqlite3.Connection, obs_id: int, *, why: str, say: str, critical: bool,
    reason: str | None,
) -> None:
    """Keep the judge's sentence and reason beside its verdict, as it gave
    them -- before any check in code changes the verdict -- so what the
    judge thought and what code made of it can both be read back."""
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO verdicts (obs_id, why, judge_say, judge_critical, judge_reason, reason)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(obs_id) DO UPDATE SET why = excluded.why,"
            " judge_say = excluded.judge_say, judge_critical = excluded.judge_critical,"
            " judge_reason = excluded.judge_reason, reason = excluded.reason,"
            " vetted = NULL, vet_why = NULL, vetted_at = NULL",
            (obs_id, why or "", say, int(critical), reason, reason),
        )


def vetted(
    conn: sqlite3.Connection,
    obs_id: int,
    *,
    outcome: str,
    why: str,
    say: str | None = None,
    critical: bool | None = None,
    reason: str | None = None,
    sender: str | None = None,
    sender_why: str | None = None,
    place: str | None = None,
) -> None:
    """Record what the checks in code made of a verdict, and apply it.

    `say` and `critical`, when given, replace the judge's; a `never` is
    dropped as one from the judge is, so nothing downstream -- the doer,
    a ping, a briefing -- ever picks it up. Only an undelivered item is
    changed: what the user was already told stays told."""
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO verdicts (obs_id) VALUES (?) ON CONFLICT(obs_id) DO NOTHING",
            (obs_id,))
        conn.execute(
            "UPDATE verdicts SET vetted = ?, vet_why = ?, vetted_at = datetime('now'),"
            " reason = COALESCE(?, reason), sender = COALESCE(?, sender),"
            " sender_why = COALESCE(?, sender_why), place = COALESCE(?, place)"
            " WHERE obs_id = ?",
            (outcome, why, reason, sender, sender_why, place, obs_id))
        # `critical` first: a `never` marks the row delivered, and an update
        # after that would find nothing undelivered to change.
        if critical is not None:
            conn.execute(
                "UPDATE observations SET critical = ? WHERE id = ? AND delivered_at IS NULL",
                (int(critical), obs_id))
        if say is not None:
            conn.execute(
                "UPDATE observations SET say = ?,"
                " delivered_at = CASE WHEN ? = 'never' THEN datetime('now') END,"
                " delivered_in = CASE WHEN ? = 'never' THEN 'dropped' END"
                " WHERE id = ? AND delivered_at IS NULL",
                (say, say, say, obs_id))


def tasks_waiting(conn: sqlite3.Connection, limit: int) -> list[Observation]:
    """Judged, with something to do, not yet started. Urgent ones first."""
    return _observations(
        conn,
        "SELECT * FROM observations"
        " WHERE triaged_at IS NOT NULL AND task IS NOT NULL AND prepared_at IS NULL"
        # Once the user has been told without it, the moment for the task has gone.
        "   AND say <> 'never' AND delivered_at IS NULL"
        f"  AND {modules.live(conn, 'observations')}"
        " ORDER BY critical DESC, say = 'now' DESC, id LIMIT ?",
        (limit,),
    )


def tasks_started_today(conn: sqlite3.Connection) -> int:
    """Both sides local, as everywhere a day is counted (protocols §7)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM observations WHERE prepared_at IS NOT NULL"
        " AND date(prepared_at, 'localtime') = date('now', 'localtime')"
    ).fetchone()
    return int(row["n"] or 0)


def start_task(conn: sqlite3.Connection, obs_id: int) -> None:
    with db.transaction(conn):
        conn.execute(
            "UPDATE observations SET prepared_at = datetime('now') WHERE id = ?", (obs_id,)
        )


def finish_task(conn: sqlite3.Connection, obs_id: int, outcome: str | None) -> None:
    with db.transaction(conn):
        conn.execute("UPDATE observations SET outcome = ? WHERE id = ?", (outcome, obs_id))


# How long an item waits for its task to be done before it goes out
# without it. A day's task cap or budget can leave a task that never starts,
# and an urgent item must not wait on it for ever.
TASK_WAIT_S = 15 * 60

_PREPARED = (
    "(task IS NULL OR prepared_at IS NOT NULL"
    f" OR triaged_at <= datetime('now', '-{TASK_WAIT_S} seconds'))"
)


def _ready(say: str) -> str:
    """Judged as `say`, undelivered, and not waiting on a doer still to run."""
    return f"say = '{say}' AND delivered_at IS NULL AND {_PREPARED}"


def to_say_now(conn: sqlite3.Connection) -> list[Observation]:
    return _observations(
        conn, f"{_WITH_VERDICT} WHERE {_ready('now')}"
        f" AND {modules.live(conn, 'observations')} ORDER BY critical DESC, id")


# A pre-event brief goes out as a ping within this long of its moment, or
# not as a ping at all: one whose moment fell in quiet hours belongs to the
# morning briefing, and pinging it at 8:00 as well said it twice.
BRIEF_WINDOW_S = 20 * 60


def before_due(conn: sqlite3.Connection) -> list[Observation]:
    """Pre-event briefs whose moment has just come."""
    return _observations(
        conn,
        f"{_WITH_VERDICT} WHERE {_ready('before')}"
        f" AND {modules.live(conn, 'observations')}"
        " AND deliver_at <= datetime('now')"
        f" AND deliver_at > datetime('now', '-{BRIEF_WINDOW_S} seconds')"
        " ORDER BY deliver_at",
    )


def hold_for_morning(conn: sqlite3.Connection, ids: list[int]) -> None:
    """`now` items that the night will not let through become briefing
    material at once, rather than waiting to be pinged at 8:00 -- where
    they raced the morning briefing that also carries them."""
    if not ids:
        return
    with db.transaction(conn):
        conn.executemany(
            "UPDATE observations SET say = 'briefing' WHERE id = ? AND delivered_at IS NULL",
            [(i,) for i in ids],
        )


def for_briefing(
    conn: sqlite3.Connection, limit: int = 20, *, briefs_due_by: str | None = None
) -> list[Observation]:
    """Everything held for the next briefing, most pressing first.

    Includes what was meant for *now* but never went -- held overnight, or
    past the day's cap -- and pre-event briefs due by `briefs_due_by`
    (SQLite UTC; default now): those whose moment came in quiet hours, and,
    for the morning briefing, those for the early meetings, which are
    better read over breakfast than buzzed forty minutes later. Passed in
    rather than read from SQLite's clock so the briefing decides on one
    clock -- its own.
    """
    return _observations(
        conn,
        f"{_WITH_VERDICT} WHERE delivered_at IS NULL AND {_PREPARED}"
        f" AND {modules.live(conn, 'observations')}"
        " AND (say IN ('briefing', 'now')"
        "      OR (say = 'before' AND deliver_at <= COALESCE(?, datetime('now'))))"
        " ORDER BY critical DESC, say = 'now' DESC, id LIMIT ?",
        (briefs_due_by, limit),
    )


def drop_missed(conn: sqlite3.Connection, lead_s: int) -> int:
    """Pre-event briefs for events that have already started.

    A brief is for walking in prepared; after the start it is only a record
    of having missed the moment, so it is dropped rather than sent late.
    `deliver_at` is the start minus the lead, so the start is `deliver_at`
    plus the lead.
    """
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE observations SET delivered_at = datetime('now'), delivered_in = 'dropped'"
            " WHERE say = 'before' AND delivered_at IS NULL"
            "   AND datetime(deliver_at, ?) <= datetime('now')",
            (f"+{int(lead_s)} seconds",),
        )
    return cur.rowcount


def delivered(conn: sqlite3.Connection, ids: list[int], how: str) -> None:
    if not ids:
        return
    with db.transaction(conn):
        conn.executemany(
            "UPDATE observations SET delivered_at = datetime('now'), delivered_in = ?"
            " WHERE id = ? AND delivered_at IS NULL",
            [(how, i) for i in ids],
        )


def demote_stale(conn: sqlite3.Connection, older_than_s: int) -> int:
    """`now` items that never went out become briefing material."""
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE observations SET say = 'briefing'"
            " WHERE say = 'now' AND delivered_at IS NULL AND critical = 0"
            "   AND triaged_at < datetime('now', ?)",
            (f"-{int(older_than_s)} seconds",),
        )
    return cur.rowcount


def pinged(
    conn: sqlite3.Connection, *, items: int, critical: bool, at_night: bool, at: datetime
) -> None:
    """Record one interruption at a local moment, stored as SQLite UTC.

    Given the moment rather than taking SQLite's `now`, so it is written on
    the same clock `pings_since` is asked on -- a caller deciding by a
    pinned clock would otherwise count against a different one.
    """
    stamp = sqltime.stamp(at)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO pings (sent_at, items, critical, at_night) VALUES (?, ?, ?, ?)",
            (stamp, items, int(critical), int(at_night)),
        )


def pings_since(conn: sqlite3.Connection, since: datetime, *, at_night: bool) -> int:
    """Interruptions since a local moment.

    `since` is local and `sent_at` is SQLite's UTC, so the column is
    converted rather than the argument: the comparison is between two local
    times or it is wrong for four hours a day (protocols §7).
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pings WHERE at_night = ?"
        " AND datetime(sent_at, 'localtime') >= ?",
        (int(at_night), since.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return int(row["n"] or 0)


def counts_today(conn: sqlite3.Connection) -> dict:
    """For the status page: what today looked like, in numbers only."""
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS noticed,
            SUM(say = 'never') AS ignored,
            SUM(say = 'now') AS now,
            SUM(say = 'briefing') AS briefing,
            SUM(say = 'before') AS before,
            SUM(outcome IS NOT NULL) AS prepared,
            SUM(triaged_at IS NULL) AS waiting
        FROM observations
        WHERE date(first_seen, 'localtime') = date('now', 'localtime')
        """
    ).fetchone()
    return {k: int(v or 0) for k, v in dict(row).items()}
