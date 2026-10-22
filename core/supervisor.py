"""The supervisor: the one process the agent is never allowed to modify.

It owns the port the app talks to, and proxies every request to whichever
worker is currently active. The port is the system's choice at each start,
named with this start's token in the run file (core/localapi.py), and no
request reaches anything here without that token (core/localonly.py).

The proxy's indirection is what makes "keep working while a worker is replaced"
possible: a new worker is started and health checked on a private port, traffic
is flipped atomically, and only then is the old worker drained and retired. If
the new worker never becomes healthy, nothing is flipped and the old one never
learns anything happened.

It knows nothing of git. It used to: a merged self-upgrade was swapped in,
watched for a day by the canary and put back if Steve answered worse, and a
HEAD that would not start three times running was rolled back to the last
commit that ran. An installed Steve has no checkout to move, and a merge
that went live with nothing watching it would be worse than no merge, so
self-upgrade no longer merges (agent/upgrade.py) and the supervisor runs
the code it was started with. A worker is named by the order it started
in and the time it started, not by a commit.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from core import identity, localapi, localonly, logscrub, paths
from core.policy import load_policy, verify_lock
from core.worker_handle import WorkerHandle, listening, reap_orphans, spawn_worker

log = logging.getLogger("supervisor")


def _frozen_core_violations(policy) -> list[str]:
    """The frozen files that differ from core/policy.lock, in a checkout.

    An install skips it: the lock is a developer's tool for the frozen core
    of a working tree, and an install's files are checked against its
    signed manifest instead (the app, at start). Walked there too, it hashed
    the shipped tree on every status call to report what the manifest
    already covers."""
    return [] if paths.is_installed() else verify_lock(policy)

# Origins allowed to talk to the agent from a browser context: the tray's
# own, and never "*". Defined with the check that enforces them -- see
# core/localonly.py for why CORS alone never protected anything here.
ALLOWED_ORIGINS = sorted(localonly.ALLOWED_ORIGINS)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",  # the field's name; RFC 2616's list misspelled it "trailers"
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


@dataclass
class SupervisorState:
    active: WorkerHandle | None = None
    draining: list[WorkerHandle] = field(default_factory=list)
    swap_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    history: list[str] = field(default_factory=list)  # workers served, newest last
    started: int = 0  # workers started since this supervisor began, for their names
    client: httpx.AsyncClient | None = None
    backend: localapi.Backend | None = None  # this start's port and token, once published
    server: uvicorn.Server | None = None  # what serves this app, which `sup_stop` ends


state = SupervisorState()


# ---------------------------------------------------------------------------
# Swap
# ---------------------------------------------------------------------------


# How long a retired worker may go on finishing what it has. A turn under
# way is pinned to it, and a research turn takes minutes (270 s on 6
# October 2026); at 30 s it was killed mid-answer. The idle sockets that
# used to hold every drain to the full timeout are closed by the worker as
# it hands over (agent/worker/routes.ws_session), so a quiet worker still
# retires in about a second -- this bounds only real work.
DRAIN_TIMEOUT_S = 300.0


async def _retire(worker: WorkerHandle, drain_timeout: float) -> None:
    """Let in-flight work finish, then stop the process."""
    drained = await worker.drain(timeout=drain_timeout)
    if not drained:
        log.warning(
            "worker :%d still had %d in-flight at drain timeout", worker.port, worker.inflight
        )
    await worker.shutdown()
    with contextlib.suppress(ValueError):
        state.draining.remove(worker)
    log.info("retired worker :%d (%s)", worker.port, worker.name)


async def swap(drain_timeout: float = DRAIN_TIMEOUT_S) -> dict:
    """Blue-green replace the active worker. Never leaves us without one."""
    async with state.swap_lock:
        old = state.active
        state.started += 1
        new = spawn_worker(f"w{state.started}-{time.strftime('%H%M%S')}")
        if not await new.wait_healthy():
            # The new code is broken. Tear it down and leave the old worker
            # serving traffic, exactly as if nothing had been attempted.
            # Why, read before it is torn down: the exit code and the log.
            why = new.last_words()
            log.error("new worker never became healthy: %s", why)
            await new.shutdown()
            return {
                "ok": False,
                "stage": "health",
                "error": "new worker never became healthy",
                "why": why[-2000:],
                "still_serving": old.name if old else None,
            }

        state.active = new  # the flip; atomic from a request handler's view
        state.history.append(new.name)
        log.info("swapped to worker :%d (%s)", new.port, new.name)

        # The duties -- Signal, the job queue, every loop -- change hands
        # here, old first. Held by both, the new worker requeued the old
        # one's running job and ran it again.
        if old is not None and not await old.retire_duties():
            log.warning("worker :%d did not confirm retiring its duties", old.port)
        if not await new.activate():
            log.error("worker :%d did not take up its duties; the watchdog will retry", new.port)

        if old is not None:
            state.draining.append(old)
            asyncio.create_task(_retire(old, drain_timeout))

        return {
            "ok": True,
            "worker": new.name,
            "port": new.port,
            "retiring": old.name if old else None,
        }


# ---------------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------------


async def sup_health(_: Request) -> JSONResponse:
    worker = state.active
    return JSONResponse(
        {
            "ok": worker is not None and worker.alive,
            "worker": worker.name if worker else None,
            "worker_port": worker.port if worker else None,
            "draining": len(state.draining),
        }
    )


async def sup_status(_: Request) -> JSONResponse:
    policy = load_policy()
    return JSONResponse(
        {
            "active": None
            if not state.active
            else {
                "port": state.active.port,
                "worker": state.active.name,
                "inflight": state.active.inflight,
                "alive": state.active.alive,
            },
            "draining": [
                {"port": w.port, "worker": w.name, "inflight": w.inflight}
                for w in state.draining
            ],
            "history": state.history,
            "frozen_core_violations": _frozen_core_violations(policy),
            "frozen_core_check": "manifest" if paths.is_installed() else "lock",
        }
    )


async def sup_swap(_: Request) -> JSONResponse:
    result = await swap()
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


async def sup_stop(_: Request) -> JSONResponse:
    """Stop, as the app's Quit and Stop ask (ui/src-tauri/src/backend.rs).

    uvicorn finishes the requests in flight, the lifespan's end stops every
    worker, and `main` withdraws the run file. The app gives this ten
    seconds; its job object ends whatever is left after that, so this is
    the clean way down, not the only one."""
    if state.server is None:
        return JSONResponse({"ok": False, "error": "not started by main()"}, status_code=503)
    state.server.should_exit = True
    return JSONResponse({"ok": True})


async def sup_prove(request: Request) -> JSONResponse:
    """The one route that answers without the token: proof that this is the
    backend the run file names, asked for before the token is sent
    (core/localapi.py). It answers the proof and nothing else."""
    try:
        nonce = (await request.json()).get("nonce")
    except (ValueError, AttributeError):
        nonce = None
    if not localapi.fair_nonce(nonce):
        return JSONResponse({"error": "a nonce of 32 to 128 hex digits"}, status_code=400)
    if state.backend is None:
        return JSONResponse({"error": "no run file published"}, status_code=503)
    return JSONResponse({"proof": localapi.proof(state.backend.token, nonce, state.backend.port)})


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------


async def proxy(request: Request) -> Response:
    worker = state.active
    if worker is None or not worker.alive:
        return JSONResponse({"error": "no healthy worker"}, status_code=503)

    path = request.path_params.get("path", "")
    if path.startswith("_"):
        # The worker's control routes -- `/_shutdown`, `/_activate`,
        # `/_retire` -- are for this process to call on the worker's own
        # port, never for anything outside to reach through the proxy.
        return JSONResponse({"error": "not found"}, status_code=404)

    worker.inflight += 1
    try:
        url = f"{worker.base_url}/{path}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        upstream = await state.client.request(
            request.method,
            url,
            headers=headers,
            params=request.query_params,
            content=await request.body(),
        )
        out = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=out,
            media_type=upstream.headers.get("content-type"),
        )
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"upstream failure: {exc}"}, status_code=502)
    finally:
        worker.inflight -= 1


async def ws_proxy(ws: WebSocket) -> None:
    """Pin a websocket to whichever worker was active when it connected.

    Long-lived sessions (a voice conversation) deliberately stay on the old
    worker across a swap rather than being cut off mid-sentence. The drain
    timeout is what bounds how long we tolerate that.
    """
    worker = state.active
    if worker is None or not worker.alive:
        await ws.close(code=1013)  # try again later
        return

    # `steve`, if the app offered it: its token rode in the other subprotocol.
    await ws.accept(subprotocol=localapi.subprotocol(ws.scope))
    worker.inflight += 1
    try:
        async with websockets.connect(f"ws://127.0.0.1:{worker.port}/ws",
                                      additional_headers=localapi.headers()) as upstream:

            async def client_to_worker() -> None:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(msg.get("code", 1000))
                    if (text := msg.get("text")) is not None:
                        await upstream.send(text)
                    elif (data := msg.get("bytes")) is not None:
                        await upstream.send(data)

            async def worker_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, str):
                        await ws.send_text(message)
                    else:
                        await ws.send_bytes(message)

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_worker()),
                    asyncio.create_task(worker_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(Exception):
                    task.result()
    except Exception as exc:  # upstream gone, handshake failed, client vanished
        log.debug("ws proxy closed: %s", exc)
    finally:
        worker.inflight -= 1
        # An app already gone makes Starlette raise WebSocketDisconnect here,
        # not RuntimeError: every quit from the tray left two tracebacks in
        # the log the app shows when the backend keeps stopping.
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await ws.close()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


# How often to check that there is still a worker, and how long to wait
# after the first failed restart before trying again. The wait doubles from
# there (see `backoff`), so this one can be short: a single failed restart
# is most likely a worker that was merely slow to start -- under load one
# can miss the thirty-second health window -- and making that case wait a
# full minute is what put a killed worker's recovery past two minutes. The
# growth is what stops a worker that crashes on startup from spinning.
WATCH_INTERVAL_S = 15
WATCH_BACKOFF_S = 15

# How often, between those checks, to look whether the worker's process has
# exited: a poll of its handle, which costs nothing. A killed worker waited
# out the rest of the interval before anyone looked, up to fifteen seconds
# with the agent gone from every channel, for no reason but the loop's one
# pace. Only for a worker that has run a full interval: one that dies young
# is restarted no faster than before, so a crash loop does not speed up.
DEATH_POLL_S = 0.5

# How long a worker is left alone before its health counts against it, and
# how many consecutive failures past that are needed to act. Together these
# are three quarters of a minute of a live process refusing connections --
# long enough that nothing transient trips it, short enough that nobody has
# time to notice by hand.
HEALTH_GRACE_S = 180
UNHEALTHY_STRIKES = 3

# The ceiling on the wait between failed restarts. Doubling from
# `WATCH_BACKOFF_S` rather than retrying at a flat interval, because a
# worker that has failed to start five times in a row is not going to start
# on the sixth, and every attempt writes a new log and prunes an old one --
# so a flat retry loop is also a machine for erasing its own evidence.
MAX_BACKOFF_S = 900

# How much of a dead worker's log to keep, copied into the supervisor's own
# output before it can be pruned.
DEATH_TAIL_LINES = 40

def backoff(failures: int) -> float:
    """How long to wait after `failures` restarts in a row have failed."""
    if failures <= 0:
        return 0.0
    return float(min(WATCH_BACKOFF_S * 2 ** (failures - 1), MAX_BACKOFF_S))


# Each death, as one JSON line, for the next worker to put in the failure
# ledger (agent/issues.record_deaths). The supervisor keeps no database, so it
# leaves a note; without one, a worker that crashed three times in five days
# (24-29 September, native faults in ntdll.dll) appeared nowhere but here.
def deaths_path() -> Path:
    return paths.data_dir() / "worker_deaths.jsonl"


DEATH_NOTE_LINES = 30
# How far back in a dead worker's log to look for a native fault's dump.
DEATH_SCAN_LINES = 5_000
# What faulthandler (agent/server.py) writes first when the process faults.
FAULT_HEADERS = ("Windows fatal exception", "Fatal Python error")


def _fault(lines: list[str]) -> list[str]:
    """The last native fault in a worker's log, as the few lines before it,
    its header, and the stack of the thread that faulted -- or [] if none.

    Not simply the end of the log: faulthandler prints every thread, and
    the one that faulted can be anywhere among them. The first crash it
    caught (30 September, 00:07) was a 205-line dump with "Current thread"
    111 lines from the end, so the last 30 lines were nine other threads
    waiting on sockets, and the one line that named the fault was not in
    them."""
    start = next((i for i in range(len(lines) - 1, -1, -1)
                  if lines[i].startswith(FAULT_HEADERS)), None)
    if start is None:
        return []
    current = next((i for i in range(start, len(lines))
                    if lines[i].startswith("Current thread")), None)
    if current is None:
        return lines[max(0, start - 5):start + DEATH_NOTE_LINES]
    end = next((i for i in range(current, len(lines)) if not lines[i].strip()), len(lines))
    return [*lines[max(0, start - 5):start + 1], "\n", *lines[current:end]]


def _exit_code(worker) -> str | None:
    """How the process ended: Windows' NTSTATUS in hex for a fault
    (0xC0000005 is an access violation), a plain number otherwise."""
    proc = getattr(worker, "proc", None)
    code = proc.poll() if proc is not None else None
    if code is None:
        return None
    return f"0x{code & 0xFFFFFFFF:08X}" if code < 0 or code > 0x7FFFFFFF else str(code)


def _note_death(worker, how: str, lines: list[str]) -> None:
    if os.environ.get("STEVE_INERT", "").strip().lower() not in ("", "0", "false"):
        return  # a gate test's supervisor, killing its workers on purpose
    entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "worker": worker.name,
             "how": how, "exit": _exit_code(worker),
             # Scrubbed again: what the worker wrote past logging (an
             # uncaught error, a fault) is in its log as it was written.
             "tail": logscrub.scrub("".join(_fault(lines) or lines[-DEATH_NOTE_LINES:])[-6000:])}
    with contextlib.suppress(OSError):
        deaths_path().parent.mkdir(parents=True, exist_ok=True)
        with deaths_path().open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry) + "\n")


def record_death(worker, how: str = "gone") -> None:
    """Copy the end of a dead worker's log somewhere pruning cannot reach.

    Worker logs are kept by count, one per worker, so every restart writes
    one and deletes the oldest. A crash loop of twenty restarts therefore
    erases every log from before it started -- including the one that says
    why. Found the morning the agent had been down all night: whatever
    killed it had already been pruned away by the time anyone looked.
    """
    path = getattr(getattr(worker, "log_file", None), "name", None)
    lines: list[str] = []
    if path:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-DEATH_SCAN_LINES:]
        except OSError:
            pass
    if getattr(worker, "name", None):
        _note_death(worker, how, lines)
    # A native fault's own stack, rather than the end of the dump: see _fault.
    tail = _fault(lines) or lines[-DEATH_TAIL_LINES:]
    if not path:
        return
    if not tail:
        # Workers log at warning and above, so a healthy one writes nothing,
        # and a crash leaves at least a traceback. Silence is therefore a
        # clue in itself: something outside ended it -- Task Manager, an
        # OOM kill, a `taskkill` -- rather than anything it did.
        log.error(
            "worker %s died with an empty log (%s): it was ended from outside, "
            "not by an error of its own", worker.name, path,
        )
        return
    log.error(
        "last %d lines from worker %s (%s):\n%s",
        len(tail), worker.name, path, "".join(tail).rstrip(),
    )


async def _answers(worker) -> bool:
    """Is the worker actually serving, or merely running?"""
    try:
        response = await state.client.get(f"{worker.base_url}/health", timeout=5.0)
    except Exception:  # noqa: BLE001 - a refused connection is the answer
        return False
    return response.status_code == 200


async def _wait_out(seconds: float) -> None:
    """Sleep `seconds`, or less if the active worker's process exits after
    running a full interval, outside a swap (`DEATH_POLL_S`)."""
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        await asyncio.sleep(min(DEATH_POLL_S, left))
        worker = state.active
        if (worker is not None and not worker.alive and not state.swap_lock.locked()
                and time.monotonic() - worker.started_at >= WATCH_INTERVAL_S):
            return


async def _ensure_on_duty(worker) -> None:
    """Activate a serving worker still on standby.

    Its activation after a swap can be lost, and such a worker answers
    every request and does nothing else: no Signal, no job queue. It
    looks healthy from outside, which is why this has to look inside.
    """
    try:
        response = await state.client.get(f"{worker.base_url}/health", timeout=5.0)
        duties = response.json().get("duties")
    except Exception:  # noqa: BLE001 - the health check proper decides that
        return
    if duties == "standby":
        log.warning("worker :%d is serving on standby; activating it", worker.port)
        await worker.activate()


async def watchdog() -> None:
    """Put a worker back when one dies.

    The supervisor started one at boot, swapped on request, and did nothing
    else -- so a worker that exited for any reason left `/health` answering
    `ok: false` indefinitely and the agent simply gone. Every channel went
    with it: the tray, voice, and Signal.

    That is the one job this process exists for. A crash is a thing to
    recover from, not a thing to report.

    Liveness first, because a worker whose process is gone is unambiguous,
    and because a worker still loading Whisper is not answering `/health`
    yet and must not be killed and respawned into the same slow start
    forever.

    But liveness alone was not enough, and the way that surfaced is worth
    recording: a shutdown request reached the worker through the proxy, it
    closed its HTTP server and the process lingered. `alive` stayed true,
    the watchdog stayed quiet, and every request got `upstream failure` for
    as long as anyone left it. A process that is running and refusing
    connections is exactly as useless as one that has exited, so health
    counts too -- after `HEALTH_GRACE_S`, which is what keeps the slow-start
    reasoning above intact, and only after `UNHEALTHY_STRIKES` in a row.
    """
    strikes = 0
    failures = 0
    while True:
        await _wait_out(WATCH_INTERVAL_S)
        worker = state.active

        # Never while a swap is in flight -- that path is replacing the
        # worker on purpose and briefly has none. Strikes reset with it,
        # so a slow swap cannot be mistaken for a wedged worker.
        if state.swap_lock.locked():
            strikes = 0
            continue

        if worker is not None and worker.alive:
            if time.monotonic() - worker.started_at < HEALTH_GRACE_S:
                strikes = 0
                continue
            if await _answers(worker):
                strikes = 0
                failures = 0
                await _ensure_on_duty(worker)
                continue
            strikes += 1
            if strikes < UNHEALTHY_STRIKES:
                continue
            log.error(
                "worker is alive but has refused %d health checks; replacing it",
                strikes,
            )
            record_death(worker, how="unresponsive")
            strikes = 0
        else:
            log.error("worker is gone; restarting it")
            if worker is not None:
                record_death(worker)

        result = await swap()
        if result.get("ok"):
            failures = 0
            continue

        failures += 1
        wait = backoff(failures)
        log.error(
            "could not restart the worker (%d in a row): %s -- next try in %ds",
            failures, result, wait,
        )
        await asyncio.sleep(wait)


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    # With this start's token, which the workers require too. The proxy
    # passes on the caller's, which LocalOnly has checked is the same.
    state.client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0),
                                     headers=localapi.headers(), verify=localapi.tls())

    policy = load_policy()
    if problems := _frozen_core_violations(policy):
        log.warning("frozen core does not match policy.lock: %s", problems)

    if orphans := reap_orphans():
        log.warning("reaped %d orphaned worker(s): %s", len(orphans), orphans)

    result = await swap()
    if not result["ok"]:
        log.error("could not start initial worker: %s", result)

    watching = asyncio.create_task(watchdog())
    try:
        yield
    finally:
        watching.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watching
        for worker in [state.active, *state.draining]:
            if worker:
                await worker.shutdown()
        await state.client.aclose()


app = Starlette(
    lifespan=lifespan,
    middleware=[
        # First, so it is outermost: nothing below sees a refused request.
        Middleware(localonly.LocalOnly),
        Middleware(
            CORSMiddleware,
            allow_origins=ALLOWED_ORIGINS,
            # Every method a worker route takes (test_local_only.py): PUT
            # was left out, so the app's pages, on an origin of their own,
            # could not save the user's name, in setup or on Profile.
            allow_methods=["GET", "POST", "PUT", "OPTIONS"],
            allow_headers=["content-type", "authorization"],
        )
    ],
    routes=[
        Route("/_sup/health", sup_health),
        Route("/_sup/status", sup_status),
        Route("/_sup/swap", sup_swap, methods=["POST"]),
        Route("/_sup/stop", sup_stop, methods=["POST"]),
        Route(localapi.PROVE_PATH, sup_prove, methods=[localapi.PROVE_METHOD]),
        WebSocketRoute("/ws", ws_proxy),
        Route(
            "/{path:path}",
            proxy,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        ),
    ],
)


def main() -> None:
    # Before anything starts: every worker, coder run and script below
    # inherits this environment, and none may read the personal edition's
    # variables (core/identity.py).
    identity.scrub_environment()
    # Said in the log, whose end the app shows when the backend keeps
    # stopping.
    if (why := paths.overlap()) is not None:
        raise SystemExit(f"Steve will not start: {why}")
    logscrub.install()
    parser = argparse.ArgumentParser(description="Steve's supervisor")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    # Made as a worker's is: it was a fixed port, 8790, which another
    # account's program could take first (core/localapi.py).
    sock = listening()
    # Published before the first worker starts, which reads its token here.
    state.backend = localapi.publish(sock.getsockname()[1])
    localapi.accept(state.backend.token)
    log.info("listening on 127.0.0.1:%d (pid %d)", state.backend.port, state.backend.pid)
    # No line per request: each held the path and query the app asked for,
    # which can be what the user searched. The worker's run at "warning",
    # below uvicorn's access lines.
    state.server = uvicorn.Server(uvicorn.Config(app, log_level=args.log_level,
                                                 access_log=False))
    try:
        state.server.run(sockets=[sock])
    finally:
        localapi.withdraw(state.backend)


if __name__ == "__main__":
    main()
