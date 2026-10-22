"""Free time: a few hours in the middle of the night to read and to tinker.

The agent does not have real agency, and pretending otherwise would be
dishonest. What it has here is a window with nobody waiting on it, a budget,
and two open-ended instructions -- learn something, improve something -- with
the choice of *what* left to it. That is a narrow kind of self-direction, but
it is a real one: nothing in this module decides the topics or the changes.

Two blocks, in this order on purpose:

**Research first.** It is cheap, it cannot break anything, and what it learns
lands in the memories repo where the improvement block can see it. Topics are
the agent's own choice. Reflection scores are offered as a hint -- `grounded`
has been the weak dimension, so being nudged toward what it has been vague
about is useful -- but only as a hint, and one slot is deliberately left for
whatever it is actually curious about. An agent given free time that is
entirely instrumental has not been given free time.

**Improvement second**, and bounded hard. Until step 6 a change went through
a gate, and additive ones merged unattended. Now a change Steve makes for
itself is a module, waiting for the user's card like any other, and the
night's proposals come back as module builds; until then it changes nothing
of itself (`tinkering.improve`).

The budget is a hard stop checked between every step, not a target. Nothing
here is important enough to run over.

How it is laid out, one block to a module: `learning` (who the user deals with,
what they promised, how the user writes), `reading` (the research block),
`tinkering` (the improvement block),
and `report` (what the night did, and the morning message about it).
`model` is the one call the blocks make to a model, and the seam tests
patch. This module is the window: when it opens, how the money is split,
and the order the blocks run in.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from datetime import time as clock

from agent import caps
from agent.night.learning import learn
from agent.night.reading import research
from agent.night.report import Budget, NightReport, digest
from agent.night.scouting import scout
from agent.night.tinkering import improve
from core import db, spend

__all__ = [
    "DEFAULT_BUDGET_USD",
    "Budget",
    "NightReport",
    "digest",
    "improve",
    "learn",
    "research",
    "run_night",
    "seconds_until_next_window",
    "take_up",
]

log = logging.getLogger("night")

# The window opens in the middle of the user's quiet hours: 03:00 for the
# code's 22:00 to 08:00, midday for someone asleep from 08:00 to 16:00. It
# was 3am, fixed, which for anyone who sleeps by day is the middle of their
# working night, and the night's coding attempts and suites are heavy.
#
# A night cut short -- by a swap or a restart after it opened -- is started
# again by the worker that takes over (`take_up`) until this long before
# quiet hours end. Nights have taken up to 1 h 42 min: one started later
# would share their laptop with their morning, and miss the briefing that
# reads it out.
TAKE_UP_UNTIL_BEFORE_END = timedelta(hours=2)

# What one night may spend, and how it splits. Research is cheap per topic, so
# an even split buys several topics against one or two code changes.
#
# $2 since 30 September 2026, the most the frozen policy lets the pipeline
# spend in a day (`coder.night_budget_usd`): the user, "2 bucks a night is fine
# if need be, it doesn't need to spend that much though". At $1 the night
# could afford two attempts at most. A ceiling, not a target.
DEFAULT_BUDGET_USD = 2.00
RESEARCH_SHARE = 0.5
# The extra dollar is for building. Learning and research keep what they had
# at $1 -- they have never come near it -- so they cannot grow into it.
RESEARCH_MAX_USD = 0.50

# How much of the night's money the learning block may spend before the other
# two start. Small: ingest is free and the notes are one short call each.
LEARN_SHARE = 0.25
LEARN_MAX_USD = 0.25


async def run_night(
    conn: sqlite3.Connection,
    *,
    budget_usd: float = DEFAULT_BUDGET_USD,
    do_learn: bool = True,
    do_research: bool = True,
    do_improve: bool = True,
    do_scout: bool = True,
) -> NightReport:
    """One night's free time. Never raises.

    Every model call in it is the night's (core/spend.py), and so is all it
    starts -- research, the coder, a review -- since the outermost unit of
    work decides the category. Until step 3 a night's calls through
    `ask_once` reached no ledger at all.

    What it may spend is what is left of tonight's cap in the ledger, so a
    night started again after a swap counts what it spent before; it was a
    fresh $2 in memory each time. The cap is asked before each block, and a
    block it refuses is not started: the digest says so, and names what was
    not done."""
    with spend.spending("night", conn=conn):
        started = time.perf_counter()
        report = NightReport(started_at=datetime.now().strftime("%Y-%m-%d %H:%M"))
        tonight = spend.spent(conn, spend.windows().night.since, category="night")
        cap = spend.limits()["NIGHT_USD"]
        budget = Budget(max(0.0, min(budget_usd, cap) - tonight))

        def afford(block: str) -> bool:
            """Whether `block` may start: no cap reached yet. Once one is,
            this block and every one after it are listed as not done.

            A night cap of nothing is the user's choice of no paid work at
            night, not a limit reached, so its blocks are skipped without
            asking. Asked, the cap refused each night and noted "its $0.00
            nightly limit was reached", with an offer to raise it, and the
            morning listed every block as not done."""
            if cap <= 0:
                return False
            if report.not_done or (refusal := spend.may_spend(conn)) is None:
                if report.not_done:
                    report.not_done.append(block)
                return not report.not_done
            report.stopped_because = caps.reached(refusal)
            report.not_done.append(block)
            return False

        try:
            # Until 12 October 2026 the night began by deleting every `wt-*`
            # folder beside the repo that this repo's git did not list: the
            # worktrees the old fallback coder left when killed. Another
            # install from the same code, checked out beside this one, keeps
            # its live worktrees in that same folder, and its git's list is
            # not this one's. With that coder gone nothing of Steve's is made
            # there, so nothing is swept.
            if do_learn and afford("learning"):
                # First, and not for tidiness. This block reads the day, and the
                # day is over.
                learn_budget = Budget(min(budget.limit * LEARN_SHARE, LEARN_MAX_USD))
                await learn(conn, learn_budget, report)
                budget.add(learn_budget.spent)
            if do_learn and afford("skills"):
                # The day's runs of what the user asked, written up as skills where
                # they were worth it (agent/night/skilling.py): reading the day
                # too, and a few cents at most.
                from agent.night import skilling

                budget.add(await skilling.distill(conn, report))

            if do_research and afford("research") and not budget.exhausted(headroom=0.10):
                research_budget = Budget(min(budget.limit * RESEARCH_SHARE, RESEARCH_MAX_USD))
                await research(conn, research_budget, report)
                budget.add(research_budget.spent)

            if do_improve and afford("changes to itself") and not budget.exhausted(
                    headroom=0.10):
                await improve(conn, budget, report)

            if do_scout and afford("model scouting"):
                # Last, on the scout's own budget rather than the night's
                # (agent/night/scouting.py); added to the total so the morning's
                # figure is what the night really spent.
                budget.add(await scout(conn, report))
            if do_scout and afford("the yardstick"):
                # The yardstick, weekly too, in a process of its own
                # (agent/night/yardstick.py), on its own budget.
                from agent.night import yardstick

                measured = await yardstick.measure(report)
                # Its child bills into scratch databases of its own
                # (agent/autonomy_eval.py), so the night's ledger hears of
                # it here, once, from the cost it reports.
                if measured:
                    with db.transaction(conn):
                        spend.bill(conn, measured, model="yardstick")
                budget.add(measured)
            if do_scout:
                # The desk's rehearsal (agent/night/rehearsal.py), last of all:
                # after the coding attempts, whose suites it would slow, and in a
                # process of its own. Free -- the rig's fakes answer for them --
                # so no cap keeps it from running.
                from agent.night import rehearsal

                budget.add(await rehearsal.rehearse(conn, report))
        except spend.Refused as refusal:
            # A cap reached partway through a block: its request in flight
            # finished and was billed (core/spend.Guarded); the rest waits.
            report.stopped_because = caps.reached(refusal)
        except Exception as exc:  # noqa: BLE001 - the morning report still happens
            log.exception("night run failed")
            report.stopped_because = f"{type(exc).__name__}: {exc}"

        report.cost_usd = budget.spent
        report.seconds = time.perf_counter() - started
        _store(conn, report)
        return report


def _store(conn: sqlite3.Connection, report: NightReport) -> None:
    import json

    with db.transaction(conn):
        conn.execute(
            "INSERT INTO jobs (kind, prompt, why, status, result, finished_at)"
            " VALUES ('night', ?, 'nightly free time', 'done', ?, datetime('now'))",
            (report.started_at, json.dumps(report.as_dict())),
        )


def _window() -> tuple[clock, timedelta]:
    """When the window opens, local, and how long after that a night cut
    short is still started again."""
    from core import call_policy

    night = timedelta(hours=call_policy.night_hours(call_policy.QUIET_FROM,
                                                    call_policy.QUIET_UNTIL))
    opens = (datetime.combine(datetime.min.date(), call_policy.QUIET_FROM) + night / 2).time()
    return opens, night / 2 - TAKE_UP_UNTIL_BEFORE_END


def seconds_until_next_window(now: datetime | None = None) -> float:
    """How long until the window next opens, in seconds."""
    now = now or datetime.now()
    target = datetime.combine(now.date(), _window()[0])
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def take_up(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    """Whether a worker taking up its duties now should start tonight's
    night at once, rather than sleep to the window's next opening -- noting,
    when it should, that it has.

    The window opens on a clock, so a worker that came up after it opened
    slept a day, and a night the worker before it had not finished was
    lost: on 11 October 2026 a swap at 03:04, landing a fix, retired the
    worker four minutes into its night, and nothing ran until the 12th.
    So: the window open, not past `TAKE_UP_UNTIL_BEFORE_END`, no night
    finished since it opened -- and none taken up already, since a worker
    that keeps dying in the window must not start a fresh night every time
    it comes back. Nor for an install younger than the window: its database
    made after the window opened, no worker of it was up to start a night.
    A stranger setting Steve up at 3:30 am had tonight's research and
    building start beside their setup, with nothing yet to read about, and
    spend up to the night's $2. Never raises: a worker that cannot tell
    sleeps to the next opening, as it always did."""
    from agent import sqltime

    now = now or datetime.now()
    opens, open_for = _window()
    opened = datetime.combine(now.date(), opens)
    if opened > now:
        opened -= timedelta(days=1)
    if not now < opened + open_for:
        return False
    try:
        with db.transaction(conn):
            # When the database was made: its first migration's.
            made = conn.execute("SELECT MIN(applied_at) FROM schema_version").fetchone()[0]
            if not made or made >= sqltime.stamp(opened):
                return False
            if conn.execute(
                "SELECT 1 FROM jobs WHERE kind IN ('night', 'night_taken_up')"
                " AND finished_at >= ?", (sqltime.stamp(opened),),
            ).fetchone():
                return False
            # A row like the night's own report (`_store`): done, and not news.
            conn.execute(
                "INSERT INTO jobs (kind, prompt, why, status, notified, finished_at)"
                " VALUES ('night_taken_up', ?, 'a night cut short, started again', 'done', 1, ?)",
                (now.strftime("%Y-%m-%d %H:%M"), sqltime.stamp(now)),
            )
    except Exception:  # noqa: BLE001 - asked before the loop's own try: raising ends the duty
        log.warning("could not tell whether tonight's night was cut short", exc_info=True)
        return False
    return True
