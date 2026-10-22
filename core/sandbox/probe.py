"""Whether this computer can hold a module in, checked at each start (D6).

There is no second wall to fall back on. Spike S1 tried the one that needs
no AppContainer, a Low-integrity token with restricting SIDs: it kept a
child from the user's files and Credential Manager, but not from the
network. So a module's code runs in an AppContainer or not at all
(decision D6), and this check says which, each time the worker starts:
Windows' support can change between starts, with an update or a policy.

It starts the runtime in a container of its own, less-privileged (LPAC)
first and a plain AppContainer if that will not start, deleted after, and
the child tries three ways out, each of which must fail:

- connecting to a loopback port this process listens on;
- connecting to the computer's own network address;
- listening on loopback for this process to reach.

Nothing leaves the computer: its own address is found by asking which
interface would route to a documentation address, which sends nothing,
and a failed check has reached only this process.

A plain AppContainer works but is looser than LPAC: things granted to
every app package are open to it. Health says so.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import threading
from dataclasses import dataclass

from core import identity
from core.sandbox import job as jobs
from core.sandbox import launch, profiles, runtime

log = logging.getLogger("sandbox.probe")

LPAC = "lpac"
APPCONTAINER = "appcontainer"
# What a socket call in a container without the network capability raises.
WSAEACCES = 10013
TIMEOUT_S = 20.0

# Run with `-c`: the child cannot read anything this process could write it
# to, so it carries its code on its command line.
CHILD = """
import json, socket, sys
port, address = int(sys.argv[1]), sys.argv[2]

def attempt(where):
    try:
        with socket.create_connection(where, timeout=3):
            return "reached"
    except OSError as error:
        return getattr(error, "winerror", None) or error.errno

found = {"loopback": attempt(("127.0.0.1", port))}
found["own_address"] = attempt((address, port)) if address else None
try:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    found["listening"] = server.getsockname()[1]
except OSError as error:
    found["listening"] = None
    found["listen_error"] = getattr(error, "winerror", None) or error.errno
print(json.dumps(found), flush=True)
sys.stdin.readline()
"""


@dataclass(frozen=True)
class Support:
    """The answer: whether code modules may run, in which container, and why."""

    ok: bool
    mode: str  # LPAC, APPCONTAINER, or "" when they may not
    reason: str


_verdict: Support | None = None


def verdict() -> Support | None:
    """The last start's answer, or None before it is known."""
    return _verdict


def own_address() -> str:
    """This computer's address on its network, or "" if it has none. A UDP
    socket's connect only picks a route: nothing is sent."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.connect(("192.0.2.1", 9))
        return udp.getsockname()[0]
    return ""


def _reachable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _try(container: profiles.Container, lpac: bool) -> str | None:
    """None if every way out failed in this kind of container, else what got through."""
    root = runtime.ready()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        # Anything that connects is accepted and dropped: its arrival is the finding.
        listener.settimeout(0.2)
        reached = threading.Event()
        stop = threading.Event()

        def watch() -> None:
            while not stop.is_set():
                with contextlib.suppress(OSError):
                    listener.accept()[0].close()
                    reached.set()

        watcher = threading.Thread(target=watch, daemon=True, name="probe-listener")
        watcher.start()
        port = listener.getsockname()[1]
        argv = [str(runtime.python(root)), *runtime.FLAGS, "-c", CHILD, str(port), own_address()]
        folder = runtime.python(root).parent
        try:
            with launch.start(argv, container=container, cwd=folder,
                              limits=jobs.Limits(memory=256 << 20, processes=1, ui=True),
                              env=launch.environment([folder]),
                              capabilities=runtime.capabilities(lpac), lpac=lpac) as child, \
                    child.deadline(TIMEOUT_S):
                line = child.stdout.readline()
                if not line:
                    raise OSError(f"the child said nothing; it exited {child.wait(5)}:"
                                  f" {child.stderr.read(400).decode(errors='replace')}")
                found = json.loads(line)
                listening = found.get("listening")
                found["listening_reached"] = bool(listening) and _reachable(listening)
                child.stdin.write(b"\n")
                child.wait(5)
        finally:
            stop.set()
            watcher.join(1)
    return judge(found, seen=reached.is_set())


def judge(found: dict, *, seen: bool) -> str | None:
    """What got through, from the child's report and whether this side's
    listener `seen` a connection; None if nothing did.

    The computer's own address must be refused by the sandbox (WSAEACCES),
    not answered: a "connection refused" there means the attempt reached
    the network stack, and a port that was listening would have taken it.
    A plain AppContainer on the laptop this was written on answers so."""
    got_through = [name for name in ("loopback", "own_address") if found.get(name) == "reached"]
    if found.get("own_address") not in (None, "reached", WSAEACCES):
        got_through.append(f"own_address (answered {found['own_address']}, not refused)")
    if found.get("listening_reached"):
        got_through.append("listening")
    if seen and "loopback" not in got_through:
        got_through.append("loopback (seen by the listener)")
    return ", ".join(got_through) or None


def run() -> Support:
    """Check, record and return this computer's support. Blocking: a second
    or two, so off the event loop."""
    global _verdict
    if (missing := runtime.problem()) is not None:
        _verdict = Support(False, "", missing)
        return _verdict
    try:
        container = profiles.ensure(identity.probe_container(profiles.install()))
    except OSError as error:
        _verdict = Support(False, "", f"Windows would not make an AppContainer here ({error})")
        return _verdict
    failures = []
    try:
        for mode, lpac in ((LPAC, True), (APPCONTAINER, False)):
            try:
                leak = _try(container, lpac)
            except (OSError, ValueError, runtime.NotBuilt) as error:
                failures.append(f"{mode}: {error}")
                continue
            if leak is not None:
                _verdict = Support(False, "", f"a {mode} container here reached {leak}")
                break
            reason = "" if lpac else ("the less-privileged container would not start here,"
                                      " so modules run in a plain one, which can read what"
                                      f" Windows opens to every app ({'; '.join(failures)})")
            _verdict = Support(True, mode, reason)
            break
        else:
            _verdict = Support(False, "", "no container would start here: "
                               + "; ".join(failures))
    finally:
        # Made again at each start, so kept by no one. Kept, every test that
        # started a worker left one in Windows' list of containers, each
        # test's install being its own.
        with contextlib.suppress(OSError):
            profiles.delete(container.name)
    log.info("module sandbox: %s", _verdict)
    return _verdict
