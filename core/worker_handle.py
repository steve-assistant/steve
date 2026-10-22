"""Lifecycle of a single worker process.

Windows has no SIGTERM, so graceful shutdown is done by asking the worker to
exit over HTTP and only escalating to TerminateProcess if it refuses. That
ordering is the whole reason in-flight requests survive a swap.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psutil

from core import localapi, paths

REPO_ROOT = Path(__file__).resolve().parent.parent


# How long a swap waits for a new worker to answer /health: ninety seconds,
# unless the environment of the process that swaps says otherwise. It was
# thirty until 8 October 2026 (the user's decision): a worker starts in about 4 s
# on a quiet laptop but took 22 s cold, and one slow start under load was a
# swap thrown away -- and, after a merge, `adopt` putting main back. A worker
# that dies on the way is given up on at once (`wait_healthy` below), so this
# bounds only one that is alive and slow. The gate's supervisor is given a
# longer one (tests/gate/test_zero_downtime.py): started cold, as the
# coding sandbox's account starts everything -- no bytecode cache, files not
# yet read -- a worker took 22 s to import on a quiet machine, and the same
# supervisor starts three workers at once under `pytest -n 3`. A swap that
# misses the window is final, where the fixture's own wait has the watchdog
# to try again. Production sets nothing: its behaviour is the ninety seconds.
START_TIMEOUT_ENV = "STEVE_WORKER_START_TIMEOUT_S"
DEFAULT_START_TIMEOUT_S = 90.0


def start_timeout() -> float:
    """Seconds a swap waits for a new worker: `START_TIMEOUT_ENV` if it holds
    a positive number, else `DEFAULT_START_TIMEOUT_S`."""
    try:
        seconds = float(os.environ.get(START_TIMEOUT_ENV, ""))
    except ValueError:
        return DEFAULT_START_TIMEOUT_S
    return seconds if seconds > 0 else DEFAULT_START_TIMEOUT_S


def listening() -> socket.socket:
    """A socket on 127.0.0.1, on a port the system chooses, which no other
    socket may share, and listening: the supervisor's own, and each
    worker's. The supervisor makes a worker's before
    the worker starts, hands it over (`spawn_worker`) and holds its own copy
    until the worker has stopped (`WorkerHandle.shutdown`), so the port is
    never free while anything here might send to it.

    It was a port number (`free_port`): bound, read and closed, then bound
    again by the worker once it had imported everything, 4 to 22 s later.
    Meanwhile the port was anyone's, and `wait_healthy` sent this start's
    token to whatever answered on it. Another Windows account's program can
    read the TCP table, process ids and all, see the supervisor's polls
    waiting on an unbound port, and bind it first (step 11's audit). And
    listening from the start, a poll waits in the backlog and is answered
    once the worker serves, rather than taking Windows two seconds to be
    refused. The supervisor's own port is the same: its first worker starts
    before uvicorn serves, and a caller that read the run file meanwhile
    lost two seconds a try (measured with devtools.slow, 14 October 2026)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no second binder
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock


def _client(timeout: float) -> httpx.AsyncClient:
    """A client of a worker: this start's token, and no certificates read
    (`localapi.tls`)."""
    return httpx.AsyncClient(timeout=timeout, headers=localapi.headers(), verify=localapi.tls())


# Where a worker's output goes. A file rather than a pipe, and that is a
# correctness decision rather than a convenience: nothing here ever read the
# pipe, so once a worker had written a bufferful -- about 64KB on Windows --
# its next log line blocked forever. Logging happens on the event loop, so the
# whole agent froze: health checks unanswered, Signal unanswered, and no
# error anywhere, because the process that would have reported
# it was stuck inside the reporting.
# Keep a few runs. Enough to read back through a bad swap, not enough to grow
# without bound on a machine that swaps several times an hour.
KEEP_LOGS = 20


@dataclass
class WorkerHandle:
    port: int
    proc: subprocess.Popen
    # The order it started in and when (`supervisor.swap`), for the logs
    # and the status: what tells one worker from the next. It was the commit
    # it ran, from when the supervisor moved a git checkout.
    name: str
    started_at: float = field(default_factory=time.monotonic)
    inflight: int = 0
    draining: bool = False
    # Held so it is not garbage-collected while the child writes to it, and
    # so stop() can close it.
    log_file: object | None = None
    # The supervisor's copy of the worker's listening socket (`listening`),
    # held until the worker has stopped, so that no one else can bind its
    # port while anything here might still send to it.
    sock: socket.socket | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def last_words(self, lines: int = 15) -> str:
        """Why a worker that never became healthy did not: its exit code if
        it died, and the end of its log. A swap used to say only "new worker
        never became healthy", and upgrade #89's real cause -- OpenBLAS out of
        memory at import -- sat in a log nobody read (8 October 2026)."""
        code = self.proc.poll()
        said = (f"it exited with code {code}" if code is not None
                else "it was running but never answered /health")
        name = getattr(self.log_file, "name", None)
        if name:
            with contextlib.suppress(OSError):
                text = Path(name).read_text(encoding="utf-8", errors="replace")
                if tail := "\n".join(text.splitlines()[-lines:]).strip():
                    said += f"; its log ends:\n{tail}"
        return said

    async def wait_healthy(self, timeout: float | None = None, interval: float = 0.15) -> bool:
        """Poll /health until the worker answers or we give up.

        `timeout` is `start_timeout()` unless a caller names one."""
        deadline = time.monotonic() + (start_timeout() if timeout is None else timeout)
        async with _client(2.0) as client:
            while time.monotonic() < deadline:
                if not self.alive:
                    return False  # died on startup; no point waiting out the clock
                try:
                    r = await client.get(f"{self.base_url}/health")
                    if r.status_code == 200 and r.json().get("ok") is True:
                        return True
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(interval)
        return False

    async def activate(self, attempts: int = 3) -> bool:
        """Hand this worker the duties. Retried, because a worker serving
        traffic with no duties answers everything and does nothing else --
        no Signal, no jobs. The watchdog retries later as well."""
        for attempt in range(attempts):
            with contextlib.suppress(httpx.HTTPError):
                async with _client(10.0) as client:
                    response = await client.post(f"{self.base_url}/_activate")
                if response.status_code == 200:
                    return True
            if attempt + 1 < attempts:
                await asyncio.sleep(1.0)
        return False

    async def retire_duties(self) -> bool:
        """Take the duties back before a replacement takes them up. The
        worker keeps serving the requests it already has."""
        with contextlib.suppress(httpx.HTTPError):
            async with _client(15.0) as client:
                response = await client.post(f"{self.base_url}/_retire")
            return response.status_code == 200
        return False

    async def drain(self, timeout: float = 30.0, interval: float = 0.1) -> bool:
        """Stop accepting new work and wait for in-flight requests to finish."""
        self.draining = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.inflight <= 0:
                return True
            await asyncio.sleep(interval)
        return self.inflight <= 0

    async def shutdown(self, grace: float = 10.0) -> None:
        """Ask nicely, then insist; then let go of its port and its log.
        Both were kept open when the worker had already died, or exited when
        asked: only a worker that had to be terminated had its log closed."""
        try:
            await self._stop(grace)
        finally:
            for held in (self.sock, self.log_file):
                if held is not None:
                    with contextlib.suppress(Exception):
                        held.close()

    async def _stop(self, grace: float) -> None:
        if not self.alive:
            return
        with contextlib.suppress(Exception):
            async with _client(2.0) as client:
                await client.post(f"{self.base_url}/_shutdown")

        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not self.alive:
                return
            await asyncio.sleep(0.1)

        with contextlib.suppress(Exception):
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                self.proc.kill()


def _open_log(port: int, name: str):
    """A file for this worker to write to, and a prune of the old ones."""
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    handle = (paths.logs_dir() / f"worker-{stamp}-{name}-{port}.log").open(
        "w", encoding="utf-8", errors="replace", buffering=1
    )
    old = sorted(paths.logs_dir().glob("worker-*.log"))[:-KEEP_LOGS]
    for path in old:
        with contextlib.suppress(OSError):
            path.unlink()
    return handle


# Tells a worker to wait for `/_activate` before taking up its duties --
# Signal, the job queue, the loops -- so two never hold them at once.
# See `_take_up_duties` in agent/server.py.
STANDBY_ENV = "STEVE_STANDBY"


# Who started a worker, as "pid:creation time" of the process that spawned
# it, in the worker's environment -- where older code that knows nothing of
# it ignores it, so a supervisor can still start a worker at any commit.
# `reap_orphans` kills a worker only once its owner is gone. It used to kill
# any worker whose parent was not a supervisor, which is also every worker a
# test, the coding harness or a second checkout's tooling starts: run side by
# side, one test's supervisor killed another test's worker (6 October 2026).
# The creation time is what tells the owner from a later process given the
# same pid.
OWNER_ENV = "STEVE_WORKER_OWNER"
# The data folder of the process that started a worker: whose install it is.
# An orphan is reaped by its own install, or by anyone once that folder is
# gone, as a finished test's is. Any supervisor's start used to reap every
# orphan, whosever it was: another test's start killed the zero-downtime
# gate's own orphan before the gate could reap it (exit code 15, psutil's
# kill, 18 October 2026). Each test has its own data folder, so each test is
# its own install here, and an install is a data folder: no id file is read
# or made to judge a process.
DATA_ENV = "STEVE_WORKER_DATA"


def owner_token(pid: int | None = None) -> str:
    """The token a worker started by `pid` (this process if None) carries."""
    proc = psutil.Process(pid) if pid else psutil.Process()
    return f"{proc.pid}:{proc.create_time():.3f}"


def spawn_worker(name: str, cwd: Path | None = None, env: dict | None = None,
                 owner: str | None = None) -> WorkerHandle:
    """Start a worker on a fresh port, on standby. Does not wait for it to
    become healthy, and does not activate it. It is this process's to look
    after (`OWNER_ENV`) unless `owner` names another."""
    sock = listening()
    port = sock.getsockname()[1]
    log_file = _open_log(port, name)
    env = dict(os.environ if env is None else env)
    env[STANDBY_ENV] = "1"
    # Set, never inherited: a worker that starts a worker owns that one.
    env[OWNER_ENV] = owner or owner_token()
    env[DATA_ENV] = str(paths.data_dir())
    # numpy's OpenBLAS reserves a buffer for every hardware thread as it loads:
    # 583 MiB committed by an idle worker, 100 MiB with one thread, starting
    # in the same 3.7 s (measured 8 October 2026), and a swap holds two or
    # three workers at once. Nothing a worker does with numpy is big enough to
    # want the threads. Unless the caller chose otherwise.
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    try:
        proc = subprocess.Popen(
            # Shut off from the user's Python (`-E -s`): without it their
            # PYTHONPATH and their own site-packages were on every worker's path,
            # so a module of the same name elsewhere on the machine could stand in
            # for one of Steve's. Not `-I`, which also takes the folder it starts
            # in off the path: in a checkout the venv's editable install then
            # served the main checkout's code to a worktree's supervisor, and to a
            # gate test's copy, whose test hung. `-B`: an install's folder is
            # never written.
            [sys.executable, "-E", "-s", "-B", "-m", "agent.server", "--port", str(port),
             "--socket-on-stdin"],
            cwd=str(cwd or REPO_ROOT),
            env=env,
            # A pipe in, written once and closed below: the socket's share.
            stdin=subprocess.PIPE,
            # Never a PIPE out. See paths.logs_dir() above: an unread pipe is a
            # worker that freezes as soon as it has something to say.
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    except BaseException:
        sock.close()
        raise
    # Windows hands a socket to another process as `share` makes it, for
    # that process's id: a few hundred bytes, which wait in the pipe until
    # the worker has imported everything and reads them (agent/server.main).
    # A child that died at once leaves the pipe broken; `wait_healthy` sees
    # it dead.
    with contextlib.suppress(OSError):
        proc.stdin.write(sock.share(proc.pid))
    with contextlib.suppress(OSError):
        proc.stdin.close()
    return WorkerHandle(port=port, proc=proc, name=name, log_file=log_file, sock=sock)


def _is_worker_process(cmdline: list[str]) -> bool:
    """True only for an actual `python -m agent.server` process.

    A substring search for "agent.server" is far too loose: it matches the
    shell that launched the test, the editor with the file open, and anything
    else whose command line happens to mention it. Requiring a python
    executable and an adjacent `-m agent.server` is what makes this safe to
    point at a kill().
    """
    if len(cmdline) < 3:
        return False
    return _runs_module(cmdline, "agent.server")


def _runs_module(cmdline: list[str], module: str) -> bool:
    if len(cmdline) < 2:
        return False
    if not Path(cmdline[0]).name.lower().startswith("python"):
        return False
    return any(
        cmdline[i] == "-m" and cmdline[i + 1] == module for i in range(len(cmdline) - 1)
    )


def _owner_alive(token: str) -> bool | None:
    """Whether the process a worker's `OWNER_ENV` names is still running:
    None for a token that does not parse. A process that cannot be looked
    at counts as alive -- not knowing is no reason to kill."""
    pid_text, _, created = (token or "").partition(":")
    try:
        pid, created_at = int(pid_text), float(created)
    except ValueError:
        return None
    try:
        return abs(psutil.Process(pid).create_time() - created_at) < 1.0
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True


def looked_after(proc) -> bool:
    """Whether a worker process has someone to clean it up.

    A worker is two processes on Windows -- the venv's `python.exe`
    launcher and the interpreter it starts, with one command line -- and
    uvicorn on Windows starts a child with the same command line too
    (confirmed against a bare uvicorn app). The inner one goes with the
    outer: killed on its own, a healthy server dies; and judged on its own
    it was killed first, its launcher then exited by itself, and the reap
    reported a worker it had not killed. The outer one is looked after while
    its owner is alive (`OWNER_ENV`), and, its owner gone, by its own
    install while that install's data folder is there (`DATA_ENV`). A worker
    started before the folder was stamped is judged by its owner alone.

    A worker without a readable owner is never Steve's to judge. The
    personal edition Steve was copied from runs the same `python -m
    agent.server`, as the same user, and records its owner under its own
    variable. Judged by their parents, as workers without an owner were,
    the workers its tests start had no supervisor above them, so Steve's
    next supervisor start, or a unit test that reaped, would have killed
    them (the audit of 12 October 2026). A stray worker left running costs
    memory; another install's worker killed costs its work."""
    try:
        parent = psutil.Process(proc.ppid()).cmdline()
    except psutil.Error:
        parent = []
    if _runs_module(parent, "agent.server"):
        return True  # its worker parent decides, for both
    try:
        env = proc.environ()
    except psutil.Error:
        env = {}
    token = env.get(OWNER_ENV)
    alive = _owner_alive(token) if token else None
    if alive is None or alive:
        return True
    return not _reapable_here(env.get(DATA_ENV))


def _reapable_here(stamp: str | None) -> bool:
    """Whether an orphan stamped with `stamp` (`DATA_ENV`) is this install's
    to reap: it is this install's, or its install's folder is gone, or it
    carries no stamp at all."""
    if not stamp:
        return True
    here = paths.data_dir()
    if os.path.normcase(os.path.abspath(stamp)) == os.path.normcase(os.path.abspath(here)):
        return True
    return not Path(stamp).exists()


def reap_orphans() -> list[int]:
    """Kill worker processes left behind by a supervisor that died badly.

    A clean shutdown drains and stops its workers, but SIGKILL -- or Task
    Manager, or closing the terminal -- skips that entirely, and the orphans
    keep running with a database handle open. Eight of them accumulated during
    one afternoon of testing before this existed.

    Only this install's workers whose owner is gone are killed
    (`looked_after`): any number of supervisors, tests, harnesses and
    installs can each run workers side by side, and none reaps another's
    while that one's data folder is there. The matching above is strict for
    the same reason.
    """
    # Never the caller, and never anything it descends from. This is called
    # from inside a worker during upgrade cleanup, and a worker that reaps
    # itself is restarted by the supervisor, which requeues the job it was
    # running, which starts another upgrade, which reaps again. One such loop
    # produced eleven worktrees and seven concurrent harness processes before
    # it was noticed.
    protected = {os.getpid()}
    with contextlib.suppress(psutil.Error):
        protected.update(p.pid for p in psutil.Process().parents())

    killed: list[int] = []
    # By name first, then the command line of the Pythons only. Asking every
    # process for its command line took 26 seconds on a laptop running 600
    # of them (30 September 2026): a stall in front of every supervisor
    # start and every upgrade's cleanup, and by itself enough to fail the
    # zero-downtime gate's 45-second startup under the full suite. The
    # names are in the process snapshot and cost nothing; a worker is
    # always a Python.
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["pid"] in protected:
            continue
        if not (proc.info.get("name") or "").lower().startswith("python"):
            continue
        try:
            cmdline = proc.cmdline() or []
            if not _is_worker_process(cmdline):
                continue
            if looked_after(proc):
                continue

            # Take uvicorn's child with it, or the next pass would see that
            # child as a fresh orphan and we would leak one process per reap.
            for child in proc.children(recursive=True):
                with contextlib.suppress(psutil.Error):
                    child.kill()
                    killed.append(child.pid)
            proc.kill()
            killed.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return killed
