"""Sandbox tests.

Skipped when Docker is down, because Docker being unavailable is a normal
state on this machine and a red suite you learn to ignore is worse than no
suite. The degradation path is tested unconditionally -- that one matters most
precisely when Docker is not running.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tools import code_exec


@pytest.fixture
def _docker_up():
    # Asked inside the test, not at import: the fences refuse Docker to
    # every test but one marked `docker`, and to collection, where this was
    # a `skipif` that ran `docker version` for every run that imported the
    # file, chosen or not.
    if not code_exec.available():
        pytest.skip("docker daemon not running")


def docker_required(test):
    """Real Docker: skipped where it is down, and marked `processes`, so the
    developer's fast tier leaves it out (17.7 s for this module on 8 October
    2026, the timeout test alone 8 s). The gate and the full suite run it.
    Marked `docker` too, which opens Docker to it alone (tests/gate/fences.py)."""
    return pytest.mark.processes(pytest.mark.docker(pytest.mark.usefixtures("_docker_up")(test)))


# --------------------------------------------------------------------------
# Works without Docker
# --------------------------------------------------------------------------


class _Daemon:
    """Docker's command line, answering every question with `code`, and
    keeping what it was asked."""

    def __init__(self, code: int) -> None:
        self.code, self.asked = code, []

    def __call__(self, *args, timeout=None):
        self.asked.append(args)
        return subprocess.CompletedProcess(args, self.code, "", "")


def test_an_unavailable_sandbox_always_says_why(monkeypatch):
    """The status light and the model read the reason; "unavailable" alone
    leaves the user nothing to do. Faked both ways down: asked of the real
    daemon, this tested only whichever state the computer was in."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert code_exec.status()["reason"]
    monkeypatch.setattr(shutil, "which", lambda name: "docker")
    monkeypatch.setattr(code_exec, "_docker", _Daemon(1))
    down = code_exec.status()
    assert down["available"] is False and down["reason"]


def test_asking_whether_docker_is_up_runs_no_plugins(monkeypatch):
    """`docker info` gave the same answer and ran every CLI plugin Docker
    Desktop installs to list them: thirteen processes and 1.3 s of CPU a
    time, once a minute all day for a status light (5 October 2026). Nor is
    every image listed to find one."""
    from core.browser import sandbox

    asked: list[tuple] = []

    def docker(*args, timeout=None):
        asked.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="28.4.0 linux\n", stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: "docker")
    monkeypatch.setattr(code_exec, "_docker", docker)
    assert code_exec.available() and code_exec.status()["available"]
    assert code_exec.status()["server_version"] == "28.4.0"
    assert sandbox.available(lambda args, timeout: docker(*args))

    assert asked and all(args[0] in ("version", "image") for args in asked), asked


def test_an_engine_running_windows_containers_is_not_a_sandbox_and_says_why(monkeypatch):
    """Steve's images are Linux's. On CI's runner (22 October 2026) Docker
    answered, so the code sandbox said it was up, and each run then failed
    with "read-only mode is not supported for Windows containers"."""
    from core.browser import sandbox

    def docker(*args, timeout=None):
        return subprocess.CompletedProcess(args, 0, stdout="28.4.0 windows\n", stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: "docker")
    monkeypatch.setattr(code_exec, "_docker", docker)
    assert not code_exec.available()
    assert not sandbox.available(lambda args, timeout: docker(*args))
    said = code_exec.status()
    assert not said["available"] and "Linux containers" in said["reason"], said


def test_path_traversal_is_refused_before_a_container_is_made(monkeypatch):
    """Checked on the host, before anything runs. Asked of the real daemon,
    this checked the path only where Docker was up: with it down, the run
    stopped at "Docker is not running" first."""
    daemon = _Daemon(1)
    monkeypatch.setattr(code_exec, "available", lambda: True)
    monkeypatch.setattr(code_exec, "_docker", daemon)
    result = code_exec.run_python("print(1)", files={"../escape.txt": "nope"})
    assert not result.ok and "refusing" in (result.error or "")
    assert all(args[0] == "image" for args in daemon.asked), daemon.asked


def test_a_code_runs_container_carries_its_install_so_uninstall_finds_it(monkeypatch):
    """A worker ended mid-run leaves the container, and uninstall removes
    only what carries its install's id; a code run's carried no label at
    all (the review of steps 7 to 9)."""
    from core import identity
    from core.sandbox import profiles

    daemon = _Daemon(1)
    monkeypatch.setattr(code_exec, "available", lambda: True)
    monkeypatch.setattr(code_exec, "_docker", daemon)
    code_exec.run_python("print(1)")
    [create] = [args for args in daemon.asked if args[0] == "create"]
    label = f"{identity.DOCKER_INSTALL_LABEL}={profiles.install()}"
    assert create[create.index("--label") + 1] == label


def test_the_analysis_image_is_pinned_and_run_by_the_id_its_build_recorded(monkeypatch):
    """Its packages were whatever the index served the day it was built, on a
    base named by tag, and it was found and run by a tag that anyone allowed
    to use Docker can point elsewhere (step 11's audit, C4 and F2)."""
    import re

    from core import images

    assert re.search(r"^FROM \S+@sha256:[0-9a-f]{64}$", code_exec.ANALYSIS_DOCKERFILE, re.M)
    assert "pip install --no-cache-dir --require-hashes -r" in code_exec.ANALYSIS_DOCKERFILE
    pins = code_exec.REQUIREMENTS.read_text(encoding="utf-8").replace("\\\n", " ")
    lines = [line for line in pins.splitlines() if line.strip() and not line.startswith("#")]
    assert lines and all(re.match(r"[\w.-]+==\S+ +--hash=sha256:[0-9a-f]{64}", line)
                         for line in lines), lines

    ours = "sha256:" + "a" * 64
    asked: list[tuple] = []

    def docker(*args, timeout=None):
        asked.append(args)
        held = args[:2] == ("image", "inspect") and args[-1] == ours
        return subprocess.CompletedProcess(args, 0 if held else 1, f"{ours}\n" if held else "", "")

    monkeypatch.setattr(code_exec, "available", lambda: True)
    monkeypatch.setattr(code_exec, "_docker", docker)

    code_exec.run_python("print(1)")
    [create] = [args for args in asked if args[0] == "create"]
    assert code_exec.DEFAULT_IMAGE in create and "@sha256:" in code_exec.DEFAULT_IMAGE

    images.remember(code_exec.analysis_image(), ours)
    asked.clear()
    code_exec.run_python("print(1)")
    [create] = [args for args in asked if args[0] == "create"]
    assert ours in create and code_exec.analysis_image() not in create


def test_missing_docker_is_a_result_not_an_exception(monkeypatch):
    """A dead daemon must not abort the turn; the model should see and say so."""
    monkeypatch.setattr(code_exec, "available", lambda: False)
    result = code_exec.run_python("print(1)")
    assert result.ok is False
    assert "Docker is not running" in (result.error or "")
    assert result.exit_code is None


# --------------------------------------------------------------------------
# Requires Docker
# --------------------------------------------------------------------------


@docker_required
def test_code_runs_and_returns_stdout():
    result = code_exec.run_python("print(sum(i * i for i in range(1, 11)))")
    assert result.ok, result.stderr
    assert result.stdout.strip() == "385"


@docker_required
def test_network_is_denied_by_default():
    """The default matters more than the capability: code that phones home
    should have to be granted that explicitly, in the transcript."""
    result = code_exec.run_python(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=4)\n"
        "    print('REACHED')\n"
        "except OSError:\n"
        "    print('blocked')",
        timeout_s=25,
    )
    assert "REACHED" not in result.stdout
    assert "blocked" in result.stdout


@docker_required
def test_a_traceback_is_a_result():
    result = code_exec.run_python("raise ValueError('deliberate')")
    assert result.ok is False
    assert result.exit_code == 1
    assert "ValueError: deliberate" in result.stderr
    assert result.error is None  # a failing program is not a harness error


@docker_required
def test_timeout_is_enforced():
    result = code_exec.run_python("import time; time.sleep(60)", timeout_s=5)
    assert result.timed_out is True
    assert result.ok is False
    assert result.duration_s < 45


@docker_required
def test_input_files_arrive_and_artifacts_come_back(tmp_path):
    """The tar-stream round trip, which `docker cp` cannot do against a
    read-only rootfs."""
    result = code_exec.run_python(
        "data = open('/work/input.txt').read().strip()\n"
        "open('/work/out/result.txt', 'w').write(data.upper())\n"
        "print('ok')",
        files={"input.txt": "hello sandbox"},
        timeout_s=60,
    )
    assert result.ok, result.stderr
    assert result.artifacts == ["result.txt"]
    assert result.artifact_dir is not None

    from pathlib import Path

    produced = Path(result.artifact_dir) / "result.txt"
    assert produced.read_text(encoding="utf-8") == "HELLO SANDBOX"


async def test_the_analysis_image_is_built_only_once_the_user_turns_running_code_on(
        monkeypatch):
    """A fresh install built a gigabyte of image at its first start, for a
    tool nobody had asked for."""
    import asyncio

    from agent.worker import background
    from core import knobs

    built = []
    monkeypatch.setattr(background.code_exec, "ensure_analysis_image",
                        lambda: built.append(1) or True)
    monkeypatch.setattr(background, "SANDBOX_LOOK_S", 0)

    waiting = asyncio.create_task(background.build_sandbox())
    for _ in range(20):
        await asyncio.sleep(0)
    assert built == [] and not waiting.done()

    knobs.write({"RUN_CODE": "on"})
    await asyncio.wait_for(waiting, 5)
    assert built == [1]


async def test_the_browsers_images_are_built_once_the_user_turns_the_browser_on(monkeypatch):
    """Only a command a new user never runs built them, so the browser that
    setup's Docker page turned on said, at its first page, that it was not
    built. Not before the switch: the first build pulls about 2 GB."""
    import asyncio

    from agent.worker import background
    from core import knobs
    from core.browser import sandbox

    built = []
    monkeypatch.setattr(sandbox, "build", lambda: built.append(1) or ["steve/browser:1"])
    monkeypatch.setattr(background, "SANDBOX_LOOK_S", 0)

    waiting = asyncio.create_task(background.build_browser())
    for _ in range(20):
        await asyncio.sleep(0)
    assert built == [] and not waiting.done()

    knobs.write({"BROWSER": "on"})
    await asyncio.wait_for(waiting, 5)
    assert built == [1]
