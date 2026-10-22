"""A worker's port is the supervisor's from before the worker starts until
after it stops.

It was a number: `free_port` bound a socket, read its port and closed it,
and the worker bound the port again once it had imported everything, 4 to
22 s later. All that time the supervisor polled the port with this start's
token. Another Windows account's program can read the TCP table, process
ids and all, see those polls waiting on a port no one holds, bind it first,
and be sent the token, which opens every route (step 11's audit). Now the
supervisor binds the port, so that no other socket may share it, and hands
that socket to the worker; no one else can bind the port while anything
might send to it.
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from core import worker_handle


class _Stdin:
    def __init__(self):
        self.written, self.closed = b"", False

    def write(self, data: bytes) -> None:
        self.written += data

    def close(self) -> None:
        self.closed = True


class _Worker:
    """A worker that never starts: what spawn_worker hands its socket to.
    Its process id is this test's, so the test can take the share itself."""

    def __init__(self):
        self.pid, self.stdin, self.code = os.getpid(), _Stdin(), None

    def poll(self):
        return self.code


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    monkeypatch.setattr("core.paths.logs_dir", lambda: tmp_path / "logs")
    workers: list[_Worker] = []

    def popen(args, **kwargs):
        workers.append(_Worker())
        return workers[-1]

    monkeypatch.setattr(worker_handle.subprocess, "Popen", popen)
    return workers


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def test_a_workers_port_is_held_from_its_spawn_until_its_shutdown(spawned):
    """The window the token was taken in: from the spawn to the worker's
    own bind. No program may bind the port in it, nor while the worker
    serves; once the worker has stopped, the port is let go."""
    handle = worker_handle.spawn_worker("w1")
    assert not _bindable(handle.port)
    [worker] = spawned
    socket.fromshare(worker.stdin.written).close()  # the worker took it, and died
    assert not _bindable(handle.port)
    worker.code = 0
    asyncio.run(handle.shutdown())
    assert _bindable(handle.port)


def test_the_worker_is_handed_the_listening_socket_on_its_port(spawned):
    """What the worker serves on (agent/server.main): the supervisor's own
    socket, on the port the handle names, already listening, so a poll waits
    in the backlog rather than being refused."""
    handle = worker_handle.spawn_worker("w1")
    [worker] = spawned
    assert worker.stdin.closed
    with socket.fromshare(worker.stdin.written) as theirs:
        assert theirs.getsockname() == ("127.0.0.1", handle.port)
        with socket.create_connection(("127.0.0.1", handle.port), timeout=2):
            pass
    worker.code = 0
    asyncio.run(handle.shutdown())


def test_a_worker_that_cannot_be_started_leaves_no_port_held(monkeypatch, tmp_path):
    monkeypatch.setattr("core.paths.logs_dir", lambda: tmp_path / "logs")
    held = []
    real = worker_handle.listening

    def listening():
        held.append(real())
        return held[-1]

    def popen(args, **kwargs):
        raise OSError("no python here")

    monkeypatch.setattr(worker_handle, "listening", listening)
    monkeypatch.setattr(worker_handle.subprocess, "Popen", popen)
    with pytest.raises(OSError):
        worker_handle.spawn_worker("w1")
    assert held[0].fileno() == -1
