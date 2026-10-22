"""Gate tests for blue-green worker replacement.

The whole architecture rests on one claim: the agent can replace its own code
while remaining available. This file is where that claim is falsifiable.

These are integration tests -- they start a real supervisor subprocess, which
starts real worker subprocesses, and drive them over real HTTP. That is
deliberate. An in-process mock of this would test the parts that cannot break.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from core import localapi

REPO_ROOT = Path(__file__).resolve().parents[2]
# Real processes: the developer's fast tier leaves this module out (see
# docs/testing.md); the gate and the full suite never do. And on one worker,
# under the check's `--dist loadgroup` (devtools/check.DIST): the supervisor
# below is a module fixture, which xdist's plain `load` started again on
# every worker that drew one of these tests.
pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("supervisor")]
# How long the module's supervisor has to report a healthy worker. It was
# 45 s, and setup takes ~14 s on a quiet laptop: four full runs on a loaded
# one (30 September x3, 7 October) ended with all 17 tests that share the
# supervisor in ERROR at once, with no cause shown, and each cost a rerun.
# A healthy start leaves the loop at its first 200, so a longer limit costs
# nothing when nothing is wrong.
STARTUP_TIMEOUT = 120.0


def _test_env() -> dict:
    """The environment for every process these tests start.

    Inert, so the workers answer HTTP and do nothing else. Before this,
    each test worker was a complete second copy of the agent: it took up
    the real remote channel with the real credentials and knocked the real
    agent off it for as long as this module ran. It also re-armed the real
    wake alarm and shared the job queue.
    """
    import os

    env = dict(os.environ)
    env["STEVE_INERT"] = "1"
    # Its data folder is the copy's own (core/paths.py finds it beside the
    # code), not the test's fenced one, which a module's supervisor would
    # outlive.
    for name in ("STEVE_DATA_DIR", "STEVE_MEMORIES_DIR", "STEVE_MODEL_CACHE"):
        env.pop(name, None)
    # How long this supervisor's swaps wait for a new worker. Its own default
    # is the thirty seconds production uses, and a swap that misses it is
    # final: upgrade #89 was rejected on "new worker never became healthy" in
    # the coding sandbox. What had happened there was the memory limit (see
    # tests/gate/fences.py), not a slow start; but a worker started cold, as
    # that account starts everything, did take 22 s to import once, and this
    # supervisor starts workers three at a time under `-n 3`.
    env["STEVE_WORKER_START_TIMEOUT_S"] = str(STARTUP_TIMEOUT)
    return env


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=gate", "-c", "user.email=gate@localhost", *args],
        cwd=str(root), capture_output=True, text=True, check=True,
    ).stdout


# A route that answers when asked to, for a test that holds a request open
# across a swap, and that says which worker answered. The sandbox's copy of
# the worker gains it before its first commit. Until step 11's audit it was
# in the product, where whoever held the token could hold a request open
# for ever (`delay=inf`) and make every swap wait out its drain.
ECHO_ROUTE = '''async def _echo(request):
    """The sandbox's own route, from tests/gate/test_zero_downtime.py."""
    import asyncio

    from starlette.responses import JSONResponse

    delay = float(request.query_params.get("delay", 0))
    if delay:
        await asyncio.sleep(delay)
    return JSONResponse({"echo": request.query_params.get("msg", ""), "pid": os.getpid()})


app.router.routes.append(Route("/echo", _echo))


'''
_MAIN = b'if __name__ == "__main__":'


def _with_echo(source: bytes) -> bytes:
    """The worker's source with `/echo` added, before its main guard runs."""
    head, guard, tail = source.rpartition(_MAIN)
    assert guard and tail.strip() == b"main()", "agent/server.py no longer ends in its main guard"
    return head + ECHO_ROUTE.encode() + guard + tail


@pytest.fixture(scope="module")
def sandbox_repo(tmp_path_factory) -> Path:
    """A throwaway copy of the working tree, with a git history of its own.

    Everything under test runs from here, never from the checkout pytest
    was started in -- because that checkout is the one the live agent runs
    from. Until this, the supervisor under test ran in it: the broken-revision
    test wrote `raise RuntimeError('deliberately broken worker')` over the
    live `agent/server.py` for as long as that test took, so a live worker
    restarting in that window would have started broken, and a self-upgrade
    merging in it would have met a dirty tree. Its swaps ran `git checkout`
    in the live repository, its workers wrote their logs into the live
    `data/logs` and pruned the live agent's to make room, and they opened
    the live database.

    core/paths.py finds the data folder beside the code, so a supervisor
    started from the copy keeps its logs and database inside the copy. What
    is copied is what is on disk -- tracked files and untracked ones git
    does not ignore -- so uncommitted work is what gets tested, which is the
    point of running the gate before committing it.

    One thing is added: the copy's `agent/server.py` serves `/echo`
    (`ECHO_ROUTE`), which the product does not.

    The checkout's compiled bytecode comes too. Workers run with `-B` and
    write none, so in a copy without it every worker this module starts
    compiled all of Steve's modules again: about 4 s a start, measured on
    21 October 2026, on one worker. `copy2` keeps each source's time, which
    the bytecode is checked against; one that no longer matches its source
    is compiled again, as before.
    """
    import importlib.util
    import os
    import shutil

    root = tmp_path_factory.mktemp("repo")
    listed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=str(REPO_ROOT), capture_output=True, check=True,
    ).stdout
    for rel in filter(None, listed.split(b"\0")):
        source = REPO_ROOT / os.fsdecode(rel)
        if source.is_file():  # a tracked file deleted on disk is listed too
            target = root / os.fsdecode(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if source.suffix != ".py":
                continue
            compiled = Path(importlib.util.cache_from_source(source))
            if compiled.is_file():
                into = Path(importlib.util.cache_from_source(target))
                into.parent.mkdir(exist_ok=True)
                shutil.copy2(compiled, into)
    server = root / "agent" / "server.py"
    server.write_bytes(_with_echo(server.read_bytes()))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "gate snapshot of the working tree")
    return root


@pytest.fixture(scope="module")
def supervisor(tmp_path_factory, sandbox_repo):
    """A live supervisor on its own port, torn down at the end of the module.

    Its output goes to a file, never to a pipe. This fixture used
    `stdout=PIPE` and only read it if startup failed -- the exact mistake
    `core/worker_handle.py` documents, and it bit here the same way. Once
    the supervisor had written more than a Windows pipe buffer holds, its
    next log line blocked, which froze its event loop, which surfaced as
    `ReadTimeout` on every request in the last two tests of this module.
    It appeared when the watchdog started copying a dead worker's last forty
    lines into its own log -- exactly when those two tests run -- and only
    in the full suite, where the lines happened to be long enough to cross
    the line.
    """
    global _SUPERVISOR_LOG, _SANDBOX_REPO, _TOKEN
    _SANDBOX_REPO = sandbox_repo
    _SUPERVISOR_LOG = tmp_path_factory.mktemp("supervisor") / "supervisor.log"
    log_handle = _SUPERVISOR_LOG.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-m", "core.supervisor", "--log-level", "warning"],
        cwd=str(sandbox_repo),
        env=_test_env(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    served = _serving(proc, _run_file(sandbox_repo))
    if served is None:
        log_handle.flush()
        # With its log, and each worker's: this message alone was all four
        # loaded runs showed, seventeen times over, and the cause had to be
        # found by rerunning.
        died = proc.poll() is not None
        why = _diagnosis()
        _stop_everything(proc, sandbox_repo)
        if died:
            pytest.fail(f"supervisor died on startup:\n{why}")
        pytest.fail(f"supervisor never reported a healthy worker within {STARTUP_TIMEOUT:.0f} s"
                    f" (a loaded machine, or a worker that cannot start):\n{why}")
    base, _TOKEN = served

    try:
        yield base
    finally:
        _stop_everything(proc, sandbox_repo)
        log_handle.close()


_SUPERVISOR_LOG: Path | None = None
_SANDBOX_REPO: Path | None = None
# The module's supervisor's token, from its run file.
_TOKEN: str | None = None


def _run_file(repo: Path) -> Path:
    """Where a supervisor started from `repo` names its port and token."""
    return repo / "data" / "run" / "backend.json"


def _serving(proc: subprocess.Popen, run_file: Path) -> tuple[str, str] | None:
    """A supervisor's address and token once it reports a healthy worker;
    None if it died first, or never did within `STARTUP_TIMEOUT`.

    Its port is the system's choice, named with this start's token in the
    run file it writes (core/localapi.py)."""
    base = token = None
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline and proc.poll() is None:
        if base is None:
            try:
                published = json.loads(run_file.read_text(encoding="utf-8"))
                base, token = f"http://127.0.0.1:{published['port']}", published["token"]
            except (OSError, ValueError, KeyError):
                time.sleep(0.2)
                continue
        try:
            r = httpx.get(f"{base}/_sup/health", timeout=2.0, verify=localapi.tls(),
                          headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200 and r.json().get("ok"):
                return base, token
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return None


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _client(timeout: float) -> httpx.AsyncClient:
    """A client as the app is one: with this start's token on every request.
    Given the shared TLS context: one of its own took a third of a second to
    make, and the module makes dozens (`localapi.tls`)."""
    return httpx.AsyncClient(timeout=timeout, headers=_headers(), verify=localapi.tls())


def _subprotocols() -> list[str]:
    """The token on a websocket, as the app's browser API has to send it."""
    return ["steve", f"token.{_TOKEN}"]


def _stop_everything(proc: subprocess.Popen, repo: Path) -> None:
    """Stop the supervisor, and with it every worker still running from `repo`.

    `proc.terminate()` alone is TerminateProcess on Windows: the supervisor
    dies at once, without stopping the worker it started. That worker -- 583
    MiB of committed memory each -- then lived on, ownerless, until some
    later supervisor's startup reaped it (`reap_orphans`), so a gate run
    carried a leaked worker for each pytest worker that had finished with
    this module, on top of the ones in use. In the coding sandbox that
    memory counts against the 6 GiB its whole tree is allowed
    (tests/gate/fences.py), and a run that reached the limit failed the next
    worker's start. Found by listing processes after a run: one
    `agent.server` left in the module's temp folder.

    The supervisor goes first, so its watchdog cannot replace a worker that
    is killed; then what it started, and anything a swap started after that
    list was made -- each of them runs with the copy as its working
    directory.
    """
    import psutil

    try:
        family = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        family = []
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)

    here = repo.resolve()
    for other in psutil.process_iter():
        try:
            if Path(other.cwd()).resolve().is_relative_to(here):
                family.append(other)
        except (psutil.Error, OSError, ValueError):
            continue
    for victim in family:
        with contextlib.suppress(psutil.Error):
            victim.kill()
    psutil.wait_procs(family, timeout=10)


def _supervisor_tail(lines: int = 30) -> str:
    """The end of the test supervisor's own output, for failure messages."""
    if _SUPERVISOR_LOG is None or not _SUPERVISOR_LOG.exists():
        return "(no supervisor log)"
    text = _SUPERVISOR_LOG.read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _worker_tails(lines: int = 8, newest: int = 6) -> str:
    """The end of each recent worker's own log, oldest first.

    A worker that cannot start says why there and nowhere else. The
    supervisor's answer to it is "new worker never became healthy" whether the
    worker was slow, or died importing: under the coding sandbox's memory
    limit it died with "OpenBLAS error: Memory allocation still failed", and
    that line was in a log nobody printed."""
    if _SANDBOX_REPO is None:
        return "(no worker logs)"
    logs = sorted((_SANDBOX_REPO / "data" / "logs").glob("worker-*.log"))[-newest:]
    if not logs:
        return "(no worker logs)"
    out = []
    for path in logs:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        out.append(f"--- {path.name}\n" + ("\n".join(text.splitlines()[-lines:]) or "(empty)"))
    return "\n".join(out)


def _diagnosis() -> str:
    """What to read when a swap or a start failed: the supervisor's output,
    then each worker's."""
    return f"supervisor:\n{_supervisor_tail()}\nworkers:\n{_worker_tails()}"


async def _worker_pid(client: httpx.AsyncClient, base: str) -> int:
    r = await client.get(f"{base}/echo", params={"msg": "ping"})
    r.raise_for_status()
    return r.json()["pid"]


# A swap answers once the new worker does, so the request that asks for one
# has to be willing to wait as long as the supervisor is (`_test_env`), or a
# slow start on a loaded machine surfaces as a ReadTimeout from the test's own
# client rather than as the swap that worked.
SWAP_TIMEOUT = STARTUP_TIMEOUT + 30.0


async def _swap(client: httpx.AsyncClient, base: str) -> httpx.Response:
    return await client.post(f"{base}/_sup/swap", timeout=SWAP_TIMEOUT)


async def _until(check, why: str, timeout: float = 60.0) -> None:
    """Wait for `check()`, a plain or an async callable, to be true.

    For a thing the test needs to have happened before it goes on, in place
    of a sleep that guesses how long it takes: a guess is too long on a
    quiet machine and too short on the loaded one these tests failed on.
    The limit is generous because it is free when the thing happens, and the
    failure says what it was waiting for, with the logs."""
    deadline = time.monotonic() + timeout
    while True:
        done = check()
        if inspect.isawaitable(done):
            done = await done
        if done:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"{why} within {timeout:.0f} s\n{_diagnosis()}")
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------


async def test_supervisor_proxies_to_a_worker(supervisor):
    async with _client(10.0) as client:
        r = await client.get(f"{supervisor}/echo", params={"msg": "hello"})
        assert r.status_code == 200
        assert r.json()["echo"] == "hello"


async def test_swap_actually_replaces_the_process(supervisor):
    async with _client(60.0) as client:
        before = await _worker_pid(client, supervisor)

        r = await _swap(client, supervisor)
        assert r.status_code == 200, f"{r.text}\n{_diagnosis()}"
        assert r.json()["ok"] is True

        after = await _worker_pid(client, supervisor)
        assert after != before, "swap reported success but the same process is still serving"


async def test_no_requests_dropped_during_swap(supervisor):
    """Hammer the proxy continuously while swapping underneath it.

    This is the Phase 0 acceptance criterion. Any non-200, any exception, any
    connection reset is a failure -- 'mostly available' is not the claim.
    """
    failures: list[str] = []
    pids: set[int] = set()
    stop = asyncio.Event()

    async def hammer(client: httpx.AsyncClient) -> None:
        while not stop.is_set():
            try:
                r = await client.get(f"{supervisor}/echo", params={"msg": "x"})
                if r.status_code != 200:
                    failures.append(f"status {r.status_code}: {r.text[:120]}")
                else:
                    pids.add(r.json()["pid"])
            except Exception as exc:  # noqa: BLE001 - any failure at all counts
                failures.append(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(0.01)

    async with _client(30.0) as client:
        hammers = [asyncio.create_task(hammer(client)) for _ in range(8)]
        # Hammering before the swap starts: wait for it, not for half a second.
        await _until(lambda: pids, "the hammers never got a reply")

        r = await _swap(client, supervisor)
        assert r.json()["ok"] is True, f"{r.text}\n{_diagnosis()}"

        # Past the flip, and past the old worker's death: it retires after
        # the flip, and a request routed to it as it went is the drop this
        # test exists to catch. A fixed 1.5 s was a guess at how long that
        # takes, and a loaded machine can take longer. The retiring worker is
        # in `draining` until it has stopped.
        async def retired() -> bool:
            status = (await client.get(f"{supervisor}/_sup/status")).json()
            return not status["draining"]

        await _until(retired, "the old worker never finished retiring")
        await _until(lambda: len(pids) >= 2, "the new worker never served a request")
        await asyncio.sleep(0.5)  # and a little traffic with only the new worker there
        stop.set()
        await asyncio.gather(*hammers)

    assert not failures, f"{len(failures)} dropped request(s); first few: {failures[:5]}"
    assert len(pids) >= 2, f"never observed the new worker serving traffic (pids={pids})"


async def test_inflight_request_survives_the_swap(supervisor):
    """A slow request started before the flip must still complete.

    This is what the drain phase buys us, and it is the difference between
    'restarts fast' and 'does not interrupt you mid-sentence'.

    What this cannot see, found when mutating the supervisor to skip its drain
    (`_retire` without `worker.drain`): that mutation passes this test, at 3 s
    and at 12 s alike, because a stopped worker finishes the requests it has
    (uvicorn's graceful shutdown) and `shutdown` waits 10 s for it to. A drain
    is only observable by a request that outlasts that, which is too long a
    wait for a test that runs in every gate. Left as it was rather than
    pretended otherwise.
    """
    async with _client(60.0) as client:
        slow = asyncio.create_task(
            client.get(f"{supervisor}/echo", params={"msg": "slow", "delay": 3.0})
        )

        # Let it land on the old worker: the supervisor counts it in flight.
        async def landed() -> bool:
            status = (await client.get(f"{supervisor}/_sup/status")).json()
            return status["active"]["inflight"] >= 1

        await _until(landed, "the slow request never reached the worker")

        r = await _swap(client, supervisor)
        assert r.json()["ok"] is True, f"{r.text}\n{_diagnosis()}"

        response = await slow
        assert response.status_code == 200, "in-flight request was killed by the swap"
        assert response.json()["echo"] == "slow"


async def test_status_reports_frozen_core_intact(supervisor):
    async with _client(10.0) as client:
        r = await client.get(f"{supervisor}/_sup/status")
        assert r.status_code == 200
        body = r.json()
        assert body["frozen_core_violations"] == [], body["frozen_core_violations"]
        assert body["active"]["alive"] is True
        # Named by when it started, not by a commit: the supervisor knows
        # nothing of git (test_crash_loops.py).
        assert body["active"]["worker"] == body["history"][-1], body


async def test_websocket_proxies_to_worker(supervisor):
    """Bidirectional proxying, using the protocol's own liveness probe.

    ping/pong rather than a chat turn on purpose: this asserts the transport,
    and a gate test must not depend on a live model or spend money.
    """
    import websockets

    ws_url = supervisor.replace("http://", "ws://") + "/ws"
    async with websockets.connect(ws_url, subprotocols=_subprotocols()) as ws:
        # The protocol a browser needs answered, never the token's.
        assert ws.subprotocol == "steve"
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello"

        await ws.send(json.dumps({"type": "ping"}))
        pong = json.loads(await ws.recv())
        assert pong["type"] == "pong"
        assert isinstance(pong["pid"], int)


async def test_worker_reports_its_capabilities(supervisor):
    """/status is what the UI and the model both read to know what works."""
    async with _client(30.0) as client:
        r = await client.get(f"{supervisor}/status")
        assert r.status_code == 200
        body = r.json()
        # Every role must be resolvable, or a turn will fail at routing time.
        assert set(body["models"]) == {
            "triage", "converse", "reason", "escalate", "bulk"
        }
        # The sandbox may legitimately be down; it must still report itself.
        assert "available" in body["sandbox"]
        assert "total_usd" in body["cost"]


@pytest.fixture
def restorable_worker_source(sandbox_repo):
    """Lets a test corrupt agent/server.py -- the sandbox's copy, never the
    live one -- and puts it back for the tests that swap after it."""
    path = sandbox_repo / "agent" / "server.py"
    original = path.read_bytes()
    try:
        yield path
    finally:
        path.write_bytes(original)


async def test_broken_revision_is_refused_and_service_continues(
    supervisor, restorable_worker_source
):
    """The failure mode self-modification actually produces.

    Code that checks out fine but dies at import. The supervisor must catch it
    during the health check and never move traffic -- a failed upgrade has to
    be a no-op, not an outage.
    """
    async with _client(120.0) as client:
        before = await _worker_pid(client, supervisor)

        restorable_worker_source.write_bytes(
            b"raise RuntimeError('deliberately broken worker')\n"
        )

        r = await _swap(client, supervisor)
        assert r.status_code == 503, "supervisor accepted a revision that cannot start"
        body = r.json()
        assert body["ok"] is False
        assert body["stage"] == "health", f"failed at the wrong stage: {body}"

        probe = await client.get(f"{supervisor}/echo", params={"msg": "still-alive"})
        assert probe.status_code == 200, "service went down during a failed upgrade"
        assert probe.json()["pid"] == before, "a failed swap disturbed the running worker"

    # The fixture restores the good source on teardown; the next test's swap
    # exercises recovery.


async def test_orphaned_workers_are_reaped_on_startup(
    supervisor, sandbox_repo, tmp_path, monkeypatch
):
    """A supervisor killed with SIGKILL leaves its workers running.

    They keep a database handle open and accumulate silently -- eight built up
    during one afternoon of testing. Startup must clean up any worker whose
    owner is gone, while leaving alone the workers of anything still running
    -- a concurrently-running supervisor, or a test or harness that started
    its own. It used to reap every worker whose parent was not a supervisor,
    and run side by side one test's supervisor killed another test's worker
    (6 October 2026).
    """
    import subprocess
    import sys

    from core.worker_handle import owner_token, reap_orphans, spawn_worker

    # This module is imported from the live checkout, so its log directory
    # is the live agent's -- where the orphan's log would land, and where
    # the pruning that makes room for it would delete the live agent's.
    monkeypatch.setattr("core.paths.logs_dir", lambda: tmp_path / "logs")
    # These workers start in the copy and read its run file, so they want
    # the module's supervisor's token, which the handles here send as the
    # supervisor's own do.
    monkeypatch.setattr("core.localapi._accepted", _TOKEN)

    # The orphan is stamped with this test's data folder (`DATA_ENV`), and
    # an orphan whose install's folder is gone is anyone's to reap. The
    # fences leave the folder unmade, so any supervisor starting elsewhere
    # on the machine -- an e2e backend, the real app a screen test opens --
    # killed this orphan while it started, with exit code 15 (19 and 21
    # October 2026). Made, it is this test's alone to reap.
    from core import paths

    paths.data_dir().mkdir(parents=True, exist_ok=True)
    # An owner that is gone: a process that started a worker and then died
    # without stopping it, as a supervisor killed by Task Manager does.
    gone = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    dead_owner = owner_token(gone.pid)
    gone.kill()
    gone.wait()
    orphan = spawn_worker("test-orphan", cwd=sandbox_repo, env=_test_env(), owner=dead_owner)
    # And one this test owns, whose parent is not a supervisor either: its
    # owner is alive, so it is not anyone else's to reap.
    kept = spawn_worker("test-owned", cwd=sandbox_repo, env=_test_env())
    try:
        # The same limit as the supervisor's start: these two start together,
        # beside whatever else the machine is running, and 30 s was not a
        # measured number. A failure carries each one's last words: on 18
        # October the orphan exited while starting, under the check's load,
        # and its log went with the test's folder, so why was never known.
        assert await orphan.wait_healthy(timeout=STARTUP_TIMEOUT), (
            f"test orphan never started: {orphan.last_words()}")
        assert await kept.wait_healthy(timeout=STARTUP_TIMEOUT), (
            f"test-owned worker never started: {kept.last_words()}")

        # Another install's start, whose data folder is elsewhere, leaves it.
        with monkeypatch.context() as other:
            other.setattr("core.paths.data_dir", lambda: tmp_path)
            assert orphan.proc.pid not in reap_orphans(), "another install reaped this one's orphan"

        killed = reap_orphans()
        assert orphan.proc.pid in killed, f"orphan {orphan.proc.pid} not reaped ({killed})"
        assert kept.proc.pid not in killed and kept.alive, "reaped a worker whose owner lives"

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and orphan.alive:
            await asyncio.sleep(0.1)
        assert not orphan.alive
    finally:
        await kept.shutdown()
        await orphan.shutdown()

    # The live supervisor's own worker must have survived.
    async with _client(15.0) as client:
        health = await client.get(f"{supervisor}/_sup/health")
        assert health.json()["ok"] is True, "reaping killed a supervised worker"


# --------------------------------------------------------------------------
# A dead worker comes back
# --------------------------------------------------------------------------


async def test_a_killed_worker_is_replaced_without_being_asked(supervisor):
    """The failure the user reported as "it's offline".

    The supervisor started a worker at boot, swapped on request, and did
    nothing else. So a worker that died for any reason left `/health`
    answering `ok: false` for as long as nobody looked -- and the agent was
    gone from every channel at once, Signal included.

    Keeping a worker alive is the one job this process has. A crash is a
    thing to recover from, not a thing to report.

    Kills the real process rather than faking it: the whole question is
    whether the supervisor notices a death it did not cause.
    """
    import psutil

    from core.supervisor import WATCH_INTERVAL_S

    async with _client(10.0) as client:
        before = (await client.get(f"{supervisor}/_sup/status")).json()["active"]
        psutil.Process(
            next(
                p.pid
                for p in psutil.process_iter(["pid", "cmdline"])
                if f"--port {before['port']}"
                in " ".join(p.info["cmdline"] or []).replace("  ", " ")
            )
        ).kill()

        deadline = time.monotonic() + WATCH_INTERVAL_S * 4 + STARTUP_TIMEOUT
        recovered = None
        while time.monotonic() < deadline:
            # Every two seconds, it was: up to two lost after the recovery.
            await asyncio.sleep(0.2)
            health = (await client.get(f"{supervisor}/_sup/health")).json()
            if health.get("ok"):
                recovered = health
                break

        # Captured before asserting, so a failure says what the supervisor
        # thought was happening rather than only that it took too long.
        status = (await client.get(f"{supervisor}/_sup/status")).json()

    assert recovered is not None, (
        f"the supervisor never replaced the dead worker; status: {status}\n"
        f"{_diagnosis()}"
    )
    assert recovered["worker_port"] != before["port"], "same port; nothing restarted"


async def test_the_watchdog_leaves_a_swap_alone(supervisor):
    """A swap deliberately has no active worker for a moment. A watchdog
    that raced it would start a third one and leave an orphan behind.

    Asked of the running supervisor. This test used to begin by checking
    `swap_lock` on `core.supervisor` imported into pytest's own process -- a
    lock no supervisor under test ever touches, so it could not fail."""
    async with _client(60.0) as client:
        result = (await _swap(client, supervisor)).json()
        assert result["ok"], f"swap failed: {result}\n{_diagnosis()}"
        health = (await client.get(f"{supervisor}/_sup/health")).json()
        status = (await client.get(f"{supervisor}/_sup/status")).json()

    assert health["ok"]
    assert status["active"]["port"] == result["port"], "the swap's worker is not the one serving"
    # The one it replaced, draining -- and nothing else.
    assert len(status["draining"]) <= 1, f"a third worker appeared: {status['draining']}"


# --------------------------------------------------------------------------
# Nobody but this machine's own clients -- see core/localonly.py
# --------------------------------------------------------------------------


async def test_a_web_page_cannot_drive_the_supervisor(supervisor):
    async with _client(15.0) as client:
        foreign = await client.post(
            f"{supervisor}/actions/decide",
            content=b'{"id": 1, "approve": true}',
            headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
        )
        rebound = await client.get(
            f"{supervisor}/echo", params={"msg": "x"},
            headers={"Host": "evil.example"},
        )
        tray = await client.get(
            f"{supervisor}/echo", params={"msg": "x"},
            headers={"Origin": "http://tauri.localhost"},
        )
        plain = await client.get(f"{supervisor}/echo", params={"msg": "x"})

    assert foreign.status_code == 403, "a foreign page's POST reached the agent"
    assert rebound.status_code == 403, "a rebound hostname reached the agent"
    assert tray.status_code == 200
    assert plain.status_code == 200


async def test_a_web_page_cannot_open_the_agents_websocket(supervisor):
    """CORS never covered websockets. This is the one that let any page chat
    with the agent as the user and read every answer."""
    # `InvalidStatus` by name: `websockets.exceptions` is a submodule, there
    # as an attribute only once something in the process has imported it.
    # On 8 October 2026 the worker this test landed on had not, and it
    # failed with AttributeError before connecting to anything.
    import websockets
    from websockets.exceptions import InvalidStatus

    ws_url = supervisor.replace("http://", "ws://") + "/ws"
    # Even holding the token: a page is refused by where it is from.
    with pytest.raises(InvalidStatus) as refused:
        async with websockets.connect(ws_url, origin="https://evil.example",
                                      subprotocols=_subprotocols()):
            pass
    assert refused.value.response.status_code == 403

    async with websockets.connect(ws_url, origin="http://tauri.localhost",
                                  subprotocols=_subprotocols()) as ws:
        assert (await ws.recv())  # the hello frame: the app still gets in


async def test_the_workers_control_routes_are_not_reachable_through_the_proxy(supervisor):
    """`/_shutdown`, `/_activate` and `/_retire` are the supervisor's to call
    on a worker's own port. Through the proxy, `/_shutdown` from anything
    local left the supervisor serving a worker that had closed its server."""
    async with _client(15.0) as client:
        before = await _worker_pid(client, supervisor)
        for route in ("_shutdown", "_activate", "_retire"):
            r = await client.post(f"{supervisor}/{route}")
            assert r.status_code == 404, f"/{route} reached the worker"
        assert await _worker_pid(client, supervisor) == before


async def test_a_worker_on_its_own_port_refuses_a_web_page_too(supervisor):
    """The worker's port is reachable directly, skipping the supervisor."""
    async with _client(15.0) as client:
        port = (await client.get(f"{supervisor}/_sup/health")).json()["worker_port"]
        direct = await client.get(
            f"http://127.0.0.1:{port}/echo", params={"msg": "x"},
            headers={"Origin": "https://evil.example"},
        )
    assert direct.status_code == 403


# --------------------------------------------------------------------------
# Nobody without this start's token -- see core/localapi.py
# --------------------------------------------------------------------------


async def test_a_request_without_this_starts_token_is_refused_by_the_supervisor_and_its_worker(
        supervisor):
    """Every program of every Windows user on this PC reaches 127.0.0.1.
    Until 13 October 2026 one that sent no Origin was let in, and could
    answer a card through `/actions/decide` as if it were the user. The
    token stops it, on the supervisor's port and on the worker's own."""
    async with _client(15.0) as client:
        port = (await client.get(f"{supervisor}/_sup/health")).json()["worker_port"]
    async with httpx.AsyncClient(timeout=15.0, verify=localapi.tls()) as stranger:
        for url in (f"{supervisor}/echo", f"{supervisor}/_sup/status",
                    f"http://127.0.0.1:{port}/echo"):
            none = await stranger.get(url, params={"msg": "x"})
            wrong = await stranger.get(url, params={"msg": "x"},
                                       headers={"Authorization": "Bearer " + "0" * 64})
            assert (none.status_code, wrong.status_code) == (401, 401), url


async def test_the_agents_websocket_needs_the_token_too(supervisor):
    """The socket a chat runs over: the app's own origin is not enough."""
    import websockets
    from websockets.exceptions import InvalidStatus

    ws_url = supervisor.replace("http://", "ws://") + "/ws"
    with pytest.raises(InvalidStatus) as refused:
        async with websockets.connect(ws_url, origin="http://tauri.localhost",
                                      subprotocols=["steve"]):
            pass
    assert refused.value.response.status_code == 403


async def test_the_supervisor_proves_itself_for_the_port_it_listens_on(supervisor):
    """What the app asks before it sends the token anywhere
    (ui/src-tauri/src/lib.rs): answered without the token, and for this port
    alone, so a program squatting on a stale port that relays the nonce here
    gets back a proof that does not match its own."""
    import secrets

    nonce = secrets.token_hex(32)
    port = int(supervisor.rsplit(":", 1)[1])
    async with httpx.AsyncClient(timeout=15.0, verify=localapi.tls()) as stranger:
        answered = await stranger.post(f"{supervisor}/_sup/prove", json={"nonce": nonce})
        unfair = await stranger.post(f"{supervisor}/_sup/prove", json={"nonce": "x"})

    assert answered.status_code == 200
    assert answered.json() == {"proof": localapi.proof(_TOKEN, nonce, port)}
    assert localapi.proof(_TOKEN, nonce, port + 1) != answered.json()["proof"]
    assert unfair.status_code == 400


def test_this_machines_tools_find_the_supervisor_through_its_run_file(
        supervisor, sandbox_repo, monkeypatch):
    """`python -m core.localapi` and the worker's calls to the supervisor
    (agent/upgrade.py) replaced `curl http://127.0.0.1:8790`: they read the
    run file, ask for the proof, and only then send the token."""
    monkeypatch.setattr(localapi, "run_file", lambda: _run_file(sandbox_repo))

    status, health = localapi.call("GET", "/_sup/health", timeout=15.0)

    assert status == 200 and health["ok"] is True


def test_the_code_under_test_is_the_sandboxs_not_the_live_checkout(sandbox_repo):
    """The isolation only holds if `-m core.supervisor` started in the copy
    imports the copy. The venv's editable install maps these packages to
    the live checkout, and if it took precedence over the working directory
    every test here would quietly be running -- and writing -- there again."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import core.supervisor, core.paths, agent.server; "
         "print(core.supervisor.__file__); print(core.paths.data_dir()); "
         "print(agent.server.__file__)"],
        cwd=str(sandbox_repo), env=_test_env(), capture_output=True, text=True, check=True,
    ).stdout.split("\n")

    for line in filter(None, out):
        assert Path(line).resolve().is_relative_to(sandbox_repo.resolve()), line


def test_the_sandbox_is_a_faithful_copy(sandbox_repo):
    """What runs is what is on disk, uncommitted changes included, and the
    copy's git is its own -- a checkout in it cannot move the live HEAD."""
    for rel in ("core/supervisor.py", "tests/gate/test_zero_downtime.py"):
        assert (sandbox_repo / rel).read_bytes() == (REPO_ROOT / rel).read_bytes(), rel
    server = "agent/server.py"
    assert (sandbox_repo / server).read_bytes() == _with_echo((REPO_ROOT / server).read_bytes())
    top = _git(sandbox_repo, "rev-parse", "--show-toplevel").strip()
    assert Path(top).resolve() == sandbox_repo.resolve()


def test_the_test_workers_cannot_touch_the_real_world():
    """The guard on the guard. Every process this module starts inherits
    `_test_env()`, and that is only worth anything while it keeps saying
    what it says."""
    env = _test_env()

    assert env.get("STEVE_INERT") == "1"


async def test_an_inert_worker_starts_nothing_in_the_background(tmp_path, monkeypatch):
    """The switch itself. An inert worker must not run the job queue --
    it shares the real database, so it could claim and half-execute a real
    approved action -- and must start none of the loops that reach mail,
    Signal or the wake alarm."""
    import agent.server as server
    from agent.worker import background, links
    from core import db

    started: list[str] = []

    def record(name):
        async def loop():
            started.append(name)
        return loop

    for home, name in (
        (links, "warm_voice"),
        (background, "build_sandbox"), (background, "build_browser"),
        (background, "reembed"), (background, "reflection_sweep"),
        (background, "night_loop"), (background, "attention_loop"),
        (background, "briefing_loop"),
        (background, "reminder_loop"), (background, "action_sweep"),
        (background, "wake_loop"),
    ):
        monkeypatch.setattr(home, name, record(name))
    real_open = db.open_store
    monkeypatch.setattr(db, "open_store", lambda *a, **k: real_open(tmp_path / "x.db"))
    monkeypatch.setattr(server.jobs, "JobWorker", lambda *a, **k: pytest.fail(
        "an inert worker built a job queue"))
    monkeypatch.setenv("STEVE_INERT", "1")

    async with server.lifespan(server.app):
        await asyncio.sleep(0.05)

    assert started == [], f"an inert worker started {started}"



def test_the_supervisor_under_test_never_writes_to_a_pipe():
    """The failure it prevents looks like load, which is why it is pinned.
    A pipe nobody reads fills, the next write blocks, and the event loop
    freezes with it -- but only once enough has been logged, which here
    meant only in the full suite, and only after an unrelated change made
    the supervisor chattier."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    fixture = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "supervisor"
    )
    code = "\n".join(ast.unparse(n) for n in fixture.body[1:])  # past the docstring

    assert "PIPE" not in code


# A stand-in for a supervisor: it starts a worker that sleeps (in the working
# directory it is told, or its own), says the worker's pid, and then either
# waits or exits, leaving the worker without a parent.
_STAND_IN = (
    "import subprocess, sys, time; "
    "cwd = sys.argv[1] or None; leave = sys.argv[2] == 'leave'; "
    "worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], cwd=cwd); "
    "print(worker.pid, flush=True); "
    "time.sleep(0.5 if leave else 120)"
)


def _stand_in(repo: Path, worker_cwd: str, then: str):
    """A running stand-in for the module's supervisor, and the pid of the
    sleeping worker it started."""
    proc = subprocess.Popen([sys.executable, "-c", _STAND_IN, worker_cwd, then],
                            cwd=str(repo), stdout=subprocess.PIPE, text=True)
    return proc, int(proc.stdout.readline())


def _gone(pid: int) -> bool:
    import psutil

    if not psutil.pid_exists(pid):
        return True
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_stopping_the_supervisor_stops_the_workers_it_started(tmp_path):
    """The fixture used to end the supervisor with `terminate()` alone, which
    on Windows is TerminateProcess: the supervisor died and its worker -- 583
    MiB of committed memory -- did not. Every pytest worker that had finished
    with this module left one running, until a later supervisor's start reaped
    it; in the coding sandbox those counted against the 6 GiB its whole tree
    may commit, and a gate run that reached the limit failed the start of the
    next worker (upgrade #89). The worker here has another working directory
    than the copy's, so only being the supervisor's child finds it."""
    supervisor_stand_in, worker_pid = _stand_in(tmp_path, str(Path(sys.executable).parent), "wait")
    try:
        assert not _gone(worker_pid)

        _stop_everything(supervisor_stand_in, tmp_path)

        assert supervisor_stand_in.poll() is not None, "the supervisor was left running"
        assert _gone(worker_pid), "a worker outlived the supervisor that started it"
    finally:
        supervisor_stand_in.stdout.close()
        _stop_everything(supervisor_stand_in, tmp_path)


def test_a_worker_whose_supervisor_is_already_gone_is_stopped_by_where_it_runs(tmp_path):
    """A swap that starts a worker as the supervisor is being stopped, or a
    supervisor that died first, leaves a worker with no parent to be found
    through. Everything started from the copy runs with the copy as its
    working directory, so that is what names it."""
    supervisor_stand_in, worker_pid = _stand_in(tmp_path, "", "leave")
    try:
        supervisor_stand_in.wait(timeout=30)
        assert not _gone(worker_pid), "the worker should outlive its supervisor here"

        _stop_everything(supervisor_stand_in, tmp_path)

        assert _gone(worker_pid), "an ownerless worker in the copy was left running"
    finally:
        supervisor_stand_in.stdout.close()
        _stop_everything(supervisor_stand_in, tmp_path)


# --------------------------------------------------------------------------
# The app's Quit -- see ui/src-tauri/src/backend.rs
# --------------------------------------------------------------------------

# How long the app waits for the supervisor to stop before its job object
# ends what is left (`STOP_WAIT` in ui/src-tauri/src/backend.rs).
APP_STOP_WAIT = 10.0


def test_asked_to_stop_the_supervisor_stops_its_workers_withdraws_its_run_file_and_exits(
        sandbox_repo, tmp_path):
    """The app's Quit, Stop and Restart ask this, and give it ten seconds
    before the job object ends whatever is left. A stop that only looked
    done would leave every Quit ending the workers mid-turn, and a run file
    naming a port that something else may take next.

    Its own supervisor, in a data folder of its own: the module's must keep
    serving, and two supervisors in one folder would share one run file."""
    import psutil

    env = _test_env()
    env["STEVE_DATA_DIR"] = str(tmp_path / "data")
    run_file = tmp_path / "data" / "run" / "backend.json"
    log = tmp_path / "supervisor.log"
    with log.open("w", encoding="utf-8", errors="replace") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "core.supervisor", "--log-level", "warning"],
            cwd=str(sandbox_repo), env=env, stdout=log_handle, stderr=subprocess.STDOUT)
    family: list = []
    try:
        served = _serving(proc, run_file)
        assert served, "it never served:\n" + log.read_text(encoding="utf-8", errors="replace")
        base, token = served
        family = psutil.Process(proc.pid).children(recursive=True)
        assert family, "no worker was started"

        refused = httpx.post(f"{base}/_sup/stop", timeout=5.0, verify=localapi.tls())
        assert refused.status_code == 401 and proc.poll() is None

        asked = time.monotonic()
        answer = httpx.post(f"{base}/_sup/stop", timeout=5.0, verify=localapi.tls(),
                            headers={"Authorization": f"Bearer {token}"})
        assert answer.status_code == 200
        code = proc.wait(timeout=APP_STOP_WAIT)
        took = time.monotonic() - asked

        assert code == 0, f"it exited {code} after {took:.1f} s"
        assert not run_file.exists(), "its run file was left behind"
        _, alive = psutil.wait_procs(family, timeout=1)
        assert not alive, f"left running: {[p.pid for p in alive]}"
    finally:
        # Only its own: `_stop_everything` ends everything run from the copy,
        # the module's supervisor's workers included. The supervisor first,
        # so its watchdog cannot replace a worker that is killed.
        with contextlib.suppress(psutil.Error):
            family += psutil.Process(proc.pid).children(recursive=True)
        if proc.poll() is None:
            proc.kill()
        for victim in family:
            with contextlib.suppress(psutil.Error):
                victim.kill()
        psutil.wait_procs(family, timeout=10)
