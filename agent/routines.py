"""Routines: what the user asks to have done on a schedule, or watched for.

Every leading agent has these by October 2026 -- OpenClaw's heartbeat and
cron jobs, Claude Cowork's scheduled tasks, Gemini's scheduled actions -- and
this agent had only one-off reminders and checks that end within a day
(`agent/reminders.py`, MAX_SPAN). So, the maintainer, 4 October 2026: "every Sunday at
7pm, recap my week", "every weekday at 8:30, what's due", "check every few
hours until the tickets drop".

Two shapes:

- **at** -- at a time, on every day or chosen days. Delivered when it runs,
  like a reminder: the user chose the hour.
- **every** -- every N minutes (30 at least), until a date, or until a
  condition the user named is met (a watch). These keep quiet hours unless the user made
  one urgent: a report at 3am that could have waited is held for the morning.

A run is a background job of its own on the `routine` channel
(`agent/worker/handlers.handle_routine`), so it is unattended: it reads only
URLs it was shown or that are in its instruction (`core/egress.unseen`), and
may do only what a worker may -- private and reversible -- plus what its
envelope allows. The envelope is what the user approved it to do outward without
asking each time, with its targets fixed ("email mom@..."): approved once on
an `approve_routine` card, and checked on every call by
`core/monitor.routine_gate`. Anything outside it gets a card.

It reports what changed, like a keep-checking check: the run says NOTHING_NEW
when nothing did, and a watch says CONDITION_MET when its condition holds,
which ends it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core import db, unseen

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS = DAYS[:5]
MAX_ACTIVE = 20
MIN_EVERY_MINUTES = 30
# How far ahead a routine may end, and how long a watch may run unattended.
MAX_AHEAD = timedelta(days=366)
# What all routines together may spend in a day is core's: background
# work's `routines` part (core/spend.py), held back at the job's start.

NOTHING_NEW = "NOTHING_NEW"
MET = "CONDITION_MET"


class BadRoutine(ValueError):
    """A routine that cannot be set up as asked, and why."""


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    kind: str  # "at" | "every"
    time: str | None = None  # "HH:MM", for "at"
    days: tuple[str, ...] = ()  # for "at"; empty is every day
    minutes: int | None = None  # for "every"
    until: str | None = None  # local ISO; when it stops
    condition: str | None = None  # for a watch: what ends it

    @classmethod
    def build(cls, *, at_time: str = "", days=(), every_minutes: int = 0, until: str = "",
              condition: str = "", now: datetime | None = None) -> Schedule:
        """From what the model passes, checked; BadRoutine if it will not do."""
        now = now or datetime.now()
        days = tuple(d.strip().lower()[:3] for d in (days or ()) if d and d.strip())
        if any(d not in DAYS for d in days):
            raise BadRoutine(f"days must be among {', '.join(DAYS)}")
        ends = None
        if until:
            try:
                ends = datetime.fromisoformat(until.strip())
            except ValueError as exc:
                raise BadRoutine(f"until must be an ISO local datetime, got {until!r}") from exc
            ends = ends.astimezone().replace(tzinfo=None) if ends.tzinfo else ends
            if ends <= now:
                raise BadRoutine("until is already past")
            if ends - now > MAX_AHEAD:
                raise BadRoutine("a routine may be set at most a year ahead")
        if at_time and every_minutes:
            raise BadRoutine("either a time of day or an interval, not both")
        if at_time:
            try:
                hour, minute = (int(x) for x in at_time.strip().split(":")[:2])
                if not (0 <= hour < 24 and 0 <= minute < 60):
                    raise ValueError
            except ValueError as exc:
                raise BadRoutine(f"at_time must be HH:MM, got {at_time!r}") from exc
            if condition:
                raise BadRoutine("a condition belongs to an interval watch")
            return cls("at", f"{hour:02d}:{minute:02d}", days,
                       until=ends.isoformat(timespec="minutes") if ends else None)
        if every_minutes:
            if int(every_minutes) < MIN_EVERY_MINUTES:
                raise BadRoutine(f"at most every {MIN_EVERY_MINUTES} minutes")
            if not (ends or condition):
                raise BadRoutine("an interval routine needs an end: a date, or a condition")
            if days:
                raise BadRoutine("days belong to a time of day")
            return cls("every", minutes=int(every_minutes),
                       until=(ends or now + timedelta(days=30)).isoformat(timespec="minutes"),
                       condition=condition.strip() or None)
        raise BadRoutine("give a time of day (at_time) or an interval (every_minutes)")

    def next_after(self, moment: datetime) -> datetime | None:
        """The next run strictly after `moment`, or None past its end."""
        end = datetime.fromisoformat(self.until) if self.until else None
        if self.kind == "every":
            nxt = moment + timedelta(minutes=self.minutes or MIN_EVERY_MINUTES)
        else:
            hour, minute = (int(x) for x in (self.time or "00:00").split(":"))
            nxt = None
            for ahead in range(8):
                day = (moment + timedelta(days=ahead)).replace(hour=hour, minute=minute,
                                                               second=0, microsecond=0)
                if day > moment and (not self.days or DAYS[day.weekday()] in self.days):
                    nxt = day
                    break
        if nxt is None or (end is not None and nxt > end):
            return None
        return nxt

    def says(self) -> str:
        """In words, for them and the card."""
        if self.kind == "at":
            if not self.days:
                when = "every day"
            elif set(self.days) == set(WEEKDAYS) and len(self.days) == 5:
                when = "weekdays"
            else:
                when = ", ".join(d.capitalize() for d in self.days)
            return f"{when} at {self.time}" + (f" until {self.until}" if self.until else "")
        until = f"until {self.condition}" if self.condition else f"until {self.until}"
        if self.condition and self.until:
            until += f" (or {self.until})"
        hours = (self.minutes or 0) / 60
        every = f"every {hours:g} hours" if hours >= 1 else f"every {self.minutes} minutes"
        return f"{every} {until}"

    def to_json(self) -> str:
        return json.dumps({"kind": self.kind, "time": self.time, "days": list(self.days),
                           "minutes": self.minutes, "until": self.until,
                           "condition": self.condition})

    @classmethod
    def from_json(cls, text: str) -> Schedule:
        raw = json.loads(text)
        return cls(raw["kind"], raw.get("time"), tuple(raw.get("days") or ()),
                   raw.get("minutes"), raw.get("until"), raw.get("condition"))


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass
class Routine:
    id: int
    title: str
    instruction: str
    schedule: Schedule
    envelope: dict = field(default_factory=dict)
    urgent: bool = False
    status: str = "active"
    session_id: int | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_report: str | None = None
    held: str | None = None
    runs: int = 0

    @classmethod
    def from_row(cls, row) -> Routine:
        return cls(id=row["id"], title=row["title"], instruction=row["instruction"],
                   schedule=Schedule.from_json(row["schedule"]),
                   envelope=json.loads(row["envelope"]) if row["envelope"] else {},
                   urgent=bool(row["urgent"]), status=row["status"],
                   session_id=row["session_id"], next_run_at=row["next_run_at"],
                   last_run_at=row["last_run_at"], last_report=row["last_report"],
                   held=row["held"], runs=row["runs"])

    @property
    def is_watch(self) -> bool:
        return self.schedule.kind == "every"

    def as_dict(self) -> dict:
        from core import monitor

        return {"id": self.id, "title": self.title, "status": self.status,
                "when": self.schedule.says(), "next_run": self.next_run_at,
                "runs": self.runs, "urgent": self.urgent,
                "may_without_asking": monitor.describe_envelope(self.envelope)
                if self.envelope else "nothing outward",
                "instruction": self.instruction}


def create(conn: sqlite3.Connection, *, title: str, instruction: str, schedule: Schedule,
           envelope: dict | None = None, urgent: bool = False, session_id: int | None = None,
           now: datetime | None = None) -> Routine:
    """Store a routine. One with an envelope waits for its card to be approved."""
    from core import monitor

    now = now or datetime.now()
    title, instruction = (title or "").strip(), (instruction or "").strip()
    if not title or not instruction:
        raise BadRoutine("a routine needs a title and an instruction")
    if why := unseen.refusal(title=title, instruction=instruction, condition=schedule.condition):
        raise BadRoutine(why)
    if envelope and (why := monitor.envelope_refusal(conn, envelope)) is not None:
        raise BadRoutine(why)
    count = conn.execute("SELECT COUNT(*) FROM routines WHERE status IN"
                         " ('active', 'awaiting_approval', 'paused')").fetchone()[0]
    if count >= MAX_ACTIVE:
        raise BadRoutine(f"there are already {count}; cancel one first")
    first = schedule.next_after(now)
    if first is None:
        raise BadRoutine("that schedule never runs")
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO routines (title, instruction, schedule, envelope, urgent, status,"
            " session_id, next_run_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title[:120], instruction, schedule.to_json(),
             json.dumps(envelope) if envelope else None, int(bool(urgent)),
             "awaiting_approval" if envelope else "active", session_id,
             first.isoformat(timespec="seconds")))
    return get(conn, int(cur.lastrowid))


def get(conn: sqlite3.Connection, routine_id: int) -> Routine | None:
    row = conn.execute("SELECT * FROM routines WHERE id = ?", (int(routine_id),)).fetchone()
    return Routine.from_row(row) if row else None


def listed(conn: sqlite3.Connection) -> list[Routine]:
    rows = conn.execute("SELECT * FROM routines WHERE status IN"
                        " ('active', 'awaiting_approval', 'paused') ORDER BY id").fetchall()
    return [Routine.from_row(r) for r in rows]


def activate(conn: sqlite3.Connection, routine_id: int, envelope_sha: str) -> bool:
    """Their yes to an `approve_routine` card: the routine starts -- if its
    envelope, instruction and schedule are still the ones the card showed
    (`monitor.holder_sha`)."""
    from core import monitor

    routine = get(conn, routine_id)
    if routine is None or routine.status != "awaiting_approval" or not routine.envelope:
        return False
    if monitor.holder_sha(conn, "routine", routine.id) != envelope_sha:
        return False
    with db.transaction(conn):
        conn.execute("UPDATE routines SET status = 'active' WHERE id = ?", (routine.id,))
    return True


def pause(conn: sqlite3.Connection, routine_id: int, *, resume: bool = False,
          now: datetime | None = None) -> bool:
    routine = get(conn, routine_id)
    if routine is None:
        return False
    if resume:
        if routine.status != "paused":
            return False
        nxt = routine.schedule.next_after(now or datetime.now())
        with db.transaction(conn):
            conn.execute("UPDATE routines SET status = ?, next_run_at = ? WHERE id = ?",
                         ("active" if nxt else "ended",
                          nxt.isoformat(timespec="seconds") if nxt else None, routine.id))
        return nxt is not None
    if routine.status != "active":
        return False
    with db.transaction(conn):
        conn.execute("UPDATE routines SET status = 'paused' WHERE id = ?", (routine.id,))
    return True


def pause_all(conn: sqlite3.Connection) -> int:
    with db.transaction(conn):
        return conn.execute("UPDATE routines SET status = 'paused'"
                            " WHERE status = 'active'").rowcount


def cancel(conn: sqlite3.Connection, routine_id: int) -> bool:
    with db.transaction(conn):
        return conn.execute("UPDATE routines SET status = 'cancelled'"
                            " WHERE id = ? AND status IN ('active', 'awaiting_approval',"
                            " 'paused')",
                            (int(routine_id),)).rowcount > 0


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def due(conn: sqlite3.Connection, now: datetime | None = None) -> list[Routine]:
    """What is due to run."""
    now = now or datetime.now()
    rows = conn.execute("SELECT * FROM routines WHERE status = 'active'"
                        " AND next_run_at IS NOT NULL AND next_run_at <= ?",
                        (now.isoformat(timespec="seconds"),)).fetchall()
    return [Routine.from_row(r) for r in rows]


def claim(conn: sqlite3.Connection, routine: Routine, now: datetime | None = None) -> bool:
    """Move a due routine on to its next time, exactly once: the loop
    enqueues its run only if this wins. Past its end, it runs this once more
    and is then ended by `record_run`."""
    now = now or datetime.now()
    nxt = routine.schedule.next_after(now)
    with db.transaction(conn):
        won = conn.execute(
            "UPDATE routines SET next_run_at = ? WHERE id = ? AND status = 'active'"
            " AND next_run_at = ?",
            (nxt.isoformat(timespec="seconds") if nxt else None, routine.id,
             routine.next_run_at)).rowcount
    return won > 0


def prompt(routine: Routine) -> str:
    """What the run is asked, besides its note."""
    return routine.instruction


def mandate(routine: Routine) -> str:
    """What the user asked of a routine, for the action critic (core/critic.py):
    their instruction, and whom they approved it to reach."""
    from core import monitor

    allowed = (" The user approved it to, without asking: "
               f"{monitor.describe_envelope(routine.envelope)}." if routine.envelope else "")
    return f'Routine "{routine.title}", {routine.schedule.says()}: {routine.instruction}{allowed}'


def run_note(routine: Routine) -> str:
    """Appended to the system prompt for a run: what it is, what was said last
    time, and how to say there is nothing new."""
    from core import monitor

    outward = (f" Without asking, it may only: {monitor.describe_envelope(routine.envelope)}."
               " Anything else outward becomes a card for the user." if routine.envelope
               else " It does nothing outward without asking: drafts, notes and private"
               " calendar events only.")
    watch = (f"\n\nThis is a watch. Its condition: {routine.schedule.condition}. If the "
             f"condition now holds, begin your reply with {MET} and say what you found."
             if routine.schedule.condition else "")
    last = (f"\n\nWhat the user was told last time:\n{routine.last_report}"
            if routine.last_report
            else "\n\nThis is its first run.")
    return (f"\n\nThis turn is a run of a routine the user set up: \"{routine.title}\", "
            f"{routine.schedule.says()}. Nobody is watching it run.{outward} Your reply "
            "goes to the user as a message. If nothing worth telling them has changed since "
            f"what they were told last time, reply exactly {NOTHING_NEW} and nothing else."
            f"{watch}{last}")


# Citation markers the model sometimes leaves after a web search, in Unicode's
# private use area ("\ue200cite\ue202turn0search0\ue201"): invisible or
# garbage on their phone. The first live routine run, 4 October 2026, ended in one.
_CITATION = re.compile("[\ue200-\ue2ff][^\ue200-\ue2ff]{0,80}?(?:[\ue200-\ue2ff][^\ue200-\ue2ff]"
                       "{0,80}?)*[\ue201]|[\ue000-\uf8ff]")


def plain(reply: str, *, lines: bool = False) -> str:
    """A reply as their phone should show it: citation markers out, spacing
    collapsed -- within each line only, with `lines`, for a goal's report of
    three times to choose from (agent/goals.py)."""
    text = _CITATION.sub("", reply or "")
    if lines:
        kept = (" ".join(line.split()) for line in text.splitlines())
        return "\n".join(line for line in kept if line).strip()
    return " ".join(text.split()).strip()


def understood(routine: Routine, reply: str) -> tuple[str | None, bool]:
    """What to tell them, if anything, and whether a watch's condition is met."""
    text = plain(reply)
    if not text or text.upper().startswith(NOTHING_NEW):
        return None, False
    met = bool(routine.schedule.condition) and text.upper().startswith(MET)
    if met:
        text = text[len(MET):].lstrip(" :-—\n") or "Its condition is met."
    if routine.last_report and text == routine.last_report.strip():
        return None, met
    return text, met


def record_run(conn: sqlite3.Connection, routine: Routine, report: str | None, *,
               met: bool = False, held: str | None = None) -> bool:
    """After a run: what the user was told, and whether the routine is over -- a
    watch whose condition is met, or one past its end. Returns finished."""
    current = get(conn, routine.id)
    finished = met or (current is not None and current.next_run_at is None)
    with db.transaction(conn):
        conn.execute(
            "UPDATE routines SET last_run_at = ?, runs = runs + 1,"
            " last_report = COALESCE(?, last_report),"
            # A new held report replaces the old; one sent now supersedes it;
            # a run with nothing new leaves it to be sent. Overwriting it with
            # nothing lost a watch's 11pm news to its midnight run.
            " held = CASE WHEN ? IS NOT NULL THEN ? WHEN ? IS NOT NULL THEN NULL"
            " ELSE held END,"
            " status = CASE WHEN ? AND status = 'active' THEN 'ended' ELSE status END"
            " WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), report, held, held, report,
             int(finished), routine.id))
    return finished


def keeps_quiet(routine: Routine, now: datetime | None = None) -> bool:
    """Whether a report from this run waits for the morning: an interval
    watch, not marked urgent, inside quiet hours. A routine at a time the user chose
    is delivered then, like a reminder."""
    from core.call_policy import within_quiet_hours

    return routine.is_watch and not routine.urgent and within_quiet_hours(now)


def held_reports(conn: sqlite3.Connection) -> list[Routine]:
    rows = conn.execute("SELECT * FROM routines WHERE held IS NOT NULL").fetchall()
    return [Routine.from_row(r) for r in rows]


def clear_held(conn: sqlite3.Connection, routine_id: int) -> None:
    with db.transaction(conn):
        conn.execute("UPDATE routines SET held = NULL WHERE id = ?", (routine_id,))


def next_run(conn: sqlite3.Connection) -> datetime | None:
    """The soonest run of any active routine, for the wake alarm."""
    row = conn.execute("SELECT MIN(next_run_at) FROM routines WHERE status = 'active'"
                       " AND next_run_at IS NOT NULL").fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def toolset(routine: Routine) -> set[str]:
    """What a run is offered: what a worker may use -- reading, drafting,
    private calendar events -- and the outward tools its envelope names.
    Never a routine tool: a routine cannot make or widen one."""
    from agent.workers import WORKER_TOOLS

    return set(WORKER_TOOLS) | set(routine.envelope)
