"""The background loops: the heartbeat, the outbox, reminders, the attention
loop, the night and its briefings, reflection, card expiry, the wake alarm,
and vectors for what has none.

Each runs as a duty, so exactly one worker runs it -- see `duties`. Each
pauses on `runtime.nap`, never `asyncio.sleep`, so that when the heartbeat
notices the computer waking every pause ends and what came due while it
slept goes at once (agent/awake.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from agent import (
    approvals,
    attention,
    night,
    reflect,
    reminders,
    runtime,
)
from agent.attention import briefing
from agent.attention import settings as attention_settings
from agent.worker import answering, links
from core import spend
from tools import code_exec
from voice import level

log = logging.getLogger("worker")


# How often to look for conversations that have gone quiet. Well under the
# idle threshold, so a session reflects shortly after it actually ends.
SWEEP_INTERVAL_S = 300

# When the morning briefing goes out, for the wake alarm. The briefing's
# own clock is `attention.settings.MORNING`; this is that hour.
DIGEST_HOUR = attention_settings.MORNING.hour

# How often the briefing loop checks whether a briefing is due, besides
# waking at the briefing times themselves: the laptop may have slept
# through one.
BRIEFING_CHECK_S = 300

# How often to look for cards that have gone stale. A card is answered in
# minutes or not at all, so this is slow on purpose -- its only job is to
# stop a twelve-hour-old "send that email?" still looking live at bedtime.
ACTION_SWEEP_S = 600

# How often to re-point the wake alarm. Two minutes is well inside the
# ninety-second lead time it arms with, and arming is free when nothing
# has moved.
WAKE_REARM_S = 120

# How often to look whether the user has turned RUN_CODE or BROWSER on, so
# their images are built (`build_sandbox`, `build_browser`).
SANDBOX_LOOK_S = 60

# How often to look for finished work never put in the outbox (a worker
# that stopped between finishing it and saying so), and for spending notices.
RESEND_EVERY_S = 300

# How often the outbox looks for a row due a try, besides being woken by a
# new one: well under its shortest back-off (agent/outbox.RETRY_AFTER).
OUTBOX_EVERY_S = 30

# How often to look for memories and facts with no vector (`reembed`).
REEMBED_EVERY_S = 3600

# How often to look for spoken exchanges that have ended (`spoken_sweep`).
SPOKEN_SWEEP_S = 60

# How often the voice's level is told while it listens or speaks: "a few
# times a second" (docs/design/presence.md).
LEVEL_EVERY_S = 0.125

# How often to delete attachments past their day (`attachment_sweep`): one
# is kept at most an hour over it.
ATTACHMENT_SWEEP_S = 3600


async def _forever(what: str, step, every_s) -> None:
    """`step()`, then a pause of `every_s` seconds -- or of `every_s()`,
    worked out after each round -- for as long as the worker runs. A round
    that raises is logged as `what` failing, and the next goes ahead: one
    bad round is not the loop. Eleven loops here spelled this out each, and
    two more swallowed every failure unlogged.

    Each round is background work (core/spend.py): what it spends is
    background's, whatever channel a conversation it holds is on. The pause
    ends early when the computer wakes (`runtime.nap`)."""
    while True:
        try:
            with spend.spending("background", conn=runtime.conn):
                await step()
        except Exception:  # noqa: BLE001 - one bad round is not the loop
            log.exception("%s failed", what)
        await runtime.nap(every_s() if callable(every_s) else every_s)


async def heartbeat() -> None:
    """Beat every minute, and notice the computer waking (agent/awake.py):
    then every loop's pause ends, so what came due while it slept goes now.
    And hold the computer awake while it is plugged in, if the user asked."""
    from agent import awake
    from core import knobs

    async def step():
        awake.tick(runtime.conn, keep_awake=knobs.on("KEEP_AWAKE"))

    try:
        await _forever("heartbeat", step, awake.BEAT_S)
    finally:
        awake.let_go()


async def outbox_loop() -> None:
    """Send what the outbox holds to the remote channel (`links.send_due`):
    at once when a message is written, otherwise every `OUTBOX_EVERY_S`, for
    the rows whose back-off is over."""
    runtime.outbox_wake = asyncio.Event()

    async def step():
        runtime.outbox_wake.clear()
        if went := await links.send_due():
            log.info("sent %d message(s) from the outbox", went)

    async def pause():
        # A new message sets the event, and so does a wake (`runtime.woke`).
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(runtime.outbox_wake.wait(), OUTBOX_EVERY_S)

    while True:
        try:
            await step()
        except Exception:  # noqa: BLE001 - one bad round is not the loop
            log.exception("outbox round failed")
        await pause()


async def resend_loop() -> None:
    """Put in the outbox the background results that never went there
    (`handlers.resend_unannounced`): the worker stopped between finishing a
    job and saying so. And the month's spending cap, nearly or wholly
    reached, once quiet hours are over (agent/caps.py): it stops everything,
    so it does not wait for a briefing."""
    from agent import caps
    from agent.worker import handlers, links

    async def step():
        if sent := await handlers.resend_unannounced():
            log.info("told %d background result(s) that had not been", sent)
        await caps.by_message(runtime.conn, links.notify_user)

    await _forever("resend sweep", step, RESEND_EVERY_S)


async def account_health_loop() -> None:
    """Check every account the user added, and say once on their channel
    what has been down six hours or ends within a day (`core/health.py`).
    Google's sign-in ran out unannounced on 26 September. A loop of its own
    said Google's after that, until item 9 made Google an account like any
    other; now every account's is said here, the same way."""
    from agent import accounts

    async def step():
        await accounts.check_all(runtime.conn)
        if sent := await accounts.tell(runtime.conn, links.notify_user):
            log.info("told the user about %d account(s)", len(sent))

    await _forever("account health check", step, accounts.CHECK_EVERY_S)


async def server_idle_loop() -> None:
    """Stop each local MCP server no call has used for a while: each holds
    its own memory, up to half a gigabyte, and the next call that needs it
    starts it again (`core/sandbox/servers.Pool`)."""
    from core.sandbox import servers

    async def step():
        if stopped := await asyncio.to_thread(servers.POOL.stop_idle):
            log.info("stopped %d idle local server(s)", len(stopped))

    await _forever("idle server check", step, 60)


async def reminder_loop() -> None:
    """Fire reminders and recurring checks when they come due.

    No quiet hours here, unlike the watcher. That one is unsolicited and
    keeps to daylight; these were asked for, and an alarm that declines to go
    off at six in the morning is not an alarm.
    """
    async def step():
        for reminder in reminders.due(runtime.conn):
            await _fire_reminder(reminder)

    await _forever("reminder tick", step, reminders.TICK_S)


async def _fire_reminder(reminder: reminders.Reminder) -> None:
    """Do whatever this reminder means, and move it on once it is told.

    A reminder went to `done` before it was sent, so one that nothing could
    deliver (Signal down) was recorded as gone off and never arrived (the
    scouts' audit, 10 October 2026). Then it was moved on only once Signal
    took it, and tried again a minute later from a dict in memory. Now it
    moves on once it is in the outbox, which the app shows at once and which
    retries the phone itself (agent/outbox.py).
    """
    answer: str | None = None
    message = reminder.text

    if reminder.kind == "check":
        try:
            answer, _meta = await answering.voice_reply(
                # Its own thread. A reminder checking on something is not
                # part of the conversation the user is having on their phone, and
                # splicing it in would read as a non-sequitur to both.
                channel="reminder",
                text=f"{reminder.text}\n\n"
                "Answer in one short sentence, just the current state. No "
                "preamble, no offer to keep watching -- that is already "
                "arranged."
            )
        except Exception as exc:  # noqa: BLE001 - report the failure, keep going
            answer = f"(couldn't check: {type(exc).__name__})"
        message = answer
        finished = reminders.advance(runtime.conn, reminder, result=answer)
        if not reminders.should_report(reminder, answer or "", finished=finished):
            return
        said = f"{'Last check — ' if finished else ''}{message}".strip()
        await links.notify_user(said, kind="reminder",
                                noted=f"[check {reminder.id} went off] {said}")
        return

    said = message.strip()
    # The user sees just "Mop"; the conversation records what it was. As bare
    # text, two reminders going off and their "both complete" read to the
    # model as being about the gym and the run the user had just mentioned, and it
    # cancelled the week's six gym and run reminders.
    await links.notify_user(said, kind="reminder",
                            noted=f"[reminder {reminder.id} went off] {said}",
                            reminder_id=reminder.id)
    reminders.advance(runtime.conn, reminder)


async def attention_loop() -> None:
    """Notice, judge, prepare and tell, every minute, for as long as it runs.

    One loop where there used to be two -- a watcher every three hours and
    an initiative sweep every four -- reading the same inbox with different
    keys and different rules. See `agent/attention/`.

    While the computer sleeps nothing is watched; when it wakes, every
    source is overdue and the first tick catches up.
    """
    async def step():
        result = await attention.tick(runtime.conn)
        runtime.attention_state.update(last_tick=result.as_dict())

    await _forever("attention tick", step, attention_settings.TICK_S)


async def night_loop() -> None:
    """Sleep until 3am and take the free time.

    A loop around a clock rather than a cron entry, because the worker is
    already a long-lived process: a swap before 3am hands the window to the
    new worker, and one after it hands over a night the old worker had not
    finished, which the new one starts again (`night.take_up`). The report
    is stored by the night itself (`night._store`) and read out by the
    morning briefing -- whichever worker is up at 8am, which used to have to
    be this one.
    """
    owed = night.take_up(runtime.conn)
    while True:
        try:
            if not owed:
                await until_the_night()
            else:
                log.info("starting tonight's night again: a swap or a restart cut it short")
            owed = False
            report = await night.run_night(runtime.conn)
            log.info("night finished: %s", report.as_dict())
        except Exception:  # noqa: BLE001 - tomorrow night should still happen
            log.exception("night run failed")


# A wait for the night's window that ends this many seconds late was the
# event loop's lag. Later than that, the computer slept through the opening.
NIGHT_ON_TIME_S = 60


async def until_the_night(now=datetime.now) -> None:
    """Wait for the night's window to open (`night.seconds_until_next_window`).

    A computer asleep at the opening woke to a wait that had ended, and the
    night's suites then ran beside the user's morning. Now a wait that ends
    late starts the night only while a night cut short may still be taken up
    (`night.take_up`); past that, it waits for the next night."""
    opens = now() + timedelta(seconds=night.seconds_until_next_window(now()))
    while True:
        left = (opens - now()).total_seconds()
        if left > 0:
            await runtime.nap(left)
        elif -left < NIGHT_ON_TIME_S or night.take_up(runtime.conn, now()):
            return
        else:
            log.info("asleep when the night's window opened (%s); waiting for the next",
                     f"{opens:%a %H:%M}")
            opens = now() + timedelta(seconds=night.seconds_until_next_window(now()))


async def briefing_loop() -> None:
    """The morning briefing and the evening look-ahead, once each a day.

    Every few minutes while awake, send a briefing that is due. A computer
    that slept through 8:00 sends the morning's when it wakes, until it is
    too late to be worth reading (`briefing.due`).
    """
    async def step():
        if done := await briefing.step(runtime.conn):
            log.info("briefings: %s", done)

    await _forever("briefing", step,
                   lambda: min(BRIEFING_CHECK_S, briefing.seconds_until_next(datetime.now())))


async def reflection_sweep() -> None:
    """Close quiet sessions and queue their reflections, forever.

    On its own timer rather than hooked to a turn ending, because the thing it
    waits for is the *absence* of turns.
    """
    async def step():
        # Same reason. This threw every five minutes since it was
        # written, was caught by the loop's handler, and meant no
        # session was ever queued for reflection from here.
        reflect.sweep(runtime.conn)
        # And the redaction model reflection loads, once it is idle:
        # ~740 MB the worker otherwise held all day.
        from agent import redact

        await asyncio.to_thread(redact.release_if_idle)

    await runtime.nap(SWEEP_INTERVAL_S)
    await _forever("reflection sweep", step, SWEEP_INTERVAL_S)


async def spoken_sweep() -> None:
    """Sum up spoken exchanges once they end (agent/worker/journal.py), on a
    timer for the reflection sweep's reason: an exchange ends with the
    absence of turns."""
    from agent.worker import journal

    async def step():
        if made := await journal.sum_up_spoken(runtime.conn):
            log.info("summed up %d spoken exchanges", made)

    await _forever("summing up spoken exchanges", step, SPOKEN_SWEEP_S)


def tell_level(told: tuple[str, float] | None) -> tuple[str, float] | None:
    """One round of `voice_level`: every open window hears the voice's
    level while it listens or speaks (voice/level.py), and once more as it
    stops, so the mark settles. What it told; None while quiet."""
    now = level.now()
    if now is None and told is None:
        return None
    of, loud = now or (None, 0.0)
    for ws in list(runtime.listeners):
        with contextlib.suppress(Exception):
            asyncio.create_task(ws.send_json({"type": "level", "of": of, "level": loud}))
    return now


async def voice_level() -> None:
    """The voice's level to the app, a few times a second while there is one."""
    told: list[tuple[str, float] | None] = [None]

    async def step():
        told[0] = tell_level(told[0])

    await _forever("telling the voice's level", step, LEVEL_EVERY_S)


async def wake_loop() -> None:
    """Keep a wake alarm pointed at whatever is due next.

    Re-armed on a timer rather than when the schedule changes, because the
    schedule changes from several places -- a reminder set in conversation,
    one cancelled over Signal, the night window rolling over -- and a
    re-arm that is merely a couple of minutes late costs nothing. Arming is
    a no-op when the target has not moved, so this is usually free.

    Only with the user's WAKE_TIMER switch on (core/knobs.py): a fresh
    install armed a task in their Task Scheduler that wakes the computer,
    unasked. Off, the task is taken down once, whoever armed it.
    """
    from agent import wake
    from core import knobs

    cleared = False

    def arm_it() -> dict:
        """Its own connection, because this runs off the event loop.

        `wake.arm` shells out to PowerShell to register a scheduled task,
        which is far too slow for the loop -- so it has to be threaded, and
        a threaded call cannot borrow `runtime.conn`.
        """
        from core import db

        nonlocal cleared
        if not knobs.on("WAKE_TIMER"):
            if not cleared:
                wake.disarm()
                cleared = True
            return {"armed": None, "why": "the wake timer is off"}
        cleared = False
        conn = db.open_store()
        try:
            return wake.arm(conn, digest_hour=DIGEST_HOUR)
        finally:
            conn.close()

    await _forever("arming the wake", lambda: asyncio.to_thread(arm_it), WAKE_REARM_S)


async def action_sweep() -> None:
    """Retire cards nobody answered."""
    async def step():
        approvals.expire_stale(runtime.conn)

    await runtime.nap(ACTION_SWEEP_S)
    await _forever("retiring unanswered cards", step, ACTION_SWEEP_S)


# How often routines are looked at. Thirty seconds keeps "at 7pm" honest
# without spinning; a run is minutes of work, queued as a job.
ROUTINE_TICK_S = 30


async def routine_loop() -> None:
    """Queue each routine's run when it comes due, exactly once
    (`routines.claim`), and deliver what quiet hours held back once they end.
    A routine due while the laptop slept runs once when it wakes, not once
    per missed time."""
    await runtime.nap(ROUTINE_TICK_S)
    await _forever("routine tick", routine_tick, ROUTINE_TICK_S)


async def routine_tick(now=None) -> list[int]:
    """One look: queue what is due, and send what quiet hours held once they
    are over. Returns the jobs queued."""
    from agent import jobs, modroutines, routines
    from agent.worker.handlers import routine_text
    from core.call_policy import within_quiet_hours

    queued = []
    for routine in routines.due(runtime.conn, now):
        if routines.claim(runtime.conn, routine, now):
            queued.append(jobs.enqueue(runtime.conn, kind="routine", prompt=str(routine.id),
                                       why=f"routine: {routine.title}"))
    # A module's: the same job, named `<module>.<n>` (agent/modroutines.py).
    for found in modroutines.due(runtime.conn, now):
        if modroutines.claim(found, now):
            queued.append(jobs.enqueue(runtime.conn, kind="routine", prompt=found.id,
                                       why=f"{found.label}'s routine: {found.title}"))
    if not within_quiet_hours(now):
        for routine in routines.held_reports(runtime.conn):
            await links.notify_user(
                routine_text(routine, routine.held or "",
                             finished=routine.status == "ended", met=False),
                noted=f"[routine {routine.id} \"{routine.title}\"] {routine.held}")
            routines.clear_held(runtime.conn, routine.id)
    return queued


# How often goals are looked at. A session is minutes of work, queued as a
# job; nothing about a goal needs the minute.
GOAL_TICK_S = 60


async def goal_loop() -> None:
    """Start a session on each goal that is due, exactly once
    (`goals.claim`), and end those past their deadline."""
    await runtime.nap(GOAL_TICK_S)
    await _forever("goal tick", goal_tick, GOAL_TICK_S)


async def goal_tick(now=None) -> list[int]:
    """One look: end the goals that ran out of time, and -- outside quiet
    hours, when nothing it found could reach them or anyone else before
    morning anyway -- send what goals owe them and queue a session for each
    that is due. Returns the jobs queued."""
    from agent import goals, jobs
    from agent.worker.handlers import tell_goal
    from core.call_policy import within_quiet_hours

    for ended in goals.expire(runtime.conn, now):
        goals.owe(runtime.conn, ended.id, goals.owed_report(
            ended, f"It reached its deadline before it was done ({ended.progress()}). "
                   "Start it again with a later one if it still matters."))
    if within_quiet_hours(now):
        return []
    for goal in goals.owed(runtime.conn):
        await tell_goal(goal, None)
    queued = []
    for goal in goals.due(runtime.conn, now):
        if goals.claim(runtime.conn, goal, now):
            queued.append(jobs.enqueue(runtime.conn, kind="goal", prompt=str(goal.id),
                                       why=f"goal: {goal.title}"))
    return queued


async def mailbox_loop() -> None:
    """Read the agent's own mailbox (agent/mailbox.py) every couple of
    minutes: one search of its account when nothing is new. Off until the
    user chooses its account in the app."""
    from agent import mailbox

    await runtime.nap(mailbox.POLL_S)
    await _forever("mailbox tick", mailbox_tick, mailbox.POLL_S)


async def mailbox_tick() -> list[str]:
    """One look: judge what is new, then act on each. Returns what was done."""
    from agent import mailbox, offloop

    taken = await offloop.with_own_conn(runtime.conn, mailbox.poll)
    done = []
    for intake in taken:
        fresh = mailbox.get(runtime.conn, intake.id) or intake
        done.append(await mailbox.take(runtime.conn, fresh))
    return done


# How often a model switch is looked at during its day's watch.
MODEL_WATCH_S = 900


async def model_watch() -> None:
    """Watch a model the scout switched to, and put it back if the agent is
    doing worse with it (agent/models_switch.py). The putting back is at
    once; telling them waits out quiet hours, since there is nothing for them
    to do -- held in memory, and in `model_switches` if a restart loses it."""
    from agent import models_switch
    from core.call_policy import within_quiet_hours

    held: list[str] = []

    async def step():
        said = models_switch.tick(runtime.conn)
        if said:
            models_switch.apply()
        held.extend(said)
        if held and not within_quiet_hours():
            for text in held:
                await links.notify_user(text)
            held.clear()

    await runtime.nap(MODEL_WATCH_S)
    await _forever("model watch", step, MODEL_WATCH_S)


# How often the browser's sessions are swept: idle ones closed, a dead
# worker's reaped, old screenshots removed (agent/browser/session.py). A
# minute, so a browser idle for `session.IDLE_S` (three) is gone by the fourth
# minute, not the fifth or sixth (8 October 2026 audit: 1.1 GiB held for 11
# minutes by a browser nobody was using).
BROWSER_SWEEP_S = 60


async def browser_sweep() -> None:
    """Close idle browser sessions and clear what a dead worker left. Only
    while the browser is on or a session is open: with it off there is
    nothing to sweep, and no reason to ask Docker every two minutes."""
    import core.browser
    from agent.browser import session as sessions

    async def step():
        if not (sessions.open_sessions() or core.browser.enabled()):
            return
        from agent.browser import live

        await sessions.sweep()
        await live.sweep()
        await asyncio.to_thread(sessions.purge_screenshots)
        await asyncio.to_thread(sessions.purge_quarantine)

    await runtime.nap(BROWSER_SWEEP_S)
    await _forever("sweeping the browser", step, BROWSER_SWEEP_S)


async def reembed() -> None:
    """Embed the memories and facts that have no vector: at once, then
    hourly.

    A memory without one is never recalled, and a fact without one is found
    by name only. `db.open_store` empties every vector when they are not the
    code's model's -- a database from before step 3 holds a local model's
    -- and a fact kept while the embedder could not answer has none. A round
    with nothing to embed is two quick reads."""
    from agent import facts, offloop
    from tools import memory

    async def step():
        for what, fn in (("memories", memory.reembed), ("facts", facts.reembed)):
            try:
                if done := await offloop.with_own_conn(runtime.conn, fn):
                    log.info("embedded %d %s that had no vector", done, what)
            except memory.EmbedderUnavailable as exc:
                log.info("%s left without vectors for now: %s", what, exc)

    await _forever("re-embedding", step, REEMBED_EVERY_S)


async def attachment_sweep() -> None:
    """Delete the photos and voice notes sent to Steve once they are a day
    old (channels/signal.py `KEEP_ATTACHMENTS_S`, Telegram's and the app's
    the same): at once, then hourly. Kept, every one the user ever sent
    would stay in the data folder."""
    from agent.worker import app_photos
    from channels import signal, telegram

    def sweep() -> int:
        return signal.sweep_attachments() + telegram.sweep_attachments() + app_photos.sweep()

    async def step():
        if gone := await asyncio.to_thread(sweep):
            log.info("deleted %d attachments over a day old", gone)

    await _forever("sweeping attachments", step, ATTACHMENT_SWEEP_S)


async def probe_containers() -> None:
    """Whether modules' code can run on this computer (decision D6), asked
    once at each start: Windows' support can change between starts, with
    an update or a policy. The answer is kept where code modules and health
    read it (core/sandbox/probe.py); until it is known, they wait."""
    from core.sandbox import probe

    try:
        found = await asyncio.to_thread(probe.run)
    except Exception as exc:  # noqa: BLE001 - no answer leaves code modules off, as unsupported
        log.warning("could not check whether modules can run here: %s", exc)
        return
    if not found.ok:
        log.warning("modules with code cannot run here: %s", found.reason)


async def build_sandbox() -> None:
    """Build the analysis image if it is missing.

    `run_code` falls back to a bare `python:3.13-slim` when the image with
    pandas and matplotlib in it is absent, and says nothing -- so on a fresh
    machine, or after a `docker prune`, data analysis would fail with
    "no module named pandas" and look like the model's fault. The comment on
    the analysis image said "built on demand" and nothing built it.

    Here rather than on the request path: the build takes minutes, and a turn
    that waits for it has already failed.

    Only once the user has turned RUN_CODE on (core/knobs.py), and looked
    for again until then: a fresh install built a gigabyte of image at its
    first start, for a tool nobody had asked for.
    """
    from core import knobs

    while not knobs.on("RUN_CODE"):
        await runtime.nap(SANDBOX_LOOK_S)
    try:
        if await asyncio.to_thread(code_exec.ensure_analysis_image):
            log.info("built the analysis sandbox image")
    except Exception as exc:  # noqa: BLE001 - no Docker is a normal machine
        log.info("no analysis sandbox: %s", exc)


async def build_browser() -> None:
    """Build the browser's images once the user has turned BROWSER on.

    Until 19 October only `python -m core.browser setup` built them, a
    command a new user never runs: setup's Docker page turned the browser on,
    and its first page read said the sandbox was not built. Built here as the
    analysis image is (`build_sandbox`), and only once the switch is on: the
    first build pulls about 2 GB.
    """
    from core import knobs
    from core.browser import sandbox

    while not knobs.on("BROWSER"):
        await runtime.nap(SANDBOX_LOOK_S)
    try:
        built = await asyncio.to_thread(sandbox.build)
        if built:
            log.info("built the browser images: %s", ", ".join(built))
    except Exception as exc:  # noqa: BLE001 - no Docker is a normal machine
        log.info("no browser sandbox: %s", exc)
