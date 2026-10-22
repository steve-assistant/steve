"""The module sandbox's parts that need no container (core/sandbox/).

The walls themselves are tried from inside real containers in
tests/gate/test_module_sandbox.py. What is here decides what those
containers are: whose they are, by name; what their job holds them to; what
they inherit of the environment; whether the runtime they run is whole; and
what the user is told when this computer cannot hold one.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
from pathlib import Path

import pytest

from agent.attention.sources import health
from core import identity, paths
from core.sandbox import job as jobs
from core.sandbox import launch, probe, profiles, runtime, win32
from steve_sdk.manifest import ID


def test_an_install_makes_its_id_once_and_its_names_carry_eight_characters_of_it(
        monkeypatch, tmp_path):
    """Two installs on one Windows account never share a profile, and every
    test, with a data folder of its own, has an install of its own."""
    first = profiles.install()
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert profiles.install() == first
    made = (paths.data_dir() / profiles.INSTALL_FILE).read_text(encoding="ascii").strip()
    assert len(made) == 32 and made.startswith(first)
    monkeypatch.setenv("STEVE_DATA_DIR", str(tmp_path / "another"))
    assert profiles.install() != first


def test_an_install_id_that_is_not_one_is_refused_rather_than_replaced():
    """Replaced, it would orphan every profile and grant made under the old
    id; the user moving it aside is a choice to start again."""
    path = paths.data_dir() / profiles.INSTALL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not an id\n", encoding="ascii", newline="\n")
    with pytest.raises(OSError, match="not an install id"):
        profiles.install()


def test_a_modules_container_is_named_for_its_install_and_fits_windows_limit():
    """Windows takes a profile name of up to 64 characters, and the longest
    module id must fit."""
    longest = "m" + "x" * 31
    assert ID.match(longest) and not ID.match(longest + "x")
    name = identity.container("0123abcd", longest)
    assert name == f"steve.0123abcd.m.{longest}"
    assert len(name) <= 64
    assert identity.probe_container("0123abcd") == "steve.0123abcd.probe"


def test_a_local_servers_containers_are_its_own_for_each_account_and_fit_windows_limit():
    """A server signs in to one account and keeps it in its home; another
    account's server, in the same container, could read that sign-in.
    npm, which writes the server's files, runs apart from the server."""
    longest = "m" + "x" * 31
    names = {identity.server_container("0123abcd", longest, key)
             for key in ("a" * 40, "b" * 40)}
    assert len(names) == 2 and all(len(n) <= 64 and n.startswith("steve.0123abcd.s.")
                                   for n in names)
    npm = identity.npm_container("0123abcd", longest)
    assert npm == f"steve.0123abcd.n.{longest}" and len(npm) <= 64


def test_an_overlapped_is_the_size_windows_reads():
    """A pipe's reads and writes hand Windows this structure to fill; one
    of the wrong size, and it writes past it into Python's memory."""
    assert ctypes.sizeof(win32.OVERLAPPED) == 32


def test_a_cpu_cap_is_a_share_of_every_processor(monkeypatch):
    """Windows takes hundredths of a percent of the whole machine: one core
    of sixteen is 625, never nothing and never more than all of it."""
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert jobs.cpu_rate(1) == 625
    assert jobs.cpu_rate(16) == jobs.cpu_rate(64) == 10_000
    assert jobs.cpu_rate(0.0001) == 1


def test_a_job_holds_what_it_was_given_and_no_more():
    """Read back from Windows: a limit that did not take shows as missing."""
    capped = jobs.Job(jobs.Limits(memory=64 << 20, processes=1, cpu_cores=1, ui=True))
    plain = jobs.Job()
    try:
        assert capped.held_to() == {"kill_on_close": True, "memory": 64 << 20, "processes": 1,
                                    "cpu_rate": jobs.cpu_rate(1),
                                    "ui": win32.JOB_OBJECT_UILIMIT_ALL}
        assert plain.held_to() == {"kill_on_close": True, "memory": None, "processes": None,
                                   "cpu_rate": None, "ui": 0}
    finally:
        capped.close()
        plain.close()


def test_a_container_inherits_nothing_the_user_set(monkeypatch):
    """Provider keys, Steve's own settings and the personal edition's link
    all live in the user's environment. A module sees only what Windows
    needs to start it, and a path of the folders it is given."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-key")
    monkeypatch.setenv("STEVE_SECRET_THING", "x")
    monkeypatch.setenv("HOMEPATH", r"\Users\someone")
    folder = Path(r"C:\runtime\python")
    env = launch.environment([folder], {"EXTRA": "1"})
    assert set(env) <= {*launch._KEPT, "PATH", "EXTRA"}
    assert "LOCALAPPDATA" in env  # without it python.exe does not start in a container
    assert env["PATH"].split(";")[0] == str(folder)
    assert env["PATH"].lower().endswith("system32")


def test_an_environment_block_is_sorted_as_windows_requires():
    block = launch._block({"b": "2", "A": "1", "C": "3"})
    assert block[:] == "A=1\0b=2\0C=3\0\0"


def test_a_runtime_not_built_says_what_builds_it(tmp_path):
    assert "python -m core.sandbox build" in runtime.problem(tmp_path)
    runtime.python(tmp_path).parent.mkdir(parents=True)
    runtime.python(tmp_path).write_bytes(b"")
    (tmp_path / runtime.BUILT).write_text('{"node": "0.0.1"}', encoding="utf-8")
    assert f"no Node {runtime.NODE.version}" in runtime.problem(tmp_path)
    runtime.node(tmp_path).parent.mkdir(parents=True)
    runtime.node(tmp_path).write_bytes(b"")
    made = {"node": runtime.NODE.version, "packages": dict.fromkeys(runtime._wanted(), "1")}
    (tmp_path / runtime.BUILT).write_text(json.dumps(made), encoding="utf-8")
    assert runtime.problem(tmp_path) is None


def test_a_runtime_without_a_package_pytest_requires_says_what_builds_it(tmp_path):
    """pytest 9.1 began to require pygments, and a runtime built from a list
    kept by hand had none: every module's tests failed to start, as if
    they had failed. What a test run needs follows pytest's own metadata,
    and a runtime without one of them is not whole."""
    assert {"pytest", "pluggy", "pygments"} <= set(runtime.required(runtime.TESTING))
    assert runtime.required(("no-such-distribution",)) == []
    runtime.python(tmp_path).parent.mkdir(parents=True)
    runtime.python(tmp_path).write_bytes(b"")
    runtime.node(tmp_path).parent.mkdir(parents=True)
    runtime.node(tmp_path).write_bytes(b"")
    made = {"node": runtime.NODE.version,
            "packages": {name: "1" for name in runtime._wanted() if name != "pygments"}}
    (tmp_path / runtime.BUILT).write_text(json.dumps(made), encoding="utf-8")
    assert runtime.problem(tmp_path) == (
        "the module runtime has no pygments: run python -m core.sandbox build")


def test_the_sdks_copy_is_judged_stale_by_its_source_not_its_bytecode(tmp_path):
    """A checkout's steve_sdk is copied again when it changes. Its
    `__pycache__` changes each time it runs, and must not count."""
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")
    before = runtime.sdk_hash(tmp_path)
    (tmp_path / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"\0")
    assert runtime.sdk_hash(tmp_path) == before
    (tmp_path / "a.py").write_text("A = 2\n", encoding="utf-8")
    assert runtime.sdk_hash(tmp_path) != before


def test_the_check_calls_an_answer_from_this_computers_own_address_a_leak():
    """A plain AppContainer on the laptop this was written on: loopback
    timed out and listening was not reached, but its own address answered
    "connection refused", so the attempt reached the network stack, and a
    port listening there would have taken it."""
    plain = {"loopback": None, "own_address": 10061, "listening": 52000,
             "listening_reached": False}
    assert probe.judge(plain, seen=False) == "own_address (answered 10061, not refused)"
    held = {"loopback": probe.WSAEACCES, "own_address": probe.WSAEACCES, "listening": None,
            "listen_error": probe.WSAEACCES, "listening_reached": False}
    assert probe.judge(held, seen=False) is None
    assert probe.judge(held, seen=True) == "loopback (seen by the listener)"
    assert probe.judge({**held, "listening_reached": True}, seen=False) == "listening"


def test_a_runtime_not_built_leaves_modules_off_and_says_why(monkeypatch):
    monkeypatch.setattr(runtime, "problem", lambda root=None: "the runtime is not built")
    found = probe.run()
    assert found == probe.Support(False, "", "the runtime is not built")
    assert probe.verdict() is found


def test_health_says_when_modules_with_code_cannot_run_or_run_looser(monkeypatch):
    assert health._module_sandbox(None) is None  # not checked yet
    monkeypatch.setattr(probe, "_verdict", probe.Support(True, probe.LPAC, ""))
    assert health._module_sandbox(None) is None
    monkeypatch.setattr(probe, "_verdict",
                        probe.Support(False, "", "no container would start here: lpac: x"))
    assert health._module_sandbox(None) == (
        "modules:sandbox", "Modules with code cannot run on this computer",
        "No container would start here: lpac: x. Skills, prompt sections and the"
        " built-in accounts still work.")
    monkeypatch.setattr(probe, "_verdict",
                        probe.Support(True, probe.APPCONTAINER, "the less-privileged one would"
                                                                " not start"))
    assert health._module_sandbox(None) == (
        "modules:looser", "Modules run in a looser sandbox on this computer",
        "The less-privileged one would not start.")
