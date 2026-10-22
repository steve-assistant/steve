"""A module's code, run in its container, cannot reach what is the user's.

Each wall of core/sandbox/ is tried from inside a real container, by the
module runtime's own Python, as a module's code would run: the files, the
Credential Manager, the network, loopback, a second process, memory and
time. And day one's proofs (06-modules.md, section 2), which the rest of
step 6 is built on: Node runs a package tree under LPAC, and the pipe a
local server is given opens from its container and from no other.

Nothing here is skipped. A runtime that is not built fails the test (`python
-m core.sandbox build` makes it): a wall that is not tried is a wall that is
not known to hold. Each test makes its profiles under its own install id, so
two runs never share one, and deletes them.

Frozen, because a coding pipeline that could edit this could decide its
walls had held.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from core import identity, paths
from core.sandbox import grants, launch, pipe, probe, profiles, runtime, win32
from core.sandbox import job as jobs

# `processes`: each test starts real processes, a second or two each, so the
# developer's fast tier leaves them out; the gate never does. `appcontainer`:
# the one marker the conftest opens AppContainer profiles to.
pytestmark = [pytest.mark.processes, pytest.mark.appcontainer]

# What Windows says when a container is refused: ERROR_ACCESS_DENIED for a
# folder, and EACCES from the C runtime's `open` for a file or a pipe;
# WSAEACCES for a socket; ERROR_NOT_ENOUGH_QUOTA for a process past the
# job's one. The Credential Manager refuses a less-privileged container the
# binding to its service itself (RPC_S_INVALID_BINDING), and a plain one the
# call (spike S1); either is a refusal. Let through, the child would be
# told there is no such credential (ERROR_NOT_FOUND).
ACCESS_DENIED = 5
EACCES = 13
CREDENTIALS_REFUSED = {ACCESS_DENIED, 1702}
WSAEACCES = 10013
NOT_ENOUGH_QUOTA = 1816
MEMORY = 256 << 20
LIMITS = jobs.Limits(memory=MEMORY, processes=1, cpu_cores=1, ui=True)

# What every child below starts with: a way to try something and say how it
# failed, by the Windows error where there is one.
PRELUDE = """
import json, os, sys

def attempt(do):
    try:
        do()
        return "ok"
    except OSError as error:
        return getattr(error, "winerror", None) or error.errno

def say(**found):
    print(json.dumps(found), flush=True)
"""


@dataclass
class Walls:
    root: Path
    container: profiles.Container
    code: Path
    state: Path


@dataclass
class Ran:
    exit: int | None
    timed_out: bool
    said: dict | None
    stderr: str


@pytest.fixture
def walls():
    """One module, `walls`, as step 6 lays a module out: its code read-only,
    its state writable, in its own container."""
    root = runtime.ready()
    container = profiles.ensure(profiles.for_module("walls"))
    try:
        code = paths.module_dir("walls") / "code" / "1"
        code.mkdir(parents=True)
        (code / "main.py").write_text("SAID = 'from the module'\n", encoding="utf-8",
                                      newline="\n")
        state = paths.module_state_dir("walls")
        grants.grant(code, container.sid, grants.READ)
        grants.grant(state, container.sid, grants.MODIFY)
        yield Walls(root, container, code, state)
    finally:
        profiles.delete(container.name)


def run(walls: Walls, child: str, *args: str, limits: jobs.Limits = LIMITS,
        seconds: float = 30.0, container: profiles.Container | None = None) -> Ran:
    """Run `child` (after PRELUDE) in the module's container, to its end; or
    in `container`, from the runtime's folder, which every container reads."""
    python = runtime.python(walls.root)
    argv = [str(python), *runtime.FLAGS, "-c", PRELUDE + child, *args]
    with launch.start(argv, container=container or walls.container,
                      cwd=python.parent if container else walls.code,
                      limits=limits, env=launch.environment([python.parent]),
                      capabilities=runtime.capabilities(True)) as process, \
            process.deadline(seconds):
        out = process.stdout.read()
        err = process.stderr.read()
        code = process.wait(10)
        timed_out = process.timed_out
    return Ran(code, timed_out, json.loads(out) if out.strip() else None,
               err.decode(errors="replace"))


def test_a_module_reads_its_code_and_writes_its_state_and_nothing_else_of_the_users(walls):
    """Its code and its state are the two folders granted to it. The data
    folder beside them holds every other module's state and the user's
    database; the checkout holds Steve; the home folder holds the user."""
    canary = paths.data_dir() / "canary.txt"
    canary.write_text("the user's\n", encoding="utf-8", newline="\n")
    ran = run(walls, """
code, state, canary, source, home = sys.argv[1:6]

def write(path):
    with open(path, "w") as f:
        f.write("written from the container")

say(read_code=attempt(lambda: open(os.path.join(code, "main.py")).read()),
    write_state=attempt(lambda: write(os.path.join(state, "note.txt"))),
    write_code=attempt(lambda: write(os.path.join(code, "planted.py"))),
    read_canary=attempt(lambda: open(canary).read()),
    read_source=attempt(lambda: open(source).read()),
    list_home=attempt(lambda: os.listdir(home)))
""", str(walls.code), str(walls.state), str(canary), str(paths.SOURCE / "pyproject.toml"),
        str(Path.home()))
    assert ran.said == {"read_code": "ok", "write_state": "ok",
                        "write_code": EACCES, "read_canary": EACCES,
                        "read_source": EACCES, "list_home": ACCESS_DENIED}, ran
    assert (walls.state / "note.txt").read_text(encoding="utf-8") == "written from the container"
    assert not (walls.code / "planted.py").exists()


def test_a_module_cannot_open_the_credential_manager(walls):
    """Every account's secret is there. The child asks for a credential
    that does not exist, in Steve's own space: refused, it is told access
    is denied; let through, it would be told there is no such credential.
    Nothing is read or written either way."""
    ran = run(walls, """
import ctypes
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
found = ctypes.c_void_p()
ok = advapi32.CredReadW(sys.argv[1], 1, 0, ctypes.byref(found))
error = ctypes.get_last_error()
if ok:
    advapi32.CredFree(found)
say(credential="ok" if ok else error)
""", f"{identity.KEYCHAIN}-walls/absent")
    assert ran.said["credential"] in CREDENTIALS_REFUSED, ran


def test_a_module_reaches_neither_the_internet_nor_this_computers_own_ports(walls):
    """It has no network capability: its only ways out are the broker's
    methods. The internet address is a documentation one, which nothing
    answers, so a wall that failed would show as a timeout, not a refusal."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        ran = run(walls, """
import socket
def connect(where):
    with socket.create_connection(where, timeout=3):
        pass
say(internet=attempt(lambda: connect(("192.0.2.1", 443))),
    loopback=attempt(lambda: connect(("127.0.0.1", int(sys.argv[1])))))
""", str(listener.getsockname()[1]))
        listener.setblocking(False)
        with pytest.raises(BlockingIOError):
            listener.accept()
    assert ran.said == {"internet": WSAEACCES, "loopback": WSAEACCES}, ran


def test_a_module_cannot_start_a_second_process(walls):
    """One process: what it starts would be outside the broker's view, and
    each process is another memory cap's worth."""
    ran = run(walls, """
import subprocess
say(second=attempt(lambda: subprocess.run([sys.executable, "-c", "pass"], check=True)))
""")
    assert ran.said == {"second": NOT_ENOUGH_QUOTA}, ran


def test_a_module_cannot_take_more_memory_than_its_cap(walls):
    ran = run(walls, """
try:
    held = bytearray(int(sys.argv[1]))
    say(memory="allocated")
except MemoryError:
    say(memory="refused")
""", str(MEMORY + (144 << 20)))
    assert ran.said == {"memory": "refused"}, ran


def test_a_call_that_overstays_is_ended_by_the_parent(walls):
    """The deadline is a timer here, not anything the child runs: code that
    never returns cannot keep its process. The exit code says why it ended."""
    began = time.monotonic()
    ran = run(walls, """
import time
time.sleep(60)
say(slept=True)
""", seconds=1.0)
    assert (ran.exit, ran.timed_out, ran.said) == (launch.TIMED_OUT, True, None), ran
    assert time.monotonic() - began < 15


def test_the_job_holds_the_limits_it_was_given(walls):
    """Read back from Windows, not from what was asked: the CPU cap is
    checked here rather than by timing a busy loop, which a busy laptop
    would fail."""
    python = runtime.python(walls.root)
    with launch.start([str(python), *runtime.FLAGS, "-c", "import sys; sys.stdin.readline()"],
                      container=walls.container, cwd=walls.code, limits=LIMITS,
                      env=launch.environment([python.parent]),
                      capabilities=runtime.capabilities(True)) as process, \
            process.deadline(30):
        held = process.job.held_to()
        process.stdin.write(b"\n")
        assert process.wait(10) == 0
    assert held == {"kill_on_close": True, "memory": MEMORY, "processes": 1,
                    "cpu_rate": jobs.cpu_rate(1), "ui": win32.JOB_OBJECT_UILIMIT_ALL}


def _left(name: str) -> bool:
    """Whether the container `name` is still in Windows' list: its folder
    in AppData goes with it."""
    return (Path(os.environ["LOCALAPPDATA"]) / "Packages" / name).exists()


def _an_address_of_this_computer() -> str:
    """This computer's own address, from its host name. `probe.own_address`
    asks which interface routes to a documentation address, which sends
    nothing but is a socket's connect, and the fence refuses every one off
    this computer."""
    for *_, (address, _port) in socket.getaddrinfo(socket.gethostname(), None,
                                                   socket.AF_INET):
        if not address.startswith("127."):
            return address
    return ""


def test_this_computer_holds_a_module_in_a_less_privileged_container(monkeypatch):
    """Decision D6's check, as the worker runs it at each start, with its
    child trying this computer's own address as well as loopback."""
    address = _an_address_of_this_computer()
    assert address, "this computer has no address but loopback: one wall would go untried"
    monkeypatch.setattr(probe, "own_address", lambda: address)
    found = probe.run()
    assert found == probe.Support(True, probe.LPAC, ""), found
    assert probe.verdict() == found
    assert not _left(identity.probe_container(profiles.install())), (
        "the probe's container is made again at each start, and kept by no one")


def test_the_checks_own_child_sees_a_way_out_where_there_is_one():
    """The check is only as good as its child's eyes. Run outside any
    container, the same child reaches this test's listener, and the check
    calls that a leak."""
    root = runtime.ready()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)
        child = subprocess.Popen(
            [str(runtime.python(root)), *runtime.FLAGS, "-c", probe.CHILD,
             str(listener.getsockname()[1]), ""],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            listener.accept()[0].close()
            found = json.loads(child.stdout.readline())
            child.stdin.write(b"\n")
            child.stdin.flush()
        finally:
            child.wait(10)
    assert found["loopback"] == "reached", found
    assert "loopback" in probe.judge(found, seen=True)


def _package(code: Path) -> None:
    """A module's Node code requiring a package in its own node_modules."""
    package = code / "node_modules" / "greeting"
    (package / "lib").mkdir(parents=True)
    (package / "package.json").write_text('{"name": "greeting", "main": "lib/index.js"}\n',
                                          encoding="utf-8", newline="\n")
    (package / "lib" / "index.js").write_text(
        "module.exports = (who) => `hello ${who}`;\n", encoding="utf-8", newline="\n")
    (code / "index.js").write_text(
        "console.log(JSON.stringify({said: require('greeting')('container')}));\n",
        encoding="utf-8", newline="\n")


def test_node_runs_a_package_tree_in_a_less_privileged_container(walls):
    """Day one's first proof: local servers (section 7) are Node programs.
    Without the flags Node stops before its first line, resolving the real
    path of its folders through `C:\\`, which a container may not read."""
    _package(walls.code)
    node = runtime.node(walls.root)
    said = {}
    for flags in ((), runtime.NODE_FLAGS):
        argv = [str(node), *flags, str(walls.code / "index.js")]
        with launch.start(argv, container=walls.container, cwd=walls.code,
                          limits=jobs.Limits(memory=512 << 20, processes=1, ui=True),
                          env=launch.environment([node.parent]),
                          capabilities=runtime.capabilities(True)) as process, \
                process.deadline(30):
            out = process.stdout.read().decode(errors="replace").strip()
            err = process.stderr.read().decode(errors="replace")
            said[flags] = (process.wait(10), out, "EPERM" in err)
    assert said[runtime.NODE_FLAGS] == (0, '{"said":"hello container"}', False), said
    exit_code, out, eperm = said[()]
    assert exit_code != 0 and eperm and not out, said


def test_a_local_servers_pipe_opens_from_its_container_and_no_other(walls):
    """Day one's second proof: the pipe is a server's only network. It
    opens from the container it was made for, whose process the broker
    checks by pid, and is refused to any other while an instance waits."""
    other = profiles.ensure(profiles.for_module("walls_other"))
    held = pipe.Pipe(walls.container.sid, profiles.install())
    child = """
try:
    with open(sys.argv[1], "r+b", buffering=0) as p:
        p.write(b"hello from the container")
        say(opened=True, reply=p.read(64).decode())
except OSError as error:
    say(opened=False, error=getattr(error, "winerror", None) or error.errno)
"""
    python = runtime.python(walls.root)
    try:
        with launch.start([str(python), *runtime.FLAGS, "-c", PRELUDE + child, held.name],
                          container=walls.container, cwd=walls.code, limits=LIMITS,
                          env=launch.environment([python.parent]),
                          capabilities=runtime.capabilities(True)) as process, \
                process.deadline(30):
            connection = held.accept(15)
            assert connection is not None, process.stderr.read().decode(errors="replace")
            assert connection.client_pid == process.pid
            assert connection.stream.read(64) == b"hello from the container"
            connection.stream.write(b"hello from the broker")
            connection.close()
            said = json.loads(process.stdout.readline())
        assert said == {"opened": True, "reply": "hello from the broker"}
        refused = run(walls, child, held.name, container=other)
        assert refused.said == {"opened": False, "error": EACCES}, refused
        assert held.accept(1) is None
    finally:
        held.close()
        profiles.delete(other.name)
