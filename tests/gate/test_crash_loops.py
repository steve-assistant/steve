"""What the supervisor does when the worker will not stay up.

In the gate because this is the agent's last line of self-repair, and a
system that writes its own code must not be able to edit its own recovery
path into something weaker.

Once a worker dies -- a crash, a reboot, a laptop that ran out of battery
-- the supervisor starts another from the same code. If that one will not
start either, a plain restart every minute churns the disk all night on a
machine whose owner is asleep.

The bargain, one section per clause:

  **Back off.** A worker that has failed to start five times will not
  start on the sixth, and every attempt writes a log and prunes an old one.

  **Restart the code it was started with.** It used to roll the checkout
  back to the last commit that ran. That went with the merging (see
  `test_the_supervisor_knows_nothing_of_git`).

  **Keep the evidence.** A crash loop that erases its own logs is a crash
  loop nobody can diagnose.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from core import supervisor

# --------------------------------------------------------------------------
# Back off
# --------------------------------------------------------------------------


def test_failures_slow_the_retries_down():
    waits = [supervisor.backoff(n) for n in range(1, 10)]

    assert waits == sorted(waits)
    assert waits[0] >= supervisor.WATCH_BACKOFF_S
    assert waits[-1] == supervisor.MAX_BACKOFF_S


def test_the_backoff_has_a_ceiling_an_owner_would_accept():
    """Fifteen minutes. Long enough that a hard-down state is not churning
    the disk; short enough that fixing it by hand is noticed the same
    quarter hour."""
    assert supervisor.MAX_BACKOFF_S <= 15 * 60


class _Worker:
    """A worker as the watchdog's wait sees one: alive or not, and how long ago it started."""

    def __init__(self, *, alive: bool, age: float) -> None:
        self.alive = alive
        self.started_at = time.monotonic() - age


async def test_a_worker_that_ran_a_full_interval_and_died_is_seen_at_once(monkeypatch):
    """The watchdog looked every fifteen seconds, so a killed worker left the
    agent gone from every channel for up to that long. A death needs no
    health check to see, so the wait ends when the process does."""
    monkeypatch.setattr(supervisor, "DEATH_POLL_S", 0.01)
    monkeypatch.setattr(supervisor.state, "swap_lock", asyncio.Lock())
    monkeypatch.setattr(supervisor.state, "active",
                        _Worker(alive=False, age=supervisor.WATCH_INTERVAL_S))

    await asyncio.wait_for(supervisor._wait_out(60.0), timeout=5.0)


async def test_a_worker_that_dies_young_is_restarted_no_faster_than_the_interval(monkeypatch):
    """One that starts and dies within the interval, again and again, is a
    crash loop, and every restart writes a log and prunes an old one: it is
    left to the watchdog's own pace, as before deaths were seen at once."""
    monkeypatch.setattr(supervisor, "DEATH_POLL_S", 0.01)
    monkeypatch.setattr(supervisor.state, "swap_lock", asyncio.Lock())
    monkeypatch.setattr(supervisor.state, "active", _Worker(alive=False, age=0.0))
    started = time.monotonic()

    await supervisor._wait_out(0.3)

    assert time.monotonic() - started >= 0.3


async def test_a_worker_gone_during_a_swap_is_left_to_the_swap(monkeypatch):
    """A swap stops the old worker on purpose; a watchdog woken by that death
    would race it and start a third."""
    monkeypatch.setattr(supervisor, "DEATH_POLL_S", 0.01)
    monkeypatch.setattr(supervisor.state, "swap_lock", asyncio.Lock())
    monkeypatch.setattr(supervisor.state, "active",
                        _Worker(alive=False, age=supervisor.WATCH_INTERVAL_S))
    started = time.monotonic()

    async with supervisor.state.swap_lock:
        await supervisor._wait_out(0.3)

    assert time.monotonic() - started >= 0.3


# --------------------------------------------------------------------------
# Restart the code it was started with
# --------------------------------------------------------------------------


def test_the_supervisor_knows_nothing_of_git():
    """After three failed restarts it rolled HEAD back to the last commit
    that ran, and it put back a merge the canary caught; avoiding a rollback
    over a person's uncommitted work was most of its code. An installed
    Steve has no checkout to move, and Steve no longer merges its own
    changes, so a worker that will not start is started again, slower each
    time, from the same code."""
    import ast
    import inspect

    imported, strings = set(), set()
    for node in ast.walk(ast.parse(inspect.getsource(supervisor))):
        if isinstance(node, ast.ImportFrom):
            imported |= {node.module or "", *(f"{node.module}.{a.name}" for a in node.names)}
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)

    assert not {"core.gitops", "core.canary", "subprocess"} & imported, imported
    assert "git" not in strings


# --------------------------------------------------------------------------
# Keep the evidence
# --------------------------------------------------------------------------


def test_a_dead_workers_last_words_outlive_log_pruning(tmp_path, caplog):
    """Worker logs are kept by count, one per worker, so a loop of twenty
    restarts erases every log from before it began -- including the one
    that says why. The morning the agent was found down, whatever killed it
    had already been pruned away."""
    log_file = tmp_path / "worker.log"
    log_file.write_text(
        "".join(f"line {i}\n" for i in range(100)) + "RuntimeError: the reason\n",
        encoding="utf-8",
    )

    class Dead:
        name = "w3-120000"
        log_file = type("F", (), {"name": str(tmp_path / "worker.log")})()

    with caplog.at_level(logging.ERROR, logger="supervisor"):
        supervisor.record_death(Dead())

    assert "RuntimeError: the reason" in caplog.text
    assert "line 0\n" not in caplog.text, "copied the whole log, not the tail"


def test_a_worker_with_no_log_does_not_crash_the_watchdog(caplog):
    """The watchdog is the thing that must not fall over."""

    class NoLog:
        name = "w4-120000"

    supervisor.record_death(NoLog())


def test_a_silent_death_is_reported_as_one(tmp_path, caplog):
    """Workers log at warning and above, so a healthy one writes nothing.
    An empty log therefore means it was ended from outside -- which is a
    different investigation from a crash, and worth saying rather than
    printing "the last 0 lines"."""
    (tmp_path / "quiet.log").write_text("", encoding="utf-8")

    class Killed:
        name = "w5-120000"
        log_file = type("F", (), {"name": str(tmp_path / "quiet.log")})()

    with caplog.at_level(logging.ERROR, logger="supervisor"):
        supervisor.record_death(Killed())

    assert "ended from outside" in caplog.text


# --------------------------------------------------------------------------
# Noted for the failure ledger
# --------------------------------------------------------------------------

def _stack(title: str, frames: list[tuple[str, int, str]]) -> str:
    return f"{title} (most recent call first):\n" + "".join(
        f'  File "{path}", line {line} in {func}\n' for path, line, func in frames) + "\n"


_WAITING = [(r"C:\Python313\Lib\ssl.py", 1138, "read"),
            (r"C:\Python313\Lib\socket.py", 723, "readinto"),
            (r"C:\venv\Lib\site-packages\httplib2\__init__.py", 1427, "_conn_request"),
            (r"C:\Python313\Lib\threading.py", 1044, "_bootstrap_inner"),
            (r"C:\Python313\Lib\threading.py", 1015, "_bootstrap")]

# The shape of the first native crash faulthandler caught (30 September,
# 00:07): every thread's stack, with the one that faulted among the others
# and far from the end of the log.
FAULTED = (
    "2026-09-30 00:07:31,310 INFO httpx2: HTTP Request: POST https://api.openai.com/v1\n"
    "Windows fatal exception: access violation\n\n"
    + _stack("Thread 0x0000d7a0", _WAITING)
    + _stack("Current thread 0x000062dc", [
        (r"C:\Python313\Lib\ssl.py", 1140, "read"),
        (r"C:\venv\Lib\site-packages\httpx2\_api.py", 303, "post"),
        (r"C:\Steve\agent\attention\jev.py", 89, "_post"),
        (r"C:\Python313\Lib\threading.py", 1015, "_bootstrap")])
    + "".join(_stack(f"Thread 0x0000e8e{n}", _WAITING) for n in range(6))
)


class _Died:
    def __init__(self, log_file, code):
        self.name = "w2-120000"
        self.log_file = type("F", (), {"name": str(log_file)})()
        self.proc = type("P", (), {"poll": lambda _self: code})()


def test_a_worker_that_dies_is_noted_for_the_next_one_to_record(tmp_path, monkeypatch):
    """The supervisor keeps no database, so it leaves a note. Three workers
    died between 24 and 29 September and none of it reached the ledger."""
    monkeypatch.delenv("STEVE_INERT", raising=False)
    log_file = tmp_path / "worker.log"
    log_file.write_text(FAULTED, encoding="utf-8")

    supervisor.record_death(_Died(log_file, 3221225477))
    supervisor.record_death(_Died(log_file, None), how="unresponsive")

    lines = supervisor.deaths_path().read_text(encoding="utf-8").splitlines()
    notes = [json.loads(line) for line in lines]
    assert [(n["how"], n["exit"]) for n in notes] == [("gone", "0xC0000005"),
                                                       ("unresponsive", None)]
    assert notes[0]["worker"] == "w2-120000"


def test_a_native_fault_is_noted_by_the_thread_that_faulted_not_the_end_of_the_dump(
        tmp_path, monkeypatch, caplog):
    """faulthandler prints every thread, and the one that faulted can be
    anywhere among them. The first crash it caught was a 205-line dump with
    "Current thread" 111 lines from the end: a note of the last 30 lines
    was nine other threads waiting on sockets."""
    monkeypatch.delenv("STEVE_INERT", raising=False)
    log_file = tmp_path / "worker.log"
    log_file.write_text("INFO worker: took up duties\n" * 50 + FAULTED, encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="supervisor"):
        supervisor.record_death(_Died(log_file, 3221225477))

    tail = json.loads(supervisor.deaths_path().read_text(encoding="utf-8"))["tail"]
    assert "POST https://api.openai.com/v1" in tail and "Windows fatal exception" in tail
    assert "Current thread 0x000062dc" in tail and "jev.py" in tail
    assert "Thread 0x0000d7a0" not in tail and "httplib2" not in tail
    assert "jev.py" in caplog.text  # the supervisor's own account, too


def test_a_gate_supervisor_killing_its_workers_notes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("STEVE_INERT", "1")
    log_file = tmp_path / "worker.log"
    log_file.write_text(FAULTED, encoding="utf-8")

    supervisor.record_death(_Died(log_file, 1))

    assert not supervisor.deaths_path().exists()


def test_the_watchdog_says_how_a_worker_went():
    """Gone, or alive and refusing its health checks: different faults."""
    import ast
    import inspect

    calls = [ast.unparse(n) for n in ast.walk(ast.parse(inspect.getsource(supervisor.watchdog)))
             if isinstance(n, ast.Call) and ast.unparse(n.func) == "record_death"]

    assert "record_death(worker, how='unresponsive')" in calls
    assert "record_death(worker)" in calls
