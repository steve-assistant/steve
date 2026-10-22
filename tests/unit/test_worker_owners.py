"""A worker is reaped only once whoever started it is gone.

`reap_orphans` runs at every supervisor start and in upgrade cleanup. It
killed any worker whose parent was not a supervisor -- which is also every
worker a test, the coding harness or another checkout's tooling starts -- so
run side by side, one test's supervisor killed another test's worker
(6 October 2026). Each worker now carries its owner (`OWNER_ENV`), and the
processes here are fakes: nothing real is looked at or killed.
"""

from __future__ import annotations

import io
import os

import psutil
import pytest

from core import paths, worker_handle
from core.worker_handle import DATA_ENV, OWNER_ENV, looked_after

PYTHON = r"C:\py\python.exe"
WORKER = [PYTHON, "-m", "agent.server", "--port", "5000"]
SUPERVISOR = [PYTHON, "-m", "core.supervisor", "--port", "8790"]
PYTEST = [PYTHON, "-m", "pytest"]


class _Proc:
    def __init__(self, pid, *, cmdline=(), parent=None, created=100.0, env=None,
                 env_unreadable=False):
        self.pid, self._cmdline, self._parent = pid, list(cmdline), parent
        self._created, self._env, self._unreadable = created, env or {}, env_unreadable

    def cmdline(self):
        return self._cmdline

    def create_time(self):
        return self._created

    def ppid(self):
        return self._parent

    def environ(self):
        if self._unreadable:
            raise psutil.AccessDenied(self.pid)
        return self._env


@pytest.fixture
def machine(monkeypatch):
    """Processes by pid, as psutil would find them."""
    procs: dict[int, _Proc] = {}

    def find(pid=None):
        if pid not in procs:
            raise psutil.NoSuchProcess(pid)
        return procs[pid]

    monkeypatch.setattr(worker_handle.psutil, "Process", find)
    return procs


def _owned_by(pid, created=100.0):
    return {OWNER_ENV: f"{pid}:{created:.3f}"}


def test_a_worker_whose_owner_lives_is_left_alone_whatever_its_parent(machine):
    machine[10] = _Proc(10, cmdline=PYTEST)
    worker = machine[11] = _Proc(11, cmdline=WORKER, parent=10, env=_owned_by(10))

    assert looked_after(worker)


def test_a_worker_whose_owner_has_gone_is_an_orphan(machine):
    machine[10] = _Proc(10, cmdline=SUPERVISOR)
    worker = machine[11] = _Proc(11, cmdline=WORKER, parent=10, env=_owned_by(99))

    assert not looked_after(worker), "its owner is gone, whoever its parent is"


def test_an_owners_pid_given_to_a_later_process_is_not_the_owner(machine):
    machine[10] = _Proc(10, cmdline=PYTEST, created=500.0)  # same pid, born later
    worker = machine[11] = _Proc(11, cmdline=WORKER, parent=10, env=_owned_by(10, 100.0))

    assert not looked_after(worker)


def test_the_inner_process_of_a_worker_goes_with_its_outer_one(machine):
    """The venv launcher and its interpreter share one command line; the
    inner one is judged by the outer's owner, never on its own."""
    machine[11] = _Proc(11, cmdline=WORKER, parent=1, env=_owned_by(99))
    inner = machine[12] = _Proc(12, cmdline=WORKER, parent=11, env=_owned_by(99))

    assert looked_after(inner)


@pytest.mark.parametrize(("parent", "env", "unreadable"), [
    (SUPERVISOR, {}, False),
    (PYTEST, {}, False),
    (SUPERVISOR, None, True),    # environment not readable
    (PYTEST, None, True),
    (PYTEST, {OWNER_ENV: "not-a-token"}, False),
])
def test_a_worker_without_a_readable_owner_is_never_reaped(machine, parent, env,
                                                           unreadable):
    """Such a worker cannot be told from another install's, so it is not
    Steve's to kill: a stray worker costs memory, another's its work."""
    machine[10] = _Proc(10, cmdline=parent)
    worker = machine[11] = _Proc(11, cmdline=WORKER, parent=10, env=env,
                                 env_unreadable=unreadable)

    assert looked_after(worker)


def test_the_personal_editions_worker_is_left_alone_when_its_owner_is_gone(machine):
    """It runs the same module as the same user, and records its owner
    under a variable of its own, which Steve never reads."""
    machine[10] = _Proc(10, cmdline=PYTEST)
    worker = machine[11] = _Proc(11, cmdline=WORKER, parent=10,
                                 env={"AGENT" + "_WORKER_OWNER": "99:100.000"})

    assert looked_after(worker)


def test_an_owner_that_cannot_be_looked_at_is_not_a_reason_to_kill(machine, monkeypatch):
    def denied(pid=None):
        if pid == 10:
            raise psutil.AccessDenied(pid)
        return machine[pid]

    machine[11] = _Proc(11, cmdline=WORKER, parent=1, env=_owned_by(10))
    machine[1] = _Proc(1, cmdline=PYTEST)
    monkeypatch.setattr(worker_handle.psutil, "Process", denied)

    assert looked_after(machine[11])


def test_a_spawned_worker_carries_its_owner_and_never_an_inherited_one(monkeypatch, tmp_path):
    started: list[dict] = []

    class _Popen:
        def __init__(self, argv, cwd=None, env=None, stdin=None, stdout=None, stderr=None):
            started.append(env)
            # What spawn_worker shares the worker's socket with.
            self.pid, self.stdin = os.getpid(), io.BytesIO()

    monkeypatch.setattr(worker_handle.subprocess, "Popen", _Popen)
    monkeypatch.setattr("core.paths.logs_dir", lambda: tmp_path)
    monkeypatch.setenv(OWNER_ENV, "1:1.000")  # as a worker's own environment has
    monkeypatch.setenv(DATA_ENV, str(tmp_path / "another"))

    worker_handle.spawn_worker("abc123")
    worker_handle.spawn_worker("abc123", owner="42:7.000")

    assert started[0][OWNER_ENV] == worker_handle.owner_token()
    assert started[1][OWNER_ENV] == "42:7.000"
    assert started[0][DATA_ENV] == started[1][DATA_ENV] == str(paths.data_dir())


@pytest.fixture
def installs(monkeypatch, tmp_path):
    """This install's data folder, and another's, both there."""
    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    ours.mkdir()
    theirs.mkdir()
    monkeypatch.setattr("core.paths.data_dir", lambda: ours)
    return ours, theirs


def test_another_installs_orphan_is_left_to_it_while_its_data_folder_is_there(machine,
                                                                               installs):
    """Every supervisor's start reaped every orphan, so another test's start
    killed the zero-downtime gate's own orphan before the gate could reap
    it (18 October 2026). It is that install's to reap."""
    _, theirs = installs
    worker = machine[11] = _Proc(11, cmdline=WORKER, parent=1,
                                 env={**_owned_by(99), DATA_ENV: str(theirs)})

    assert looked_after(worker)


def test_this_installs_orphan_is_reaped_however_its_folder_is_written(machine, installs):
    ours, _ = installs
    worker = machine[11] = _Proc(11, cmdline=WORKER, parent=1,
                                 env={**_owned_by(99), DATA_ENV: str(ours).upper() + "\\"})

    assert not looked_after(worker)


def test_an_orphan_whose_installs_folder_is_gone_is_reaped_by_any_install(machine, installs,
                                                                          tmp_path):
    """A finished test's folder is removed, and no later start would
    otherwise ever reap the workers it leaked."""
    worker = machine[11] = _Proc(11, cmdline=WORKER, parent=1,
                                 env={**_owned_by(99), DATA_ENV: str(tmp_path / "gone")})

    assert not looked_after(worker)


def test_another_installs_worker_whose_owner_lives_is_left_alone(machine, installs):
    machine[10] = _Proc(10, cmdline=PYTEST)
    _, theirs = installs
    worker = machine[11] = _Proc(11, cmdline=WORKER, parent=10,
                                 env={**_owned_by(10), DATA_ENV: str(theirs)})

    assert looked_after(worker)
