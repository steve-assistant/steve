"""How long a swap waits for a new worker, and who can change it.

A swap that misses the window is final: the new worker is thrown away, the
old one keeps serving, and `adopt` puts a merged change back. It was always
thirty seconds, written into the signature, so the gate's own supervisor --
which swaps three at a time under `pytest -n 3` in a sandbox that starts every
process cold -- had no way to wait longer, and upgrade #89 (8 October 2026)
was rejected on "new worker never became healthy". The knob below lets that
supervisor be told; these tests hold the other half of the bargain, that
nobody who does not tell it gets anything but the default -- ninety seconds
since 8 October 2026, the user's decision, after a cold start took 22 s.
"""

from __future__ import annotations

import asyncio
import io
import os
import socket

import pytest

from core import worker_handle
from core.worker_handle import (
    DEFAULT_START_TIMEOUT_S,
    START_TIMEOUT_ENV,
    WorkerHandle,
    start_timeout,
)


class _Alive:
    """A worker's process, as far as `wait_healthy` asks: still running, and
    answering nothing, because nothing listens on its port."""

    def poll(self):
        return None


def _unheard_port() -> int:
    """A port nothing listens on, so each health check is refused at once."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _handle() -> WorkerHandle:
    return WorkerHandle(port=_unheard_port(), proc=_Alive(), name="w1-120000")


def test_production_waits_ninety_seconds_because_it_sets_nothing(monkeypatch):
    """The behaviour that must not move: with the variable absent, a swap
    waits exactly as it did before the variable existed."""
    monkeypatch.delenv(START_TIMEOUT_ENV, raising=False)

    assert start_timeout() == 90.0 == DEFAULT_START_TIMEOUT_S


def test_the_environment_can_lengthen_the_wait(monkeypatch):
    monkeypatch.setenv(START_TIMEOUT_ENV, "120")

    assert start_timeout() == 120.0


@pytest.mark.parametrize("junk", ["", "soon", "0", "-5", "nan"])
def test_a_value_that_is_not_a_positive_number_leaves_the_default(monkeypatch, junk):
    """A typo in an environment variable must not turn a swap's wait into
    zero seconds -- every swap would then fail -- or into an error."""
    monkeypatch.setenv(START_TIMEOUT_ENV, junk)

    assert start_timeout() == DEFAULT_START_TIMEOUT_S


async def test_a_wait_with_no_limit_named_takes_the_one_in_the_environment(monkeypatch):
    """Through `wait_healthy` itself, since the supervisor calls it with no
    argument: the variable is worth nothing if the call does not read it.
    Bounded by "not the thirty seconds" rather than by a stopwatch."""
    monkeypatch.setenv(START_TIMEOUT_ENV, "0.3")

    assert await asyncio.wait_for(_handle().wait_healthy(), timeout=15) is False


async def test_a_limit_a_caller_names_beats_the_environment(monkeypatch):
    """A wait that names its own limit (the gate's orphan test does) keeps
    it: an environment variable for the swap must not stretch it."""
    monkeypatch.setenv(START_TIMEOUT_ENV, "3600")

    assert await asyncio.wait_for(_handle().wait_healthy(timeout=0.3), timeout=15) is False


def test_a_worker_is_started_with_one_openblas_thread_unless_told_otherwise(monkeypatch,
                                                                             tmp_path):
    """583 MiB committed by an idle worker, 100 MiB with one OpenBLAS thread,
    and a swap holds two or three at once (8 October 2026)."""
    started = []

    class _Popen:
        def __init__(self, argv, **kwargs):
            started.append(kwargs["env"])
            # What spawn_worker shares the worker's socket with.
            self.pid, self.stdin = os.getpid(), io.BytesIO()

    monkeypatch.setattr(worker_handle.subprocess, "Popen", _Popen)
    monkeypatch.setattr("core.paths.logs_dir", lambda: tmp_path)

    worker_handle.spawn_worker("a" * 40, env={})
    worker_handle.spawn_worker("a" * 40, env={"OPENBLAS_NUM_THREADS": "4"})

    assert [env["OPENBLAS_NUM_THREADS"] for env in started] == ["1", "4"]


class _Dead:
    def poll(self):
        return 1


def test_a_worker_that_never_became_healthy_says_why(tmp_path):
    """Upgrade #89's cause, OpenBLAS out of memory at import, sat in a log
    the swap's answer never showed."""
    log = (tmp_path / "w.log").open("w", encoding="utf-8")
    log.write("starting\nOpenBLAS error: Memory allocation still failed after 10 retries\n")
    log.flush()
    handle = WorkerHandle(port=1, proc=_Dead(), name="w1-120000", log_file=log)

    said = handle.last_words()
    log.close()

    assert said.startswith("it exited with code 1") and "OpenBLAS error" in said
