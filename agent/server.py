"""The worker process: the agent, behind HTTP and a websocket.

The supervisor owns the public port and proxies here, so this process can be
replaced mid-conversation. Three parts of that contract matter and are easy to
break by accident:

    GET  /health     must answer fast and always, or a swap will never flip
    POST /_shutdown  must drain rather than exit, or a swap kills a live turn
    POST /_activate  must be the only thing that starts the duties

This file is only the entry point: it builds the app, wires the worker's
pieces together in `lifespan`, and runs uvicorn. The pieces are in
`agent/worker/` -- `answering` (a message becomes an answer), `links`
(Signal, voice), `background` (the loops), `handlers` (queued jobs),
`duties` (what one worker holds at a time), `routes` (the handlers below) --
and the live state they share is in `agent/runtime.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import faulthandler
import functools
import logging
import os
import socket
import sys

# The worker runs as `python -m agent.server`, which makes this module
# `__main__`, and anything doing `from agent import server` would import a
# *second, fresh* copy whose state is all None -- which is how a tool once
# reported a link offline while it was up. Nothing
# needs to import this file any more (the shared state is in
# `agent/runtime.py`), but the alias stays so that nothing can make that
# mistake again: one name for one module, registered before anything else.
if __name__ == "__main__":
    sys.modules.setdefault("agent.server", sys.modules[__name__])

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route, WebSocketRoute

from agent import (
    approvals,
    jobs,
    models,
    provider,
    runtime,
    tracing,
)
from agent.actions import perform
from agent.browser import live as browser_live
from agent.worker import accounts as account_routes
from agent.worker import (
    app_photos,
    approvers,
    control,
    duties,
    handlers,
    journal,
    knows,
    links,
    needs,
    pairing,
    places,
    readers,
    routes,
    setup,
    signin,
)
from core import db, identity, localapi, localonly, logscrub, modules

log = logging.getLogger("worker")


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    # Before any Agent is built: instrument_all only affects what comes after.
    tracing.setup()
    # The model scout's switches, each re-checked by the frozen guard; a
    # worker swap is what applies a new one.
    if refused := models.load_active():
        log.warning("model switches refused, those roles as written: %s", refused)
    provider.reset_clients()
    # The one open that refuses a database newer than this code: a worker
    # starting must not write to a schema it does not know. (Every other
    # open allows it, for the worker being drained in a swap.)
    runtime.conn = db.open_store(refuse_newer=True)
    # Every built-in module gets its registry row, on, the first time this
    # install sees it: the switch the app turns it off with.
    modules.register(runtime.conn)
    # The knobs the user set in the desktop app's control panel, inside the bounds
    # frozen in core/knobs.py, over the code's defaults.
    try:
        control.apply_knobs()
    except Exception:  # noqa: BLE001 - a bad knob file leaves the defaults, never a dead worker
        log.exception("could not apply the control panel's knobs; defaults stand")

    if duties.inert():
        log.warning("%s is set: serving HTTP only, with no jobs, loops, "
                    "or Signal", duties.INERT_ENV)
        try:
            yield
        finally:
            _close_store()
        return

    runtime.jobs = jobs.JobWorker(
        runtime.conn,
        handlers={
            "research": handlers.handle_research,
            "reflect": handlers.handle_reflect,
            "action": handlers.handle_action,
            "routine": handlers.handle_routine,
            "goal": handlers.handle_goal,
            "mail": handlers.handle_mail,
        },
        on_finish=handlers.announce,
        # The app's working line: "Working on: …" while a task runs.
        on_start=journal.job_started,
        on_stop=journal.job_stopped,
    )
    # The queue knows how to ask and how to run, but not through what. Both
    # are handed to it here so `agent/approvals.py` imports neither Signal nor
    # the toolset.
    approvals.set_notifier(functools.partial(links.notify_user, kind="card"))
    approvals.set_executor(perform)
    approvals.set_answers_from(links.answers_from)
    if not os.environ.get(duties.STANDBY_ENV):
        duties.take_up_duties()
    try:
        yield
    finally:
        # Its browser sessions go with it; one it could not close is the
        # next worker's to reap (agent/browser/session.py).
        with contextlib.suppress(Exception):
            from agent.browser import session as browser_sessions

            await browser_sessions.close_all()
            await browser_live.close()
        with contextlib.suppress(Exception):
            await duties.give_up_duties("stopped")
        if runtime.jobs:
            with contextlib.suppress(Exception):
                await runtime.jobs.stop()
        _close_store()


def _close_store() -> None:
    """The worker's connection, closed and forgotten. Left set, it stayed
    the one `agent/toolset/clients.commitment_store` hands out, so whatever
    ran next in the process was given a closed database: in the tests, the
    next test in that runner, where the golden record's draft failed with
    "Cannot operate on a closed database" one run in several."""
    if runtime.conn:
        runtime.conn.close()
    runtime.conn = None


app = Starlette(
    lifespan=lifespan,
    # The worker listens on its own localhost port as well as behind the
    # supervisor, so a page or a program that found the port would otherwise
    # skip the supervisor's check entirely. See core/localonly.py.
    middleware=[Middleware(localonly.LocalOnly)],
    routes=[
        Route("/health", routes.health),
        Route("/_shutdown", routes.shutdown, methods=["POST"]),
        Route("/_activate", duties.activate, methods=["POST"]),
        Route("/_retire", duties.retire, methods=["POST"]),
        Route("/chat", routes.chat, methods=["POST"]),
        Route("/status", routes.status),
        Route("/voice/status", routes.voice_status),
        Route("/voice/listen", routes.voice_listen, methods=["POST"]),
        Route("/voice/tap", routes.voice_tap, methods=["POST"]),
        Route("/attention/status", routes.attention_status),
        Route("/reminders", routes.reminder_status),
        Route("/actions/decide", routes.action_decide, methods=["POST"]),
        Route("/approvals/setup", approvers.setup),
        Route("/approvals/passphrase", approvers.choose_passphrase, methods=["POST"]),
        Route("/approvals/passphrase/reset", approvers.reset_passphrase, methods=["POST"]),
        Route("/approvals/passphrase/reset/windows", approvers.reset_passphrase_by_windows,
              methods=["POST"]),
        Route("/approvals/authenticator", approvers.start_authenticator, methods=["POST"]),
        Route("/approvals/authenticator/confirm", approvers.confirm_authenticator,
              methods=["POST"]),
        Route("/approvals/hello", approvers.register_hello, methods=["POST"]),
        Route("/approvals/hello/forget", approvers.forget_hello, methods=["POST"]),
        *signin.ROUTES,
        Route("/signal/status", routes.signal_status),
        Route("/signal/install", pairing.signal_install, methods=["POST"]),
        Route("/signal/link", pairing.signal_link, methods=["POST"]),
        Route("/signal/unlink", pairing.signal_unlink, methods=["POST"]),
        Route("/telegram/status", routes.telegram_status),
        Route("/telegram/token", pairing.telegram_token, methods=["POST"]),
        Route("/telegram/pair", pairing.telegram_pair, methods=["POST"]),
        Route("/telegram/unpair", pairing.telegram_unpair, methods=["POST"]),
        Route("/jobs", routes.list_jobs),
        WebSocketRoute("/ws", routes.ws_session),
        # Their live view of a sandbox browser, for logging in (agent/browser/live.py).
        *browser_live.ROUTES,
        # The desktop app's control panel (agent/worker/control.py).
        *control.ROUTES,
        # Its modules and accounts, which only the app changes (agent/worker/accounts.py).
        *account_routes.ROUTES,
        # Setup's pages (agent/worker/setup.py).
        *setup.ROUTES,
        # The journal's day page (agent/worker/journal.py).
        *journal.ROUTES,
        # Photos given in the app, and shown on their messages (agent/worker/app_photos.py).
        *app_photos.ROUTES,
        # The sidebar's places: Coming up, Extensions, Call notes (agent/worker/places.py).
        *places.ROUTES,
        # What needs the user, as notes (agent/worker/needs.py).
        *needs.ROUTES,
        # What Steve knows, to read, correct and forget (agent/worker/knows.py).
        *knows.ROUTES,
        # The local readers, installed with a click (agent/worker/readers.py).
        *readers.ROUTES,
    ],
)


# Libraries that narrate every request at INFO. Their warnings still show.
# httpx2 too, which is what the project's clients are: its INFO line per
# request was a third of every worker log (641 of 2,000 lines in 13 hours on
# 5 October 2026) and carried their web searches, `?q=...`, onto disk.
QUIET_LOGGERS = ("httpx", "httpx2", "httpcore", "openai", "urllib3", "websockets",
                 "asyncio")


def _configure_logging() -> None:
    """Make the worker's own log lines reach its log file.

    Nothing configured logging here, so Python's last-resort handler printed
    WARNING and above with no timestamp and dropped everything else: every
    worker log was empty or nearly, and "took up duties", "requeued 1 job"
    and "night finished" had never once been recorded. And stdout to a file
    is block-buffered, so a worker that died lost its last lines -- the very
    ones the supervisor copies into its account of the death.
    """
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    logscrub.install()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Except the OpenAI SDK's retries, its only INFO lines: a request that
    # stalled for five minutes and was retried left no trace in the log.
    logging.getLogger("openai._base_client").setLevel(logging.INFO)
    # A native crash -- an access violation or heap corruption in a C
    # extension -- ends the process with no Python traceback at all. Three
    # workers died that way between 24 and 29 September (Windows logged
    # python.exe faulting in ntdll.dll), and each log simply stopped. With
    # this, the Python stack of every thread goes to the log (stderr is the
    # same file) as it dies, which names the library and the call.
    faulthandler.enable(file=sys.stderr, all_threads=True)


def main() -> None:
    # The supervisor scrubs its own; a worker started any other way, by
    # hand or by a test, does the same before it starts anything.
    identity.scrub_environment()
    parser = argparse.ArgumentParser(description="Agent worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log-level", default="warning")
    # The supervisor's way (core/worker_handle.spawn_worker): its socket on
    # that port, bound and listening before this process started, shared on
    # stdin. Without it, as by hand or in a test, the worker binds the port.
    parser.add_argument("--socket-on-stdin", action="store_true")
    args = parser.parse_args()
    _configure_logging()
    # The token every request must carry: this start's, which the supervisor
    # published before it started this worker (core/localapi.py).
    if not localapi.accept_published():
        log.error("no run file at %s: every request will be refused", localapi.run_file())

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level)
    runtime.http = uvicorn.Server(config)
    if not args.socket_on_stdin:
        runtime.http.run()
        return
    sock = socket.fromshare(sys.stdin.buffer.read())
    if sock.getsockname()[1] != args.port:
        sys.exit(f"the socket shared on stdin is on port {sock.getsockname()[1]}, "
                 f"not {args.port}")
    runtime.http.run(sockets=[sock])


if __name__ == "__main__":
    main()
