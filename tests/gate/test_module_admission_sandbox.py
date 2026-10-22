"""A module's own tests run in a container of their own, with its files
read-only to them (06-modules.md, section 5; core/sandbox/testrun.py).

Run in the backend, a module's tests would run code from outside before
anyone approved it, with the user's files, network and secrets in reach.
They run instead on the module runtime, in a container made for the run
and deleted after: a test that writes the module's files or reaches the
network is refused there, and the verdict is pytest's. Nothing is skipped:
a runtime that is not built fails the test.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from core import paths
from core.sandbox import grants, profiles, testrun

pytestmark = [pytest.mark.processes, pytest.mark.appcontainer]

TOOL = "def double(n):\n    return n * 2\n"
PASSES = "from walls import tool\n\n\ndef test_double():\n    assert tool.double(21) == 42\n"
WALLS = '''import pathlib
import socket

import pytest


def test_its_own_files_are_read_only_to_it():
    with pytest.raises(PermissionError):
        (pathlib.Path(__file__).parents[1] / "tool.py").write_text("x = 1")


def test_the_network_is_closed():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=3)
'''
FAILS = "def test_wrong():\n    assert 1 == 2\n"
QUITS = "import os\n\n\ndef test_quits():\n    print('1 passed')\n    os._exit(0)\n"
WAITS = "import time\n\n\ndef test_forever():\n    time.sleep(60)\n"


def _module(folder, **tests):
    folder.mkdir(parents=True)
    (folder / "manifest.toml").write_text('id = "walls"\n', encoding="utf-8", newline="\n")
    (folder / "tool.py").write_text(TOOL, encoding="utf-8", newline="\n")
    (folder / "tests").mkdir()
    for name, text in tests.items():
        (folder / "tests" / f"{name}.py").write_text(text, encoding="utf-8", newline="\n")
    return folder


def _left(name: str) -> bool:
    """Whether the container `name` is still in Windows' list: its folder
    in AppData goes with it."""
    return (Path(os.environ["LOCALAPPDATA"]) / "Packages" / name).exists()


@pytest.fixture
def trials(monkeypatch):
    """The names of the containers the runs made."""
    made = []
    real = profiles.for_trial
    monkeypatch.setattr(profiles, "for_trial", lambda: made.append(real()) or made[-1])
    return made


def test_a_modules_tests_run_in_a_container_of_their_own_with_its_files_read_only(
        tmp_path, trials):
    folder = _module(tmp_path / "walls", test_tool=PASSES, test_walls=WALLS)
    ran = testrun.run("walls", folder)
    assert ran.ok, ran.said
    assert "3 passed" in ran.said
    [trial] = trials
    assert trial != profiles.for_module("walls")
    assert not _left(trial), "the container is deleted after the run"
    assert "S-1-15-2-" not in grants.access_list(folder), "and nothing stays granted to it"


def test_a_failing_test_or_none_at_all_is_not_a_pass(tmp_path, trials):
    failing = testrun.run("walls", _module(tmp_path / "failing", test_tool=PASSES,
                                           test_wrong=FAILS))
    assert not failing.ok and "1 failed, 1 passed" in failing.said
    none = testrun.run("walls", _module(tmp_path / "none"))
    assert none == testrun.Ran(False, "it has no tests")


def test_a_test_that_ends_the_run_itself_is_not_a_pass(tmp_path, trials):
    """`os._exit(0)` exits as a pass does, before pytest says anything: the
    exit code alone took it for one (step 11's audit, B5). The verdict is
    pytest's own line, sealed, which such a run never prints."""
    ran = testrun.run("walls", _module(tmp_path / "quits", test_tool=PASSES, test_quits=QUITS))
    assert not ran.ok and testrun.UNSEALED in ran.said

    passed = testrun.run("walls", _module(tmp_path / "passes", test_tool=PASSES))
    # The seal's line is taken out: what is shown ends as pytest ended it.
    assert passed.ok and re.fullmatch(r"1 passed in [0-9.]+s", passed.said.splitlines()[-1])


def test_a_modules_tests_run_where_the_container_may_not_open_nul(tmp_path):
    """pytest's usual capture opens NUL for stdin, and a less-privileged
    container on Windows Server may not: CI's runner refused it, and every
    module's tests failed before the first one ran (22 October 2026). The
    laptop's Windows allows it, so the refusal is made here: the run's own
    code, outside a container, with NUL's name pointing at a folder, which
    Windows refuses to open as a file just as the runner refused NUL."""
    folder = _module(tmp_path / "walls", test_tool=PASSES)
    refused = tmp_path / "refused"
    refused.mkdir()
    with pytest.raises(PermissionError):
        refused.open(encoding="utf-8")
    start = ("import os, sys; os.devnull = sys.argv.pop(1); "
             "exec(compile(sys.argv.pop(1), '<child>', 'exec'))")
    done = subprocess.run(
        [sys.executable, "-c", start, str(refused), testrun.CHILD,
         "walls", str(folder), str(tmp_path / "testing"), "-q", str(folder / "tests")],
        input="the-seal\n", capture_output=True, encoding="utf-8", errors="replace",
        timeout=60, cwd=paths.SOURCE)
    said = done.stdout.rstrip().splitlines()
    assert said and said[-1] == "the-seal 0", done.stdout + done.stderr


def test_a_test_that_never_ends_is_stopped(tmp_path, trials):
    ran = testrun.run("walls", _module(tmp_path / "waits", test_waits=WAITS), timeout_s=5)
    assert ran == testrun.Ran(False, "its tests ran past 5 seconds and were stopped")
    assert trials and not any(_left(name) for name in trials)
