"""Attention: noticing things and doing something about them, unasked.

The user's words, 24 September 2026: an always-on loop that flags anything
important, prepares what it can, and tells them -- in briefings, in timely
pings, and at night only when it matters. It replaces two sweeps that read
the same inbox with different keys and different rules (`watch.py` every
three hours, `initiative.py` every four) and between them sent them about
two messages a day.

One pipeline, one row per thing (`store`), five stages:

    sources   code, frequent, cheap. What arrived or changed: mail in both
              mailboxes, calendar changes, the week ahead, commitments
              coming due, files others changed, the weather as it bears on
              their plans, and headlines from the news the user follows. Each has
              its own cadence (`settings.CADENCE_S`) and runs off the event
              loop.
    triage    one cheap model call per batch, no tools: for each new thing,
              say it now, before it happens, in the next briefing, or
              never; whether it is critical; a headline; and optionally a
              task.
    prepare   a tool-using conversation, reversible tools only, that does
              the tasks: an event, a reminder, a draft reply, a fact looked
              up.
    deliver   code: one message per tick, within the day's and the night's
              caps, marked delivered once it is in the outbox.

That is the order an item goes through. A tick runs the doer last, so that
no ping waits on its tasks; what it prepares goes out next tick.

And twice a day, `briefing`: the morning briefing (with the night's report)
and the evening look-ahead, carrying everything held for them.

The division of labour is the design. The model judges what matters and
does the work; the code decides how often the user hears, what the doer may
touch, and when the night is quiet -- because a model asked "is this worth
interrupting them for?" has no memory of the times it already did today.

Runs as a duty (`agent/worker/background.attention_loop`), so exactly one
worker runs it. `python -m agent.attention` runs one tick against a copy of
the database and prints what it would do; see `__main__`.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field

from agent.attention import deliver, prepare, sources, store, triage

log = logging.getLogger("attention")


@dataclass
class TickResult:
    at: str = ""
    ran: list[str] = field(default_factory=list)
    noticed: int = 0
    triage: dict = field(default_factory=dict)
    prepare: dict = field(default_factory=dict)
    deliver: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "at": self.at, "ran": self.ran, "noticed": self.noticed,
            "triage": self.triage, "prepare": self.prepare, "deliver": self.deliver,
        }


async def look(
    conn: sqlite3.Connection, *, only: list[str] | None = None, force: bool = False
) -> tuple[list[str], int]:
    """Run the sources that are due and record what is new.

    `force` ignores cadences; `only` restricts to named sources. Both are
    for running a tick by hand.
    """
    from agent import offloop

    everything = await offloop.with_own_conn(conn, sources.watching)
    if only:
        everything = [s for s in everything if s.name in only]
    ready = everything if force else sources.due(conn, everything)
    noticed = 0
    for source in ready:
        found = await offloop.with_own_conn(conn, sources.collect, source)
        fresh = store.record_new(conn, found)
        noticed += len(fresh)
        if store.is_mail(source.name) and fresh:
            # A reply a goal is waiting on brings its next session forward
            # (agent/goals.py). It only ever moves a time; never fatal here.
            try:
                from agent import goals

                goals.woken_by_mail(conn, [o.ref.get("from") for o in fresh])
            except Exception:  # noqa: BLE001 - looking must not fail for a goal
                log.exception("waking goals failed")
    return [s.name for s in ready], noticed


async def tick(
    conn: sqlite3.Connection,
    *,
    notify: deliver.Notify | None = None,
    do_tasks: bool = True,
    only: list[str] | None = None,
    force: bool = False,
) -> TickResult:
    """One pass through the pipeline. Never raises for a stage's failure.

    `notify` defaults to the worker's own; a dry run passes its own, or it
    would message them about a copy's items."""
    result = TickResult(at=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        result.ran, result.noticed = await look(conn, only=only, force=force)
    except Exception:  # noqa: BLE001 - judging what is already stored still helps
        log.exception("looking failed")

    result.triage = (await triage.triage(conn)).as_dict()
    # The doer goes last. Ahead of delivery, every ping in the tick -- a
    # critical one too -- waited for all its tasks: a minute or two each, and
    # with a browsing worker (since 11 October 2026) up to five. An item with
    # a task waits for it anyway (`store._PREPARED`), so what the doer
    # prepares goes out next tick.
    result.deliver = (await deliver.deliver(conn, notify=notify)).as_dict()
    if do_tasks:
        result.prepare = (await prepare.prepare(conn)).as_dict()
    return result


def status(conn: sqlite3.Connection) -> dict:
    """What the loop has been doing, for `/attention/status`. Counts only:
    no headline or subject leaves the laptop through a status page."""
    from agent.attention import briefing, settings
    from core import spend

    today, caps = spend.windows().day.since, spend.limits()
    return {
        "sources": sources.last_runs(conn),
        "briefings_sent": {k: briefing.sent_on(conn, k) for k in ("morning", "evening")},
        "today": store.counts_today(conn),
        "spent_today_usd": {
            "judging": round(spend.spent(conn, today, part="triage"), 4),
            "work": round(spend.spent(conn, today, part="work"), 4),
        },
        "budgets_usd": {
            "judging": caps["TRIAGE_BUDGET_USD"], "work": caps["WORK_BUDGET_USD"],
        },
        "quiet_now": settings.within_quiet_hours(),
        "caps": {"day_pings": settings.MAX_DAY_PINGS, "night_pings": settings.MAX_NIGHT_PINGS},
        "may_use": sorted(prepare.PREPARE_TOOLS),
    }
