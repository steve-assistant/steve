"""The morning briefing and the evening look-ahead.

Two messages a day that the user can count on, at `settings.MORNING` and
`settings.EVENING`, carrying everything the attention loop judged worth
knowing but not worth interrupting for -- plus the day itself: the schedule,
the weather, what is waiting on them, what the night did.

**The facts are code; one line is not.** The schedule, the weather, the
cards and the items are rendered here from the database and the calendar,
exactly -- a time or a card's word rewritten by a model is a missed meeting
or a card that cannot be answered. A multi-day event remains on every date
it covers, so an overnight trip cannot disappear from tomorrow's agenda. What
a model adds is the lead: one or two
lines that connect what the lists cannot ("rain from 3 and your 4pm workshop
is in person -- take the umbrella"). If that call fails or the day's budget
is spent, the briefing goes without it.

**Sent once.** Each briefing is recorded in `sync_cursors` by the date it
was set for when Signal takes it, so a swap at 08:00 does not send two, and
a laptop that wakes at 10:00 still sends the 08:00 morning's -- but not
`LATE_BY` after its time, when a "good morning" would be absurd and its
items are better held for the evening. Never in quiet hours: the panel
refuses a briefing time inside them, and one late enough to reach them
waits for the next day's.

**What it carries is marked told**, and only when delivered. So are the
spending caps' notices (`agent/caps.py`): background work, the night or
extensions stopped at a cap, or near one, said in a line built with no
model call, since the lead's writer may be what the cap stopped. News is capped
at three lines; the rest of the day's headlines are dropped rather than
carried into the next briefing, because yesterday's news is not news.

**A view of what was checked, not a dump of what was held.** It used to
carry every undelivered item, staled by age alone: on 9 October the morning
briefing still carried three Ridgeview hurricane items seven hours after
the user wrote "I'm in Pittsburgh", and Fabrikam's "Thank you for registering" twice.
Now each held item is checked again first (`vet.recheck`), and the order is:
**Needs you** -- items whose reason is one that needs them (`vet.NEEDS_USER`),
each with it -- then at most `MAX_FYI` others; then the night, what is due,
the goals, spending and the news; and last the day and its weather, where
the eye lands after the top. Every line is recorded with where it came from
(`told.record`).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field

from agent import cardwords, sqltime
from agent.attention import settings, store, told, vet
from agent.attention.store import Observation
from agent.cut import cut
from core import call_policy, db, spend

log = logging.getLogger("attention")

# How long after its time a briefing may still go out. It was noon for the
# morning's and midnight for the evening's, clock times that meant nothing
# to anyone whose day is not nine to five.
LATE_BY = dt.timedelta(hours=4)

# An event before this is covered by the morning briefing rather than by a
# brief of its own: forty-five minutes before 9:30 is barely after 8:00.
EARLY_UNTIL = dt.time(9, 30)

MAX_NEWS = 3
# Observations under "FYI": at most this many, the rest wait for the next
# briefing or go stale.
MAX_FYI = 5
# Held longer than this, an item is stale and is dropped, not briefed.
STALE_AFTER = dt.timedelta(days=2)

LEAD_PROMPT = """You write the first line or two of the user's {kind}, the lines they read first.

Below is everything in it. Write ONE or TWO short lines, plain text, that say
the thing that matters most about {day} -- connecting facts where they touch
(rain and an in-person event, a deadline and a free afternoon, a prepared note
and its meeting). Use only what is below and change no time, place or name. No
greeting, no list, no sign-off. If nothing stands out, say in a few words that
it is a light day."""


@dataclass
class Facts:
    kind: str  # morning | evening
    about: dt.date
    agenda: list[str] = field(default_factory=list)
    # Whether the user has a calendar connected. Without one the day is not
    # said at all: "nothing on the calendar" told a user with no calendar
    # that their day was empty.
    calendar: bool = True
    weather: list[str] = field(default_factory=list)
    # The town the user said they live in, which home's forecast line is
    # about; "" with no home.
    home: str = ""
    waiting: list[str] = field(default_factory=list)
    needs_you: list[Observation] = field(default_factory=list)
    # The accounts Steve could not check, a sentence each from their health
    # (core/health.py): a briefing that said nothing of a signed-out
    # mailbox read as a quiet day.
    unchecked: list[str] = field(default_factory=list)
    # What the user owes that is due or asked about, a line each, said under
    # "Needs you" beside the observations. The boundary for the obligations
    # ledger (redesign phase P1), which fills it; empty until then.
    owed: list[str] = field(default_factory=list)
    worth_knowing: list[Observation] = field(default_factory=list)
    news: list[Observation] = field(default_factory=list)
    # Pre-event briefs for early meetings, said as a prep line under the
    # meeting in the agenda rather than as items of their own.
    folded: list[Observation] = field(default_factory=list)
    dropped: list[Observation] = field(default_factory=list)
    due: list[str] = field(default_factory=list)
    night: list[str] = field(default_factory=list)
    # Their goals (agent/goals.py), a line each: how far, and what each waits on.
    goals: list[str] = field(default_factory=list)
    # What happened, in a few lines, instead of the headlines
    # (`newsdesk.for_briefing`). Empty means the headlines go as they are.
    news_summary: str = ""
    # The spending caps reached, or nearly, since the last briefing: the
    # lines, and the notices they say, marked told once it goes.
    spending: list[str] = field(default_factory=list)
    spend_notices: list[spend.Notice] = field(default_factory=list)

    @property
    def carried(self) -> list[Observation]:
        return [*self.needs_you, *self.worth_knowing, *self.news, *self.folded]

    @property
    def title(self) -> str:
        if self.kind == "morning":
            return f"Good morning. {self.about:%A %d %B}."
        return f"Tomorrow, {self.about:%A %d %B}:"

    def without(self, ids: set[int]) -> None:
        """Leave out items another message already carries."""
        if not ids:
            return
        for name in ("needs_you", "worth_knowing", "news", "folded", "dropped"):
            setattr(self, name, [o for o in getattr(self, name) if o.id not in ids])

    def has_substance(self) -> bool:
        """Whether an evening look-ahead is worth sending at all."""
        return bool(
            self.agenda or self.waiting or self.carried or self.due
            or self.owed or self.spending
        )


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


def _clock(start: str | None) -> str:
    if not start or "T" not in start:
        return "all day"
    moment = dt.datetime.fromisoformat(start)
    return moment.strftime("%I:%M%p").lstrip("0").lower().replace(":00", "")


def _covers_date(event: dict, about: dt.date) -> bool:
    """Whether the event's half-open interval occupies this local calendar date."""
    start = str(event.get("start") or "")
    try:
        start_date = dt.date.fromisoformat(start[:10])
    except ValueError:
        return False
    if start_date > about:
        return False

    end = str(event.get("end") or "")
    if not end:
        return start_date == about
    try:
        end_date = dt.date.fromisoformat(end[:10])
    except ValueError:
        return start_date == about

    if event.get("all_day") or "T" not in end:
        # Calendar all-day end dates are exclusive, including multi-day trips.
        return start_date <= about < end_date
    if end_date > about:
        return True
    if end_date < about:
        return False
    # A timed event ending exactly at midnight does not occupy the new date.
    try:
        return dt.datetime.fromisoformat(end).time().replace(tzinfo=None) > dt.time()
    except ValueError:
        return start_date == about


def _agenda(events: list[dict], about: dt.date, prep: dict[str, str]) -> list[str]:
    lines = []
    for event in events:
        start = str(event.get("start") or "")
        if not _covers_date(event, about):
            continue
        where = f" ({event['location']})" if event.get("location") else ""
        lines.append(f"{_clock(start):>7}  {event.get('summary') or '(untitled)'}{where}")
        if note := prep.get(str(event.get("id"))):
            first, *rest = note.splitlines()
            lines.append(f"         prep: {first}")
            lines += [f"               {line}" for line in rest]
    return lines


# A fact the night kept about the user ends with the way to drop it, as the
# night wrote it (agent/night/learning.py): "... (FORGET MEMORY 12)".
_FORGET = re.compile(r"\s*\(FORGET MEMORY (\d+)\)\s*$")


def _noted(remembered: list[str]) -> str:
    """The facts kept about the user, each after its number, and the way to
    drop one said once: "(FORGET MEMORY 12)" after each fact, and "(FORGET
    MEMORY <number> drops one)" after them all, read as a tangle. The number
    comes from each fact's own words, so an earlier night's report reads the
    same."""
    facts = []
    for line in remembered:
        found = _FORGET.search(line)
        fact = (line[:found.start()] if found else line).strip().rstrip(".")
        facts.append(f"({found.group(1)}) {fact}" if found else fact)
    return ("Noted about you: " + "; ".join(facts)
            + ". To drop one, send FORGET MEMORY and its number.")


def _and(items: list[str]) -> str:
    """"x, y and z"."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _sentence(line: str) -> str:
    """A line as a sentence: its first letter capital, a full stop at its end.
    The failure ledger's and the action critic's lines are written to follow
    a colon ("noticed about itself: 3 slow replies")."""
    line = line.strip()
    line = line[:1].upper() + line[1:]
    return line if line.endswith((".", "!", "?")) else line + "."


def _night_lines(conn: sqlite3.Connection) -> list[str]:
    """The night's report, a line for each thing it did."""
    from agent import night

    row = conn.execute(
        "SELECT result FROM jobs WHERE kind = 'night' AND status = 'done'"
        " AND date(created_at, 'localtime') = date('now', 'localtime')"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row or not row["result"]:
        return []
    import json

    report = night.NightReport.from_dict(json.loads(row["result"]))
    lines = []
    if report.remembered:
        # Each whole, with its number: a fact about them kept unasked is told
        # the morning it was kept, with the way to drop it, like a grant.
        lines.append(_noted(report.remembered))
    if report.learned:
        learned = len(report.learned)
        lines.append(f"Learned {learned} thing{'' if learned == 1 else 's'}: "
                     + "; ".join(report.learned[:3]))
    if report.researched:
        lines.append("Read about " + _and(report.researched[:4]))
    if report.built:
        lines.append("Built: " + "; ".join(report.built[:2]))
    if report.improvement_paused:
        why = report.improvement_paused.strip()
        # Their own pause is a line the user knows. Any other reason is news the
        # morning must carry whole: under "(self-upgrade is paused)" it would
        # read as the pause the user set themselves.
        lines.append("Made no changes to itself: self-upgrade is paused"
                     if why.startswith("self-upgrade is paused")
                     else "Made no changes to itself: " + cut(why, 300, sentence=True))
    return lines


def gather(conn: sqlite3.Connection, kind: str, now: dt.datetime,
           about: dt.date | None = None) -> Facts:
    """Everything a briefing says, read from the calendar and the store.
    `about` is the day it is about: by default today's for the morning's,
    and tomorrow for the evening's.

    Blocking (the calendar), so run in a thread on its own connection.
    """
    from agent import approvals
    from agent.attention.sources import weather
    from tools import commitments

    if about is None:
        about = now.date() if kind == "morning" else now.date() + dt.timedelta(days=1)
    facts = Facts(kind=kind, about=about)

    due_by = now
    if kind == "morning":
        due_by = max(now, dt.datetime.combine(about, EARLY_UNTIL) - settings.BEFORE_LEAD)
    held = store.for_briefing(conn, limit=60, briefs_due_by=sqltime.stamp(due_by))
    held = _still_true(conn, held, now)
    cutoff = dt.datetime.now(dt.UTC) - STALE_AFTER
    prep: dict[str, str] = {}
    for obs in held:
        if obs.first_seen and sqltime.moment(obs.first_seen) < cutoff:
            facts.dropped.append(obs)
        elif obs.source == "news":
            (facts.news if len(facts.news) < MAX_NEWS else facts.dropped).append(obs)
        elif obs.say == "before":
            if obs.outcome and obs.ref.get("event_id"):
                prep[str(obs.ref["event_id"])] = obs.outcome
            facts.folded.append(obs)
        elif needs_user(obs):
            facts.needs_you.append(obs)
        elif len(facts.worth_knowing) < MAX_FYI:
            facts.worth_knowing.append(obs)

    from agent import accounts, features

    facts.unchecked = accounts.unchecked(conn)
    facts.calendar = features.has(conn, "calendar")
    try:
        from agent import calendars

        # Two days whatever the kind: the evening's look-ahead is about
        # tomorrow, which one day from tonight does not reach the end of.
        if facts.calendar:
            agenda = calendars.agenda(conn, 2, limit=40)
            facts.agenda = _agenda(agenda.get("events") or [], about, prep)
    except Exception as exc:  # noqa: BLE001 - a briefing without the calendar still goes
        log.warning("briefing without the calendar: %s", exc)
        facts.agenda = ["(could not read the calendar)"]

    from agent.home import home_city

    facts.home = home_city(conn) or ""
    if forecast := weather.day_line(conn, about):
        facts.weather = [forecast]
    # Where the user is going in the next few days, place by place.
    facts.weather += weather.trip_lines(conn, about)

    facts.waiting = [
        cardwords.listed(a.token, cardwords.first_line(a.preview))
        for a in approvals.pending(conn, 5)
    ]
    facts.due = [c.line() for c in commitments.due_soon(conn)]
    try:
        # What has gone quiet on their list, asked about: a card each, the
        # briefing the message that asks (agent/closing.py).
        from agent import closing

        facts.owed = closing.briefing_questions(conn)
    except Exception:  # noqa: BLE001 - a briefing without its questions still goes
        log.exception("could not ask about the open items")
    if kind == "morning":
        facts.night = _night_lines(conn)
        from agent import goals

        facts.goals = goals.briefing_lines(conn)
        from agent import issues

        # What went wrong in the last day, by kind: the failure ledger's
        # one line, so a bad day is seen the next morning, not by accident.
        if noticed := issues.briefing_line(conn):
            facts.night.append(noticed)
        if critiqued := critic_line(conn):
            facts.night.append(critiqued)
    from agent import caps

    facts.spend_notices = caps.for_the_briefing(conn)
    facts.spending = caps.lines(facts.spend_notices, now)
    return facts


def _still_true(conn: sqlite3.Connection, held: list[Observation],
                now: dt.datetime) -> list[Observation]:
    """`vet.recheck` on what the briefing holds. What it drops is marked so
    there and then; a fault in the checks leaves the items as they were."""
    if not held:
        return held
    checked = min(now, dt.datetime.now())
    try:
        kept, dropped = vet.recheck(conn, held, checked)
    except Exception:  # noqa: BLE001 - a briefing without the checks still goes
        log.exception("could not recheck the briefing's %d item(s)", len(held))
        return held
    for obs, why in dropped:
        log.info("left out of the briefing, no longer true: %s (%s)", obs.key, why)
    if now > checked:
        kept = [o for o in kept if vet.passed(o, now).ok]
    return kept


def needs_user(obs: Observation) -> bool:
    """Whether an observation goes under "Needs you": its reason, after the
    checks, is one that needs them. A row judged before reasons were kept
    (or with a code the judge misspelled) falls back to the old test, a
    `now` or critical verdict."""
    if obs.reason is not None:
        return obs.reason in vet.NEEDS_USER
    return obs.say == "now" or obs.critical


def critic_line(conn: sqlite3.Connection) -> str | None:
    """The action critic's last day, in shadow or enforcing (core/critic.py):
    how many unattended actions it reviewed, and which it would have asked
    them about -- or did. None on a day it reviewed nothing."""
    from core import critic
    from core.policy import load_policy

    found = critic.report(conn, days=1)
    if not found["reviewed"]:
        return None
    mode = load_policy().critic.mode
    held = found["would_have_stopped"]
    head = (f"the action critic ({'in shadow' if mode == 'shadow' else 'enforcing'}) reviewed "
            f"{found['reviewed']} unattended action{'s' if found['reviewed'] != 1 else ''}")
    if not held:
        return head + ", all plainly part of what you asked"
    named = []
    for item in held[:3]:
        reaches = json.loads(item["action"] or "{}").get("reaches") or []
        named.append(f"{item['tool']}{' to ' + ', '.join(reaches[:2]) if reaches else ''} "
                     f"({item['verdict'] or 'unreviewed'})")
    verb = "would have asked you about" if mode == "shadow" else "asked you about"
    return f"{head}; it {verb} {len(held)}: {'; '.join(named)}"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


# The reason code as the user reads it, after a "Needs you" item.
REASON_WORDS = {"changes-plans": "changes your plans", "reply-owed": "a reply owed",
                "deadline": "a deadline", "money": "money", "safety": "safety"}


def _item(obs: Observation, *, reason: bool = False) -> str:
    line = f"  · {obs.headline or obs.title}"
    if reason and obs.reason in REASON_WORDS:
        line += f" ({REASON_WORDS[obs.reason]})"
    if obs.outcome:
        line += "".join(f"\n      {part}" for part in obs.outcome.splitlines())
    return line


def render(facts: Facts, lead: str = "") -> str:
    """The briefing, from the facts. Everything here is exact."""
    parts = [facts.title]
    if lead:
        parts.append(lead.strip())
    if facts.waiting:
        parts.append("Waiting on you:\n" + "\n".join(facts.waiting))
    if facts.unchecked:
        parts.append("Could not check:\n" + "\n".join(f"  · {line}" for line in facts.unchecked))
    # What needs them, then what is worth knowing, then the day: the order of
    # the 10 October redesign.
    if facts.needs_you or facts.owed:
        parts.append("Needs you:\n" + "\n".join(
            [_item(o, reason=True) for o in facts.needs_you]
            + [f"  · {line}" for line in facts.owed]))
    if facts.worth_knowing:
        parts.append("FYI:\n" + "\n".join(_item(o) for o in facts.worth_knowing))
    # The night in the middle, and the day and its weather last: the top and
    # the bottom are what is read (the maintainer, 19 October 2026). The night
    # came last, and the day and its weather straight after FYI.
    if facts.night:
        parts.append("Overnight:\n" + "\n".join(
            f"  · {_sentence(line)}" for line in facts.night))
    if facts.due:
        parts.append("Due soon:\n" + "\n".join(f"  · {d}" for d in facts.due))
    if facts.goals:
        parts.append("Goals:\n" + "\n".join(facts.goals))
    if facts.spending:
        parts.append("Spending:\n" + "\n".join(f"  · {line}" for line in facts.spending))
    if facts.news_summary:
        parts.append("In the news:\n" + "\n".join(
            f"  · {line.lstrip('-•· ').strip()}" for line in facts.news_summary.splitlines()))
    elif facts.news:
        parts.append("In the news:\n" + "\n".join(_item(o) for o in facts.news))
    label = "Today" if facts.kind == "morning" else "The day"
    if facts.calendar:
        parts.append(f"{label}:\n" + ("\n".join(facts.agenda) or "  nothing on the calendar"))
    if facts.weather:
        parts.append("Weather: " + "; ".join(facts.weather))
    return "\n\n".join(parts)


async def compose(conn: sqlite3.Connection, facts: Facts, briefing_key: str) -> str:
    """The briefing's text: the facts, the news summarised, and a lead.

    The news first, so the lead is written knowing it.
    """
    from agent.attention import interests, newsdesk

    stories = [
        {"title": re.sub(rf"\s*\({re.escape(o.ref.get('outlet') or '')}\)$", "", o.title)
         if o.ref.get("outlet") else o.title,
         "outlet": o.ref.get("outlet") or "", "key": o.key}
        for o in facts.news
    ]
    topics = [i.topic for i in interests.active(conn)]
    facts.news_summary = await newsdesk.for_briefing(
        conn, stories, kind=f"{facts.kind} briefing", topics=topics, briefing=briefing_key)
    return render(facts, await lead(conn, facts))


async def lead(conn: sqlite3.Connection, facts: Facts) -> str:
    """The one or two lines a model adds. Empty if it cannot, or may not."""
    try:
        from agent.llm import ask_once
        from agent.models import Role

        kind = "morning briefing" if facts.kind == "morning" else "evening look-ahead"
        day = "today" if facts.kind == "morning" else "tomorrow"
        with spend.spending("background", part="work", conn=conn):
            if spend.may_spend(conn):
                return ""
            output, _ = await ask_once(Role.CONVERSE, LEAD_PROMPT.format(kind=kind, day=day),
                                       render(facts))
    except Exception as exc:  # noqa: BLE001 - the briefing goes without a lead
        log.warning("no lead for the %s briefing: %s", facts.kind, exc)
        return ""
    # Whole. The prompt asks for a sentence or two; a hard cut at 300
    # characters only ever ended a longer one mid-word (the maintainer, 4 October
    # 2026: no character limits on what the user reads).
    return " ".join(str(output or "").split())


# ---------------------------------------------------------------------------
# When, and sending
# ---------------------------------------------------------------------------


def _cursor(kind: str) -> str:
    return f"briefing:{kind}"


def sent_on(conn: sqlite3.Connection, kind: str) -> str | None:
    row = conn.execute(
        "SELECT cursor FROM sync_cursors WHERE source = ?", (_cursor(kind),)
    ).fetchone()
    return row["cursor"] if row else None


def _mark_sent(conn: sqlite3.Connection, kind: str, day: dt.date) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO sync_cursors (source, cursor, ran_at) VALUES (?, ?, datetime('now'))"
            " ON CONFLICT(source) DO UPDATE SET cursor = excluded.cursor, ran_at = excluded.ran_at",
            (_cursor(kind), day.isoformat()),
        )


def set_for(kind: str, now: dt.datetime) -> dt.datetime:
    """The latest time at or before `now` that briefing `kind` was set for:
    yesterday's, before today's has come."""
    at = settings.MORNING if kind == "morning" else settings.EVENING
    when = dt.datetime.combine(now.date(), at)
    return when if when <= now else when - dt.timedelta(days=1)


def due(conn: sqlite3.Connection, now: dt.datetime) -> str | None:
    """Which briefing should go out now, if any: from its time until
    `LATE_BY` after, once for the day it was set for, and never in quiet
    hours."""
    if call_policy.within_quiet_hours(now):
        return None
    for kind in ("morning", "evening"):
        when = set_for(kind, now)
        if now < when + LATE_BY and sent_on(conn, kind) != when.date().isoformat():
            return kind
    return None


def key_for(kind: str, day: dt.date) -> str:
    """What a briefing is known by in `told`: its kind and the day it is about."""
    return f"briefing:{kind}:{day.isoformat()}"


async def step(conn: sqlite3.Connection, *, now: dt.datetime | None = None) -> dict:
    """One pass of the briefing loop: send the briefing that is due, if one is."""
    now = now or dt.datetime.now()
    kind = due(conn, now)
    return {"sent": {kind: await send(conn, kind, now=now)}} if kind else {}


def seconds_until_next(now: dt.datetime) -> float:
    """Until the next briefing time, from a local `now`."""
    moments = []
    for at in (settings.MORNING, settings.EVENING):
        when = dt.datetime.combine(now.date(), at)
        if when <= now:
            when += dt.timedelta(days=1)
        moments.append(when)
    return max(1.0, (min(moments) - now).total_seconds())


async def send(
    conn: sqlite3.Connection,
    kind: str,
    *,
    notify=None,
    now: dt.datetime | None = None,
) -> dict:
    """Gather, write and send one briefing. Returns what happened."""
    from agent import offloop

    now = now or dt.datetime.now()
    if notify is None:
        from functools import partial

        from agent.worker.links import notify_user

        # Not after it is too late to be worth reading: the outbox stops
        # trying the phone then, as `due` stops writing it.
        notify = partial(notify_user, kind="briefing", until=set_for(kind, now) + LATE_BY)
    # The day it was set for, not the day it went: an evening look-ahead
    # that goes after midnight is still about the day after its evening.
    day = set_for(kind, now).date()
    about = day if kind == "morning" else day + dt.timedelta(days=1)
    facts = await offloop.with_own_conn(conn, gather, kind, now, about)
    if kind == "evening" and not facts.has_substance():
        _mark_sent(conn, kind, day)
        store.delivered(conn, [o.id for o in facts.dropped], "dropped")
        return {"sent": False, "why": "nothing for tomorrow"}

    text = await compose(conn, facts, key_for(kind, facts.about))
    message_id = await notify(text)

    _mark_sent(conn, kind, day)
    spend.told(conn, facts.spend_notices)
    store.delivered(conn, [o.id for o in facts.carried], kind)
    store.delivered(conn, [o.id for o in facts.dropped], "dropped")
    try:
        told.record(conn, key_for(kind, facts.about), kind, told_lines(facts),
                    outbox_id=message_id)
    except Exception:  # noqa: BLE001 - the briefing is in the outbox; only its record is lost
        log.exception("could not record what the %s briefing said", kind)
    return {"sent": True, "items": len(facts.carried), "chars": len(text)}


def told_lines(facts: Facts) -> list[told.Line]:
    """Every line a briefing says, with where it came from (`told.py`)."""
    lines = [told.of(o, _item(o, reason=True).strip(" ·")) for o in facts.needs_you]
    lines += [told.Line(line, source="obligations") for line in facts.owed]
    lines += [told.Line(line, source="accounts") for line in facts.unchecked]
    lines += [told.of(o, _item(o).strip(" ·")) for o in
              (*facts.worth_knowing, *facts.news, *facts.folded)]
    lines += [told.Line(line.strip(), source="calendar") for line in facts.agenda]
    for line in facts.weather:
        # A trip's kept forecast starts with its place ("Boston,
        # Massachusetts: Thu: ..."); home's does not.
        head, _, _ = line.partition(": ")
        lines.append(told.Line(line, source="weather",
                               place=head if "," in head else (facts.home or None)))
    lines += [told.Line(line, source="commitments") for line in facts.due]
    lines += [told.Line(line, source="spending") for line in facts.spending]
    return lines
