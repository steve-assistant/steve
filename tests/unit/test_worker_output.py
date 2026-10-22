"""A worker must never be able to block on its own logging.

The failure this file exists to prevent took the whole agent down and
reported nothing, which is the worst combination available.

`spawn_worker` gave the child `stdout=PIPE` and nothing ever read it. A pipe
buffer is roughly 64KB on Windows. Once a worker had written that much --
which took a busy afternoon of Google warnings, COM tracebacks and a
reflection sweep throwing every five minutes -- its next log line blocked
forever. Logging happens on the event loop, so the loop stopped: `/health`
unanswered, every proxied request hanging, Signal timing out, and no
error anywhere, because the process that would
have reported the problem was stuck inside the act of reporting.

The tell was that it looked like a *Signal* bug from the phone. It was not.

So the invariant is narrow and absolute: the child's output goes somewhere
that cannot fill up. A file can; a pipe nobody drains cannot be allowed.
"""

from __future__ import annotations

import io
import os
import subprocess

import pytest

from core import worker_handle


@pytest.fixture(autouse=True)
def logs_in_a_scratch_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("core.paths.logs_dir", lambda: tmp_path / "logs")


@pytest.fixture
def spawned(monkeypatch):
    """Capture the Popen call without starting anything."""
    seen: dict = {}

    class _Proc:
        # What spawn_worker shares the worker's socket with.
        pid, stdin = os.getpid(), io.BytesIO()

        def poll(self):
            return None

    def fake_popen(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(worker_handle.subprocess, "Popen", fake_popen)
    return seen


def test_the_worker_is_never_given_a_pipe_nobody_reads(spawned):
    """The bug itself. Not "prefer a file" -- a PIPE here is a deadlock with
    a delay fuse, and the delay is however long it takes to log 64KB."""
    worker_handle.spawn_worker("abc1234")

    assert spawned["kwargs"]["stdout"] is not subprocess.PIPE
    assert spawned["kwargs"]["stderr"] is not subprocess.PIPE


def test_the_output_goes_to_a_file_that_can_be_read_afterwards(spawned, tmp_path):
    """Half the cost of the original bug was having no worker log at all to
    look at. The stack had to be pulled out of the frozen process."""
    handle = worker_handle.spawn_worker("abc1234")

    sink = spawned["kwargs"]["stdout"]
    assert hasattr(sink, "write")
    sink.write("hello from the worker\n")
    sink.flush()

    written = list((tmp_path / "logs").glob("worker-*.log"))
    assert len(written) == 1
    assert "abc1234" in written[0].name
    assert "hello from the worker" in written[0].read_text(encoding="utf-8")
    assert handle.log_file is sink, "the handle must hold the file open"


def test_stderr_is_folded_into_the_same_file(spawned):
    """A traceback split from the log line that preceded it is most of the
    way to useless."""
    worker_handle.spawn_worker("abc1234")

    assert spawned["kwargs"]["stderr"] is subprocess.STDOUT


def test_old_logs_are_pruned(spawned, tmp_path):
    """The supervisor swaps several times an hour during a working session.
    Unbounded logs would be a slow disk leak nobody notices."""
    monkey = tmp_path / "logs"
    monkey.mkdir()
    for i in range(worker_handle.KEEP_LOGS + 5):
        (monkey / f"worker-2026010{i:02d}-000000-old-1.log").write_text("x")

    worker_handle.spawn_worker("abc1234")

    assert len(list(monkey.glob("worker-*.log"))) <= worker_handle.KEEP_LOGS + 1


def test_stopping_closes_the_file(spawned):
    """An open handle per swap is a leak, and on Windows it also keeps the
    file locked against the prune above."""
    handle = worker_handle.spawn_worker("abc1234")

    assert not handle.log_file.closed
    handle.log_file.close()
    assert handle.log_file.closed
