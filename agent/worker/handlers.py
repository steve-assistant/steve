"""What each kind of queued job does when the job worker claims it, and how a
finished one is announced.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from agent import (
    approvals,
    jobs,
    offloop,
    reflect,
    runtime,
    workers,
)
from agent.loop import UNATTENDED, Conversation
from agent.worker import links
from core import spend

log = logging.getLogger("worker")


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------


async def handle_research(job: jobs.Job) -> str:
    """Run a deferred task in its own conversation -- as a lead that may
    start workers on its parts, when Jev thinks it splits (agent/workers.py).
    Any worker still running when the lead answers is stopped."""
    started = time.perf_counter()
    split = await workers.worth_splitting(job.prompt)
    # What the critic judges the task's actions against: their request as the
    # task was written from it, or -- one the agent set itself -- said so.
    theirs = asked_by_user(job)
    mandate = (f"A background task from the user's conversation: {job.prompt}" if theirs
               else f"The agent's own background task, not the user's words: {job.prompt}")
    # Browsing workers, with `browser.background` on, offered whether or not
    # the task splits: one page that needs a real browser is one part. From
    # 1 October 2026 only a task the user asked for had them (the user). Since 11
    # October the doer and the night's research do, in their own turns
    # (`workers.send_browsing`), and so does a task the agent set itself
    # (the user, that evening) -- on their terms: `user_urls` gives it none of
    # their words, so a worker starts only from what its own searches return.
    import core.browser

    browsing = core.browser.background()
    note = "".join(filter(None, [workers.lead_note(split) if split.offer else None,
                                 workers.LEAD_BROWSE_NOTE if browsing else None])) or None
    convo = Conversation(runtime.conn, channel="job", note=note, mandate=mandate)
    convo.start = UNATTENDED
    seen = user_urls(job)
    crew = (workers.Crew(runtime.conn, convo.session_id, job_id=job.id, seen=seen,
                         mandate=mandate, split=split.offer, browsing=browsing)
            if split.offer or browsing else None)
    try:
        reply = await convo.send(job.prompt, crew=crew, seen=seen)
    finally:
        if crew is not None:
            await crew.close()
        workers.record(runtime.conn, job.id, split, crew, time.perf_counter() - started)
    convo.end()
    return reply.text


# How far back their own words count for the pages a job the user asked for may read.
USER_WORDS_H = 24


def user_urls(job: jobs.Job) -> set[str]:
    """The URLs the user wrote in the conversation a job came from, in the last
    day: what it may read besides what it searches up (`core/egress.unseen`).

    From their messages, never from the job's own text: that was written by a
    model, which may have just read a page telling it to queue a fetch of
    `attacker.example/?d=...`. A job with no conversation of theirs -- the
    agent's own -- has none."""
    from core import egress

    if job.session_id is None or not asked_by_user(job):
        return set()
    rows = runtime.conn.execute(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'user'"
        " AND created_at >= datetime('now', ?)", (job.session_id, f"-{USER_WORDS_H} hours"),
    ).fetchall()
    return set().union(*(egress.urls_in(r[0] or "") for r in rows)) if rows else set()


def asked_by_user(job: jobs.Job) -> bool:
    """Whether the job came out of a conversation the user was having.

    A job with no session is the agent's own. So is one queued from a
    session nobody was talking in -- the night's research, a sweep, a
    background job that queued another.
    """
    from core.call_policy import is_autonomous

    # By allowlist: a background job ("job") once counted as the user here,
    # so research the night queued for itself could file a change past the
    # self-upgrade pause. See `core.call_policy.PRESENT_CHANNELS`.
    return not is_autonomous(_channel(job))


def _channel(job: jobs.Job) -> str | None:
    """The channel of the conversation the job was asked from; None for the
    agent's own."""
    if job.session_id is None:
        return None
    row = runtime.conn.execute(
        "SELECT channel FROM sessions WHERE id = ?", (job.session_id,)
    ).fetchone()
    return row["channel"] if row else None


async def handle_reflect(job: jobs.Job) -> str:
    """Score a finished session. The prompt is the session id."""
    result = await reflect.reflect(runtime.conn, int(job.prompt))
    if result is None:
        return "nothing to judge"
    return json.dumps(
        {
            "scores": result.scores,
            "redacted_entities": result.redacted_entities,
            "lesson": result.lesson_path,
            "proposal_issue": result.proposal_issue_id,
        }
    )


# Housekeeping the agent does to itself. Announcing these would mean a
# notification every time a conversation went quiet, for work the user never
# asked for and cannot act on.
#
# `action` reports itself, in words, from `handle_action`. Letting the
# generic announcer have it too would follow "Sent to Clara" with
# "Background task 41 finished." about the same email. `routine` likewise,
# from `handle_routine` -- and only when something changed. `goal` likewise,
# from `handle_goal`.
SILENT_KINDS = {"reflect", "action", "routine", "goal"}


async def handle_mail(job: jobs.Job) -> str:
    """One mail to the agent's own mailbox worked on (agent/mailbox.py): their
    instruction, or a reply in a thread the user brought it into. A conversation of
    its own on the `mailbox` channel -- unattended, so pages only from their
    note, outward only by card -- with their note as the instruction and the
    mail as material, marked as such and screened. What it says back is the
    job's result, announced to them like any task the user asked for."""
    from agent import mailbox
    from agent.connectors import NoAccount
    from steve_sdk.errors import ConnectorError

    intake = mailbox.get(runtime.conn, int(json.loads(job.prompt)["intake"]))
    if intake is None:
        return "That mail is no longer in the mailbox's record."
    try:
        message = await offloop.with_own_conn(runtime.conn, mailbox.read, intake.source)
    except (NoAccount, ConnectorError) as exc:
        return f"Could not read the mail: {exc}"
    asked = await mailbox.prompt(runtime.conn, intake, message)  # the material screened
    alias = await offloop.with_own_conn(runtime.conn, mailbox.address)
    convo = Conversation(runtime.conn, channel=mailbox.CHANNEL,
                         note=mailbox.SESSION_NOTE.format(alias=alias),
                         mandate=mailbox.mandate(runtime.conn, intake))
    convo.start = UNATTENDED
    try:
        reply = await convo.send(asked, allow=mailbox.toolset(), seen=mailbox.seen_urls(intake))
    finally:
        convo.end()
    return reply.text


async def handle_routine(job: jobs.Job) -> str:
    """One run of a routine the user set up (agent/routines.py), in a conversation
    of its own on the `routine` channel: unattended, so it reads only URLs it
    was shown or that are in its instruction, and its actions pass
    `core/monitor.routine_gate`. The user hears only what changed -- now, or after
    quiet hours for an interval watch that is not urgent."""
    from agent import routines
    from core import egress

    if not job.prompt.isdigit():
        return await _module_routine(job)
    routine = routines.get(runtime.conn, int(job.prompt))
    if routine is None or routine.status != "active":
        return json.dumps({"ran": False, "why": "not active"})
    # In the routines' unit already (jobs.SPENDS_AS). Held back quietly: the
    # next briefing says the cap was reached (agent/caps.py).
    if spend.may_spend(runtime.conn):
        return json.dumps({"ran": False, "why": "budget"})
    convo = Conversation(runtime.conn, channel="routine", note=routines.run_note(routine),
                         routine_id=routine.id, mandate=routines.mandate(routine))
    convo.start = UNATTENDED
    try:
        reply = await convo.send(routines.prompt(routine), allow=routines.toolset(routine),
                                 seen=set(egress.urls_in(routine.instruction)))
    finally:
        convo.end()
    report, met = routines.understood(routine, reply.text)
    held = report if report and routines.keeps_quiet(routine) else None
    finished = routines.record_run(runtime.conn, routine, report, met=met, held=held)
    if report and not held:
        await links.notify_user(
            routine_text(routine, report, finished=finished, met=met),
            noted=f"[routine {routine.id} \"{routine.title}\"] {report}")
    return json.dumps({"ran": True, "reported": bool(report), "held": bool(held),
                       "met": met, "finished": finished, "cost_usd": round(reply.cost_usd, 5)})


async def _module_routine(job: jobs.Job) -> str:
    """One run of a module's routine (agent/modroutines.py): a user's
    routine's run, with the module named, no envelope, and each action a
    card (`core/monitor.module_gate`)."""
    from agent import modroutines
    from core import egress

    routine = modroutines.get(runtime.conn, job.prompt)
    if routine is None:
        return json.dumps({"ran": False, "why": "its module is off or has no such routine"})
    if spend.may_spend(runtime.conn):
        return json.dumps({"ran": False, "why": "budget"})
    last = modroutines.last_report(routine)
    convo = Conversation(runtime.conn, channel="routine",
                         note=modroutines.run_note(routine, last),
                         module_routine=routine.id, mandate=modroutines.mandate(routine))
    convo.start = UNATTENDED
    try:
        reply = await convo.send(routine.instruction,
                                 allow=modroutines.toolset(runtime.conn, routine),
                                 seen=set(egress.urls_in(routine.instruction)))
    finally:
        convo.end()
    report = modroutines.understood(reply.text, last)
    modroutines.record_run(routine, report)
    if report:
        await links.notify_user(
            f"{routine.label}, {routine.title}: {report}",
            noted=f"[module routine {routine.id} \"{routine.title}\"] {report}")
    return json.dumps({"ran": True, "reported": bool(report),
                       "cost_usd": round(reply.cost_usd, 5)})


def routine_text(routine, report: str, *, finished: bool, met: bool) -> str:
    """A run's report as the user reads it: which routine, what it found, and
    whether that was its last."""
    tail = ""
    if met:
        tail = "\n\n(Watch over: that was what it was waiting for.)"
    elif finished:
        tail = "\n\n(That was its last run.)"
    return f"{routine.title}: {report}{tail}"


async def handle_goal(job: jobs.Job) -> str:
    """One work session on a goal the user set (agent/goals.py), in a conversation
    of its own on the `goal` channel: unattended, so it reads only URLs the user
    wrote, and its actions pass `core/monitor.goal_gate`. It changes the goal
    through `goal_update`; its reply reaches them only when it says something
    new, with any question it asked, once."""
    from agent import goals

    goal = goals.get(runtime.conn, int(job.prompt))
    if goal is None or goal.status != "active":
        return json.dumps({"ran": False, "why": "not active"})
    if goal.spent_usd >= goal.budget_usd:
        goals.pause(runtime.conn, goal.id)
        goals.release(runtime.conn, goal.id)
        await links.notify_user(
            f'Paused the goal "{goal.title}": its ${goal.budget_usd:.2f} is spent '
            f"({goal.progress()}). Say if it should carry on with more.",
            noted=f'[goal {goal.id} "{goal.title}" paused: budget spent]')
        return json.dumps({"ran": False, "why": "goal budget"})
    if spend.may_spend(runtime.conn):
        # Not a session: free it to run again on its cadence, tomorrow. The
        # next briefing says the cap was reached (agent/caps.py).
        goals.release(runtime.conn, goal.id)
        return json.dumps({"ran": False, "why": "day budget"})
    from agent import features

    on = await features.now(runtime.conn)
    convo = Conversation(runtime.conn, channel=goals.CHANNEL,
                         note=goals.session_note(runtime.conn, goal, on), goal_id=goal.id,
                         mandate=goals.mandate(goal))
    convo.start = UNATTENDED
    cost = 0.0
    report = None
    try:
        reply = await convo.send(goals.prompt(goal), allow=goals.toolset(goal),
                                 seen=goals.user_urls(goal))
        cost = reply.cost_usd
        report = goals.understood(goal, reply.text)
    finally:
        convo.end()
        # Whatever happened, the session is over: what it did is recorded,
        # and the goal may run again.
        goal = goals.after_session(runtime.conn, goal.id, cost_usd=cost,
                                   session_id=convo.session_id, report=report)
    asked = len(goals.untold(goal)) if goal is not None else 0
    if goal is not None:
        await tell_goal(goal, report)
    return json.dumps({"ran": True, "reported": bool(report), "asked": asked,
                       "status": goal.status if goal else None, "cost_usd": round(cost, 5)})


async def tell_goal(goal, report: str | None) -> None:
    """Tell them what a goal has to say: this report, any earlier one the
    goal loop held through quiet hours (`goals.owe`), its untold questions,
    its end. The first live goal's "done", 4 October 2026, was dropped by a
    link down for six seconds, and nothing tried again; the outbox does now
    (agent/outbox.py)."""
    from agent import goals

    report = goals.owed_report(goal, report)
    asked = goals.untold(goal)
    said = goals.message(goal, report, asked)
    if said is not None:
        text, noted = said
        await links.notify_user(text, noted=noted)
        goals.mark_told(runtime.conn, goal.id, [q["n"] for q in asked])
    goals.settle(runtime.conn, goal.id)


# ---------------------------------------------------------------------------
# Actions waiting on a yes
# ---------------------------------------------------------------------------

# Outcomes the user has already been told about in the same breath they caused them.
# "UNDO MAPLE" is answered on the spot; repeating it ten seconds later as a
# failure notice reads as the system not having heard them the first time.
QUIET_ACTION_ERRORS = {"cancelled during the hold", "not approved, or already running"}


async def handle_action(job: jobs.Job) -> str:
    """Carry out something the user approved. The prompt is the action id.

    Queued rather than run wherever the yes arrived, because a yes turns up on
    whatever the user happens to be holding -- an HTTP handler, a Signal callback --
    and neither should sit through a thirty-second hold and a Gmail round trip
    before it can answer them.
    """
    action_id = int(job.prompt)
    result = await approvals.execute(runtime.conn, action_id)
    action = approvals.get(runtime.conn, action_id)
    with contextlib.suppress(Exception):
        # A goal that asked for this moves on to its next step soon, rather
        # than at its next check (agent/goals.py).
        from agent import goals

        goals.woken_by_action(runtime.conn, action_id)
    headline = action.preview.splitlines()[0] if action else f"action {action_id}"

    if result.get("ok"):
        # An executor with something to say says it: their YES to "still want
        # this?" ran the task, and its answer is the news, not "Done".
        said = str((result.get("result") or {}).get("say") or "") or f"Done — {headline}"
        if action is not None and action.tool == "browse_submit":
            # Not the card's first line ("Submit a form on ...:"), which said
            # nothing of how it went (1 October, their first browser submit).
            from agent.toolset.browser import done_text

            said = done_text(action.preview, result.get("result") or {})
        await links.notify_user(said, kind="reply")
    elif result.get("error") not in QUIET_ACTION_ERRORS:
        await links.notify_user(f"Couldn't do it — {headline}\n{result.get('error')}",
                                kind="reply")
    return json.dumps(result)


def _unannounced(job: jobs.Job) -> bool:
    """Finished work the user is never told about as a notification: housekeeping,
    and anything they did not ask for in a conversation they were having.

    By who asked, not by kind. Filtering by kind let two things through on
    25 September. The night's research had queued background jobs, and each
    finished one went to their phone at 3am, mostly reading "Queued a
    background research task". And the resend sweep sent the night's own
    report row and an attention sweep's row, as raw JSON. What the agent did
    on its own is the morning briefing's to tell.
    """
    # Mail to the agent's mailbox is their asking, by email: no conversation
    # session to show it, and the user hears the answer like any task's.
    if job.kind == "mail":
        return False
    return job.kind in SILENT_KINDS or not asked_by_user(job)


def _phone_text(job: jobs.Job) -> str:
    body = (job.result or "").strip() if job.status == "done" else (job.error or "")
    if job.kind == "mail":
        about = (job.why or "your mail").strip()
        return (f"From your agent's mailbox ({about}):\n\n{body}" if job.status == "done"
                else f"Your agent's mailbox could not finish {about}: {body}")
    if job.status == "done":
        return f"Background task {job.id} finished.\n\n{body}"
    return f"Background task {job.id} failed: {body}"


# How far back a result never put in the outbox is still worth telling. A
# day covers a night asleep; older is news nobody waits for.
RESEND_WITHIN_H = 24

# Jobs whose message is being written, so the sweep does not write it twice.
_sending: set[int] = set()


async def _to_outbox(job: jobs.Job) -> bool:
    """The result to the outbox, and the job marked told once it is there.

    It used to be marked told as soon as a send to their phone was
    *started* -- and with Signal refusing, the send failed quietly and a
    background result the user had asked for was never delivered or
    retried. Then it was marked only once Signal took it, and a sweep tried
    again every five minutes. The outbox shows it in the app at once and
    retries the phone itself (agent/outbox.py).
    """
    if job.id in _sending:
        return False
    _sending.add(job.id)
    try:
        await links.notify_user(_phone_text(job), kind="job")
    except Exception:  # noqa: BLE001 - left unmarked, so the sweep writes it later
        log.warning("job %s's result could not be written to the outbox", job.id,
                    exc_info=True)
        return False
    finally:
        _sending.discard(job.id)
    jobs.mark_notified(runtime.conn, [job.id])
    return True


async def resend_unannounced() -> int:
    """Tell what finished in the last day and was never put in the outbox:
    the worker stopped between finishing it and `announce`, or the outbox
    could not be written. Returns how many it wrote."""
    told = 0
    for job in jobs.pending_notifications(runtime.conn, within_hours=RESEND_WITHIN_H):
        if _unannounced(job):
            jobs.mark_notified(runtime.conn, [job.id])
            continue
        told += await _to_outbox(job)
    return told


# The channels of someone at the laptop: the app's and the desk's voice.
# `core.call_policy.PRESENT_CHANNELS` counts the phone too, rightly for who
# asked, not for where to speak.
AT_THE_DESK = frozenset({"tray", "voice"})


def announce(job: jobs.Job | None) -> None:
    """Push a finished job to every listener, and its result to the outbox,
    which shows it in the app and sends it to their phone; the desk voice
    is best effort."""
    if job is None:
        return
    if _unannounced(job):
        jobs.mark_notified(runtime.conn, [job.id])
        return

    # Not an early return on `not runtime.listeners`: with the tray closed and
    # only Signal open, that skipped the phone as well.
    payload = {"type": "job_finished", "job": job.as_dict()}
    for ws in list(runtime.listeners):
        with contextlib.suppress(Exception):
            asyncio.create_task(ws.send_json(payload))

    # The phone is the one place the user actually is when a background job
    # lands, so this carries the result rather than just the headline.
    with contextlib.suppress(Exception):
        asyncio.create_task(_to_outbox(job))

    # Say it out loud too, but only the headline: a research result can be
    # pages long, and reading all of it at someone is not a notification.
    # Only a job asked at the desk, and never in quiet hours: the desk's
    # voice stays loaded after the first press, and a job asked from the
    # phone at night was said through the laptop's speakers to whoever was
    # in the room.
    from core import call_policy

    if (runtime.voice is not None and runtime.voice.speak
            and (_channel(job) or "").strip().lower() in AT_THE_DESK
            and not call_policy.within_quiet_hours()):
        headline = (
            f"Background task {job.id} finished."
            if job.status == "done"
            else f"Background task {job.id} failed."
        )
        with contextlib.suppress(Exception):
            asyncio.create_task(runtime.voice.say(headline))
