"""Everything exactly one worker may do at a time, and how it changes hands.

A blue-green swap overlaps two workers for up to forty seconds. The
supervisor starts each on standby, retires the old one's duties after the
flip, and only then activates the new one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from agent import (
    runtime,
)
from agent.worker import background, links

log = logging.getLogger("worker")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


# A worker that answers HTTP and does nothing else. For the zero-downtime
# gate tests, which need a real worker process behind a real supervisor and
# had, until this existed, been getting a complete second copy of the agent:
# same database, same credentials, same tokens. It dialled the live
# service the real worker was linked to -- where the newest link wins, so the
# two knocked each other off in turn -- re-armed the real wake alarm from its own schedule,
# and shared the job queue, so it could have been the process that claimed a
# real approved action and was killed halfway through at teardown.
INERT_ENV = "STEVE_INERT"


def inert() -> bool:
    return os.environ.get(INERT_ENV, "").strip().lower() not in ("", "0", "false")


# Set by the supervisor on every worker it starts. A standby worker serves
# HTTP and takes up its duties only when told to (`/_activate`); a worker
# started by hand, with no supervisor to tell it, takes them up at once.
STANDBY_ENV = "STEVE_STANDBY"

# Everything exactly one worker may do at a time: the Signal device, the
# job queue, the microphone, and every background loop.
#
# A blue-green swap runs two workers for up to forty seconds, and both used
# to do all of it. The new one requeued the old one's running job at startup
# and ran it a second time; both held the same links, where the newest
# wins, so they knocked each other off for the whole overlap; both ran
# the reminder, watch and night loops. Now the supervisor starts each worker
# on standby, retires the old one after the flip, and only then activates
# the new one -- so the duties are never held twice.
_duties: dict = {"state": "standby"}
_duty_tasks: list[asyncio.Task] = []


def duty_state() -> str:
    return "inert" if inert() else _duties["state"]


def all_duties() -> tuple:
    """What the worker runs while it is active, in the order it starts them.
    The same for every user: a duty that reads a connector asks for its
    accounts as it runs, so a module switched off changes what it finds,
    not whether it runs (`tests/gate/test_off_means_original.py`)."""
    return (
        links.warm_voice,
        links.keep_signal,
        links.start_telegram,
        background.heartbeat,
        background.probe_containers,
        background.build_sandbox,
        background.build_browser,
        background.reembed,
        background.attachment_sweep,
        background.reflection_sweep,
        background.spoken_sweep,
        background.voice_level,
        background.night_loop,
        background.attention_loop,
        background.outbox_loop,
        background.briefing_loop,
        background.reminder_loop,
        background.action_sweep,
        background.browser_sweep,
        background.model_watch,
        background.routine_loop,
        background.goal_loop,
        background.mailbox_loop,
        background.resend_loop,
        background.wake_loop,
        background.account_health_loop,
        background.server_idle_loop,
    )


def take_up_duties() -> None:
    if _duties["state"] != "standby":
        return
    _duties["state"] = "active"
    runtime.jobs.start()
    # Fire and forget: the worker must report healthy long before speech
    # is ready, or a blue-green swap would time out waiting for the mic.
    for duty in all_duties():
        _duty_tasks.append(asyncio.create_task(duty()))
    log.info("took up duties")
    # The worker before this one may have died; the supervisor left a note.
    from agent import issues

    if crashed := issues.record_deaths(runtime.conn):
        log.warning("put %d worker death(s) in the failure ledger", crashed)
    # A module build runs in a thread of the worker that started it, and
    # ended with it; its draft waits for "Allow this build".
    from agent import modbuild

    if stopped := modbuild.recover(runtime.conn):
        log.warning("%d module build(s) stopped with the worker before this one", stopped)


async def give_up_duties(becoming: str) -> None:
    """Hand everything back. A job already running finishes; nothing new
    is claimed, and the Signal device is free at once for
    the worker taking over."""
    if _duties["state"] != "active":
        _duties["state"] = becoming
        return
    _duties["state"] = becoming
    for task in _duty_tasks:
        task.cancel()
    _duty_tasks.clear()
    if runtime.jobs:
        runtime.jobs.stop_claiming()
    if runtime.signal:
        with contextlib.suppress(Exception):
            await runtime.signal.stop()
    # Before the worker taking over polls: Telegram answers two pollers of
    # one bot with a conflict, and takes the newer.
    if runtime.telegram:
        with contextlib.suppress(Exception):
            await runtime.telegram.stop()
    # The microphone before the voice it feeds: the worker taking over opens it.
    from agent.worker import desk

    with contextlib.suppress(Exception):
        desk.stop(reason="swap")
    with contextlib.suppress(Exception):
        desk._stop_tap()  # a call being noted: the new worker picks it up
    if runtime.voice:
        with contextlib.suppress(Exception):
            runtime.voice.close()
    # The local MCP servers it started: the worker taking over starts its
    # own on need, and two would write one account's sign-in at once.
    from core.sandbox import servers

    with contextlib.suppress(Exception):
        await asyncio.to_thread(servers.POOL.stop_all)
    # The app's sockets with no turn under way: closed, so it reconnects to
    # the worker taking over now rather than at the end of the drain. One
    # mid-turn is closed once its turn is answered (routes.ws_session).
    from agent.worker.routes import SERVICE_RESTART

    for ws in [w for w in runtime.listeners if w not in runtime.turning]:
        with contextlib.suppress(Exception):
            await ws.close(code=SERVICE_RESTART)
    log.info("gave up duties (%s)", becoming)


async def activate(_: Request) -> JSONResponse:
    """The supervisor handing this worker the duties. Idempotent."""
    if inert():
        return JSONResponse({"ok": True, "duties": "inert"})
    if _duties["state"] == "retired":
        return JSONResponse(
            {"ok": False, "duties": "retired",
             "error": "a retired worker does not come back; start another"},
            status_code=409,
        )
    take_up_duties()
    return JSONResponse({"ok": True, "duties": _duties["state"]})


async def retire(_: Request) -> JSONResponse:
    """The supervisor taking them back, because a replacement is serving."""
    await give_up_duties("retired")
    return JSONResponse({"ok": True, "duties": duty_state()})
