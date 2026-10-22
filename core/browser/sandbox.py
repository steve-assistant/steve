"""Where the browser runs: a throwaway pair of containers per session, on a
network of their own, holding nothing of theirs.

The roadmap's first rule for a browser was never to hand the agent the user's
own logged-in one, and its second that a page is hostile until shown
otherwise. So each session is built from nothing and thrown away:

- **A network with no way out** (`docker network create --internal`): no
  route to the internet, this laptop, their router or another session.
- **A gate** (`core/browser/gate.py`) on that network and on Docker's bridge:
  the browser's proxy, and the only thing that can reach anywhere, under
  `core/egress`'s rules.
- **The browser** on the internal network only: Chromium driven by
  `core/browser/driver.py` over `docker exec`, so there is no port to reach
  it by. A fresh, empty profile; no mounts, no published ports, no
  credentials; a read-only root, every capability dropped, no new
  privileges, a non-root user; memory, CPU and process caps; external DNS
  pointed at nothing, so the gate is the only resolver.
- **A lifetime of its own.** Both containers' commands end after
  `LIFETIME_S` (`timeout`), and `--rm` removes them, whatever happens to the
  worker that started them. `reap` clears what a dead worker left -- only a
  dead one's, because two workers overlap during a swap (protocols §4).

The images are built from the Dockerfiles below and the frozen files they
copy, tagged by a hash of all of it, so a change to the driver or the gate
is a new image rather than a stale one. A session runs the images by the
IDs their builds recorded, never by tag (`core/images.py`): a tag is a name
any account allowed to use Docker can point elsewhere. Docker is called with a timeout
every time (protocols §5), through `_docker` looked up when called -- never
bound as a default -- so the unit tests' fence on it covers every path.
Frozen, with the rest of `core/`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core import identity, images

HERE = Path(__file__).resolve().parent
CORE = HERE.parent

LABEL = identity.DOCKER_LABEL
# By digest as well as tag: a tag is moved by whoever publishes it, and a
# build on another day would have taken whatever it named then (F2). The
# playwright digest is its multi-platform index; python's is 3.13-slim's.
BROWSER_BASE = ("mcr.microsoft.com/playwright/python:v1.63.0-noble"
                "@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0")
GATE_BASE = ("python:3.13-slim"
             "@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f")
GATE_PORT = 3128
# How long a session may live, whatever the worker does. The gate outlives
# its browser slightly so the browser's last requests are not cut off.
LIFETIME_S = 1800
GATE_LIFETIME_S = LIFETIME_S + 60

GATE_DOCKERFILE = f"""\
FROM {GATE_BASE}
COPY egress.py gate.py /gate/
WORKDIR /gate
"""
# The base carries the browsers but not the Python packages that drive
# them: Playwright, and Patchright, a patched Playwright with the same API,
# which the driver uses when it is there (`driver.load_engine`). Both are
# pinned with their hashes in requirements.txt, beside this file.
#
# `patchright install chromium` used to fetch the browser Patchright expects,
# from a download no hash covered; while the revisions agree it fetched
# nothing, since the base's /ms-playwright already holds it. Now the build
# checks that the Chromium Patchright would launch is there, and fails if
# it is not: a new Patchright that wants another revision is a new base.
BROWSER_DOCKERFILE = f"""\
FROM {BROWSER_BASE}
COPY requirements.txt /opt/agent/requirements.txt
RUN python -m pip install --no-cache-dir --break-system-packages --require-hashes \\
    -r /opt/agent/requirements.txt \\
 && python -c "import os, sys; from patchright.sync_api import sync_playwright; \\
p = sync_playwright().start(); path = p.chromium.executable_path; p.stop(); \\
sys.exit(0 if os.path.exists(path) else 'no Chromium for Patchright at ' + path)"
COPY egress.py driver.py probe.py /opt/agent/
"""

Runner = Callable[[list[str], float], subprocess.CompletedProcess]


def _docker(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def _context(kind: str) -> dict[str, bytes]:
    """The build context for `kind` ("gate" or "browser"): file name to bytes."""
    egress = (CORE / "egress.py").read_bytes()
    if kind == "gate":
        return {"Dockerfile": GATE_DOCKERFILE.encode(), "egress.py": egress,
                "gate.py": (HERE / "gate.py").read_bytes()}
    if kind == "browser":
        return {"Dockerfile": BROWSER_DOCKERFILE.encode(), "egress.py": egress,
                "requirements.txt": (HERE / "requirements.txt").read_bytes(),
                "driver.py": (HERE / "driver.py").read_bytes(),
                "probe.py": (HERE / "probe.py").read_bytes()}
    raise ValueError(kind)


KINDS = ("gate", "browser")


def image(kind: str) -> str:
    """The image for `kind`, tagged by a hash of everything it is built from."""
    return images.tag(f"browser-{kind}", _context(kind))


def build(run: Runner | None = None, *, force: bool = False) -> list[str]:
    """Build whichever image Steve has not built here; return the tags built.
    Slow the first time (the browser base is about 2 GB), then cached."""
    run = run or _docker
    built = []
    for kind in KINDS:
        tag = image(kind)
        if force or images.built(tag, run) is None:
            images.build(tag, _context(kind), run, timeout=3600)
            built.append(tag)
    return built


def available(run: Runner | None = None) -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        # `version`, not `info`: the same answer without running every CLI
        # plugin to list them (tools/code_exec.py DAEMON_VERSION).
        return (run or _docker)(["version", "--format", "{{.Server.Version}}"],
                                15).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def ready(run: Runner | None = None) -> bool:
    """Docker answers and Steve has built both images here."""
    run = run or _docker
    return available(run) and all(images.built(image(k), run) for k in KINDS)


# ---------------------------------------------------------------------------
# A session's containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sandbox:
    session: str
    owner: str
    network: str
    gate: str
    browser: str
    proxy: str = ""


def names(session: str) -> tuple[str, str, str]:
    head = f"{identity.DOCKER_NAMES}browser-{session}"
    return f"{head}-net", f"{head}-gate", head


def install_labels() -> list[str]:
    """The label on everything Steve starts in Docker: this install's id, so
    that an uninstall, or a worker reaping what a dead one left, takes only
    its own install's (core/uninstall.py)."""
    from core.sandbox import profiles

    return ["--label", f"{identity.DOCKER_INSTALL_LABEL}={profiles.install()}"]


def _labels(session: str, owner: str) -> list[str]:
    return ["--label", f"{LABEL}=1", "--label", f"{LABEL}.owner={owner}",
            "--label", f"{LABEL}.session={session}", *install_labels()]


# Held by both containers. Each is pinned by tests/gate/test_browser_contained.py.
HARDENING = ("--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges")


def network_args(session: str, owner: str) -> list[str]:
    network, _, _ = names(session)
    return ["network", "create", "--internal", *_labels(session, owner), network]


def gate_args(session: str, owner: str, image_id: str) -> list[str]:
    _, gate, _ = names(session)
    return ["run", "-d", "--rm", "--name", gate, "--network", "bridge",
            *_labels(session, owner), *HARDENING, "--user", "65534:65534",
            "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5", "--pids-limit", "64",
            "--tmpfs", "/tmp:rw,size=16m,mode=1777",
            image_id, "timeout", str(GATE_LIFETIME_S), "python", "/gate/gate.py"]


def connect_args(session: str) -> list[str]:
    network, gate, _ = names(session)
    return ["network", "connect", network, gate]


def browser_args(session: str, owner: str, image_id: str) -> list[str]:
    network, _, browser = names(session)
    return ["run", "-d", "--rm", "--name", browser, "--network", network,
            # The only resolver it may use is the gate's: lookups of its own
            # go nowhere, so a page cannot leak a name through DNS.
            "--dns", "127.0.0.1",
            *_labels(session, owner), *HARDENING, "--user", "pwuser",
            # 3 GiB, not 2 (8 October 2026, the user approved): Chromium was
            # OOM-killed at the 8th LinkedIn page in one browser, each page
            # adding 130-200 MB on top of the shared memory and tmpfs that
            # count against the cap. The laptop has 31.7 GiB. Memory-swap
            # equal to memory keeps swap out of it.
            "--memory", "3g", "--memory-swap", "3g", "--cpus", "2", "--pids-limit", "512",
            "--shm-size", "1g",
            "--tmpfs", "/tmp:rw,size=256m,mode=1777",
            "--tmpfs", "/home/pwuser:rw,size=512m,mode=1777",
            image_id, "timeout", str(LIFETIME_S), "sleep", "infinity"]


def driver_argv(sandbox: Sandbox) -> list[str]:
    """The command the worker runs to talk to the driver: over `docker exec`
    stdio, so there is no port anything could reach it by."""
    return ["docker", "exec", "-i", sandbox.browser, "python", "/opt/agent/driver.py"]


def start(owner: str, run: Runner | None = None, session: str | None = None) -> Sandbox:
    """Bring up a session's network, gate and browser, or nothing. Blocking."""
    run = run or _docker
    ids = {kind: images.built(image(kind), run) for kind in KINDS}
    if not all(ids.values()):
        raise RuntimeError("the browser's images are not built here")
    session = session or uuid.uuid4().hex[:10]
    network, gate, browser = names(session)
    made = Sandbox(session=session, owner=owner, network=network, gate=gate, browser=browser)
    try:
        for args, what in ((network_args(session, owner), "the network"),
                           (gate_args(session, owner, ids["gate"]), "the gate"),
                           (connect_args(session), "the gate's link to the network"),
                           (browser_args(session, owner, ids["browser"]), "the browser")):
            done = run(args, 120)
            if done.returncode != 0:
                raise RuntimeError(f"could not start {what}: {done.stderr.strip()[-400:]}")
        found = run(["inspect", "-f",
                     "{{(index .NetworkSettings.Networks \"" + network + "\").IPAddress}}", gate],
                    30)
        ip = found.stdout.strip()
        if found.returncode != 0 or not ip:
            raise RuntimeError("could not find the gate's address on the session network")
    except BaseException:
        stop(made, run)
        raise
    return Sandbox(session=session, owner=owner, network=network, gate=gate, browser=browser,
                   proxy=f"http://{ip}:{GATE_PORT}")


def stop(sandbox: Sandbox, run: Runner | None = None) -> None:
    """Remove a session's containers and network. Never raises."""
    run = run or _docker
    for args in (["rm", "-f", sandbox.browser, sandbox.gate], ["network", "rm", sandbox.network]):
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            run(args, 60)


def leftovers(session: str, run: Runner | None = None) -> list[str]:
    """Whatever of a session is still there: containers and networks."""
    run = run or _docker
    flt = f"label={LABEL}.session={session}"
    found = run(["ps", "-a", "--filter", flt, "--format", "{{.Names}}"], 30).stdout.split()
    found += run(["network", "ls", "--filter", flt, "--format", "{{.Name}}"], 30).stdout.split()
    return found


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------


def this_process() -> str:
    """pid and start time, because a pid alone is reused (as `agent/jobs`)."""
    import psutil

    return f"{os.getpid()}:{psutil.Process().create_time():.0f}"


def alive(owner: str) -> bool:
    import psutil

    pid_text, _, started = (owner or "").partition(":")
    try:
        return abs(psutil.Process(int(pid_text)).create_time() - float(started)) < 2
    except (ValueError, psutil.Error):
        return False


def reap(run: Runner | None = None, is_alive: Callable[[str], bool] = alive) -> list[str]:
    """Remove the sessions whose owning process is gone; return what went.
    A live owner's are left alone, so the worker taking over during a swap
    does not tear down the sessions of the one still draining; another
    install's are its own to reap, and Docker's filter is checked here."""
    from core.sandbox import profiles

    run = run or _docker
    install = profiles.install()
    keys = ('{{.Label "' + LABEL + '.owner"}}\t'
            '{{.Label "' + identity.DOCKER_INSTALL_LABEL + '"}}')
    flt = ["--filter", f"label={LABEL}=1",
           "--filter", f"label={identity.DOCKER_INSTALL_LABEL}={install}"]
    containers = run(["ps", "-a", *flt, "--format", "{{.Names}}\t" + keys],
                     30).stdout.splitlines()
    networks = run(["network", "ls", *flt, "--format", "{{.Name}}\t" + keys],
                   30).stdout.splitlines()
    gone: list[str] = []
    for kind, rows in (("container", containers), ("network", networks)):
        for row in rows:
            name, owner, theirs = [*row.split("\t"), "", ""][:3]
            if not name.strip() or theirs.strip() != install or is_alive(owner.strip()):
                continue
            args = ["rm", "-f", name] if kind == "container" else ["network", "rm", name]
            if run(args, 60).returncode == 0:
                gone.append(name)
    return gone
