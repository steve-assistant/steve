"""Local MCP servers Steve installs and starts, each held in its container.

A server the user runs themselves can read every file and secret they
have, Steve's run file and its token among them, and so approve its own
cards. One Steve starts is held tighter than that (06-modules.md, section
7):

- **It runs in its own container,** one per account it signs in to, with
  a job's memory and CPU caps and one process. It reads its installed
  files and writes its home, nothing else: not the user's files, not the
  Credential Manager, not Steve's data.
- **Its one way out is its pipe** (`core/sandbox/pipe.py`), served by
  `core/sandbox/relay.py`: each connection to a host it was approved for,
  checked as it opens. Steve's loading script (`netpipe.js`) sends Node's
  connections there; code that goes round it finds no network.
- **It is started only from the files approved:** the installed tree is
  hashed at each start and must match the hash the release or the user
  approved. npm installed it, in a container of its own reaching only the
  registry, with install scripts off: from a built-in profile's reviewed
  lockfile (`npm ci`), or, for a server the user adds, resolved as the
  registry stood when that version was published (`--before`), so a
  dependency published since cannot slip in. npm's download cache is
  deleted after each install. Its home, which it writes, is taken off
  Node's search for a module (`netpipe.js`), so nothing it leaves there
  runs at a later start.
- **What it says on stderr** may hold a code or a token, so it is kept
  only redacted, its last lines (`Log`). A sign-in code is read from the
  raw line as it passes (`Log.watch`), and kept in memory alone.
- **Its sign-in stays in its home,** a folder of the account's own in the
  module's, which export never includes; its temporary files are in its
  container's own storage, wiped when it stops. Removing the account
  deletes both (`forget`).

Without AppContainer support (D6), no server starts; the user may still
run one themselves.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from core import addons, identity, paths
from core.engines import mcp
from core.modctx import Hosts, ModuleHttp, Network
from core.sandbox import grants, launch, pipe, probe, profiles, relay, runtime
from core.sandbox import job as jobs

log = logging.getLogger("sandbox.servers")

LOADER = Path(__file__).with_name("netpipe.js")
REGISTRY = "registry.npmjs.org"
SERVER_LIMITS = jobs.Limits(memory=512 << 20, processes=1, cpu_cores=1, ui=True,
                            die_on_unhandled=True)
NPM_LIMITS = jobs.Limits(memory=1 << 30, processes=1, cpu_cores=1, ui=True,
                         die_on_unhandled=True)
INSTALL_S = 600.0
SETTLED = dt.timedelta(days=7)   # how old a version the user names must be
START_S = 60.0
MODULE_KEY = "module"            # the key a module's own server runs under, beside accounts'
IDLE_S = 900.0                   # a server no call has used this long is stopped
LOG_LINES = 200
LOG_LINE = 500
TREE = "tree"
OWN = "steve"          # beside the tree: Steve's loading script, and authorities a test adds
NPM_FLAGS = ("--ignore-scripts", "--no-audit", "--no-fund")
_PACKAGE = re.compile(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")
_VERSION = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")
_KEY = re.compile(r"[A-Za-z0-9_-]{1,40}")    # a name, never a path
_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_RESERVED_ENV = frozenset({"HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                           "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
                           "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"})
_BEFORE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


class NotHeld(RuntimeError):
    """This computer cannot hold a server in, so none starts."""


class NotInstalled(RuntimeError):
    """The server's files are missing, or are not the ones approved."""


@dataclass(frozen=True)
class SignIn:
    """Where a server sends the user to sign in, and the shape of its code:
    only a page the profile names and a code of its shape are shown. The
    tool that starts the sign-in, if the server waits to be asked; the tool
    that says who is signed in, and where in its answer the address is,
    which must be the account's; how long a code lasts."""

    page: str
    code: str
    tool: str = ""
    whoami: str = ""
    address: str = ""
    expires_s: int = 900

    def __post_init__(self) -> None:
        if bool(self.whoami) != bool(self.address):
            raise ValueError("whoami and address are named together")
        if not self.page.startswith("https://") or any(c.isspace() for c in self.page):
            raise ValueError(f"not a sign-in page: {self.page!r}")
        re.compile(self.code)


def prompt(text: str, sign_in: SignIn) -> tuple[str, str] | None:
    """The page and code a server's words ask the user to sign in with, if
    they name the profile's page exactly and hold a code of its shape. The
    page shown is the profile's, never one the server wrote, so a server
    cannot send the user anywhere else."""
    urls = {u.rstrip(".,;:)'\"") for u in re.findall(r"https://\S+", text)}
    if sign_in.page not in urls:
        return None
    for token in re.findall(r"[A-Za-z0-9-]{4,40}", text):
        if re.fullmatch(sign_in.code, token):
            return sign_in.page, token
    return None


@dataclass(frozen=True)
class Package:
    """A server's npm package, as a profile or a server module names it."""

    name: str
    version: str
    entry: str                                   # its start script, within the package
    hosts: dict[str, tuple[int, ...]]            # where its connections may go
    args: tuple[str, ...] = ()
    anywhere: bool = False
    lockfile: Path | None = None                 # a built-in profile's, reviewed
    tree: str | None = None                      # the installed tree's approved hash
    before: str | None = None                    # resolve as the registry stood then
    sign_in: SignIn | None = None
    env: dict[str, str] = field(default_factory=dict)   # the server's own settings

    def __post_init__(self) -> None:
        if not _PACKAGE.fullmatch(self.name) or not _VERSION.fullmatch(self.version):
            raise ValueError(f"not an exact npm package: {self.name}@{self.version}")
        entry = Path(self.entry)
        if entry.is_absolute() or ".." in entry.parts or entry.suffix not in (
                ".js", ".cjs", ".mjs"):
            raise ValueError(f"not a script within the package: {self.entry!r}")
        if self.before is not None and not _BEFORE.fullmatch(self.before):
            raise ValueError(f"not a moment npm takes: {self.before!r}")
        for name in self.env:
            # Steve's own names say where the server's home, its network
            # and its code are; a package's setting may not move them.
            if not _ENV.fullmatch(name) or name.startswith(("NODE_", "NPM_", "STEVE_")) \
                    or name in _RESERVED_ENV:
                raise ValueError(f"not a setting a package may give: {name!r}")

    @property
    def reach(self) -> relay.Reach:
        return relay.Reach(dict(self.hosts), anywhere=self.anywhere)


# --- where its files are ------------------------------------------------------

def folder(module_id: str, package: Package | str) -> Path:
    """One installed version (a Package, or name@version): its tree, and
    Steve's own files beside it."""
    named = package if isinstance(package, str) else f"{package.name}@{package.version}"
    name, _, version = named.rpartition("@")
    return paths.module_dir(module_id) / "server" / f"{name.replace('/', '+')}@{version}"


def home(module_id: str, key: str) -> Path:
    """The server's home for one account: its sign-in, as it keeps it.
    The no-secret-on-disk guard's one exception, by this path."""
    if not _KEY.fullmatch(key or ""):
        raise ValueError(f"not an account key: {key!r}")
    return paths.module_dir(module_id) / "server-homes" / key


def container_name(module_id: str, key: str) -> str:
    return identity.server_container(profiles.install(), module_id, key)


def remove(path: Path) -> None:
    """Delete a folder npm or a server wrote, whole. Node writes paths past
    Windows' 260 characters (npm's cache alone nests a 128-character hash
    six folders down), which Python reaches only by the extended `\\\\?\\`
    form; without it, and with errors ignored, the first version left every
    install's cache behind. Files left read-only are made writable first."""

    def writable(function, name, _error) -> None:
        with contextlib.suppress(OSError):
            os.chmod(name, stat.S_IWRITE)
            function(name)

    if path.exists():
        shutil.rmtree(_extended(path), onexc=writable)


def _extended(path: Path) -> str:
    """`path` in the form Windows reads past 260 characters."""
    full = str(path.resolve())
    if full.startswith("\\\\?\\"):
        return full
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


def _storage(name: str) -> Path:
    """A container's own folder, where Windows points its LOCALAPPDATA and
    TEMP; deleted with the profile."""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Packages" / name / "AC"


# --- the log --------------------------------------------------------------------

class Log:
    """What a server says on stderr, redacted, its last `LOG_LINES` lines.
    Each watcher sees each raw line first, and keeps what it keeps."""

    def __init__(self, redact: Callable[[str], str]) -> None:
        self._redact = redact
        self.lines: deque[str] = deque(maxlen=LOG_LINES)
        self._watchers: list[Callable[[str], None]] = []
        self._lock = threading.Lock()

    def watch(self, watcher: Callable[[str], None]) -> None:
        with self._lock:
            self._watchers.append(watcher)

    def unwatch(self, watcher: Callable[[str], None]) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._watchers.remove(watcher)

    def add(self, raw: str) -> None:
        with self._lock:
            watchers = list(self._watchers)
        for watcher in watchers:
            try:
                watcher(raw)
            except Exception:  # a watcher's fault is not the server's line's
                log.exception("a server log's watcher failed")
        line = self._redact(raw.rstrip("\r\n"))[:LOG_LINE]
        with self._lock:
            self.lines.append(line)

    def drain(self, stream: BinaryIO) -> None:
        with contextlib.suppress(OSError, ValueError):
            for raw in iter(lambda: stream.readline(LOG_LINE * 8), b""):
                self.add(raw.decode("utf-8", errors="replace"))


# --- installing ------------------------------------------------------------------

@dataclass(frozen=True)
class Installed:
    tree: str          # the tree's hash, which the card shows and a start checks
    packages: int      # how many packages npm installed
    size: int          # bytes on disk


def _supported() -> bool:
    """Whether containers here are less-privileged; NotHeld if none run."""
    support = probe.verdict()
    if support is None or not support.ok:
        raise NotHeld(support.reason if support is not None
                      else "the sandbox has not been checked on this computer yet")
    return support.mode == probe.LPAC


def _manifest(package: Package) -> str:
    return json.dumps({"name": "steve-server", "version": "0.0.0", "private": True,
                       "dependencies": {package.name: package.version}},
                      indent=1) + "\n"


def tree_hash(where: Path) -> str:
    return addons.tree_hash(where / TREE)


def npm_argv(package: Package, *, now: dt.datetime | None = None) -> list[str]:
    """npm's command: `ci` from a reviewed lockfile, or an install
    resolved as the registry stood at `before`; scripts off either way.

    `before` is held to `SETTLED` here, whoever set it. The age was checked
    only where the user names a version (`published`), but a server
    module's manifest carries its own `before`, and one imported from a zip
    could name today: npm then resolved the newest of every dependency.
    Whatever npm finds as the registry stood a week ago is a week old."""
    if package.lockfile is not None:
        return ["ci", *NPM_FLAGS]
    if package.before is None:
        raise NotInstalled(f"{package.name}@{package.version} has neither a lockfile nor "
                           "the moment it was published")
    _settled(package.name, package.version, package.before, now)
    return ["install", "--save-exact", f"--before={package.before}", *NPM_FLAGS]


def _settled(name: str, version: str, moment: str, now: dt.datetime | None) -> None:
    at = dt.datetime.fromisoformat(moment.replace("Z", "+00:00"))
    if (now or dt.datetime.now(dt.UTC)) - at < SETTLED:
        raise NotInstalled(f"{name}@{version} was published {at:%Y-%m-%d}: Steve installs a "
                           f"version only once it is {SETTLED.days} days old")


def published(name: str, version: str, *, network: Network,
              now: dt.datetime | None = None) -> str:
    """When the registry says `name@version` was published, for a server
    the user adds: its `before`. A version younger than `SETTLED` is
    refused, since a hijacked release is usually caught and pulled within
    days, and one named before then could be it. Asked through the broker,
    as any module's request is, of the registry alone."""
    http = ModuleHttp(hosts=Hosts(fixed=(REGISTRY,)), bindings={},
                      secret=lambda _name: None, network=network, https_only=True)
    answer = http.request("GET", f"https://{REGISTRY}/{quote(name, safe='@')}",
                          headers={"Accept": "application/json"})
    if not answer.ok:
        raise NotInstalled(f"the registry has no {name} (it answered {answer.status})")
    try:
        moment = answer.json()["time"][version]
    except (ValueError, KeyError, TypeError):
        raise NotInstalled(f"the registry has no {name}@{version}") from None
    if not isinstance(moment, str) or not _BEFORE.fullmatch(moment):
        raise NotInstalled(f"the registry gave {name}@{version} no publish date")
    _settled(name, version, moment, now)
    return moment


def _own(where: Path, network: Network) -> tuple[Path, dict[str, str]]:
    """Steve's loading script beside the tree, copied at each start so the
    one Steve ships is the one that runs; and a test's authorities."""
    own = where / OWN
    own.mkdir(parents=True, exist_ok=True)
    loader = own / LOADER.name
    shutil.copyfile(LOADER, loader)
    env: dict[str, str] = {}
    if (pem := network.node_authorities()) is not None:
        (own / "authorities.pem").write_text(pem, encoding="utf-8", newline="\n")
        env["NODE_EXTRA_CA_CERTS"] = str(own / "authorities.pem")
    return loader, env


def _home_env(home_dir: Path) -> dict[str, str]:
    # Windows points LOCALAPPDATA and TEMP at the container's own storage;
    # the rest a program finds its home by are this folder.
    return {"HOME": str(home_dir), "USERPROFILE": str(home_dir),
            "APPDATA": str(home_dir / "AppData" / "Roaming")}


_TURNS: dict[str, threading.Lock] = {}
_TURNS_LOCK = threading.Lock()


def _turn(module_id: str) -> threading.Lock:
    with _TURNS_LOCK:
        return _TURNS.setdefault(module_id, threading.Lock())


def install(module_id: str, package: Package, *, network: Network,
            redact: Callable[[str], str], timeout_s: float = INSTALL_S) -> Installed:
    """npm installs `package` in a container of its own, writing only the
    server's folder and reaching only the registry. Blocking.

    One module's installs take turns. Each deletes and writes the
    version's folder, and npm's container is named for the module, which
    the first to end deletes under the other: two accounts signing in at
    once, or an approval and a call, each found the server not installed
    and ran npm into one folder. One that waited finds the tree approved
    installed, and keeps it."""
    with _turn(module_id):
        if installed(module_id, package):
            # As approved: its hash is the approval's, just checked.
            return _described(folder(module_id, package), str(package.tree))
        return _install(module_id, package, network=network, redact=redact,
                        timeout_s=timeout_s)


def _described(where: Path, hashed: str) -> Installed:
    """What is installed in `where`, whose tree hashes to `hashed`."""
    tree = where / TREE
    lock = json.loads((tree / "package-lock.json").read_text(encoding="utf-8"))
    count = sum(1 for key in lock.get("packages", {}) if key.startswith("node_modules/"))
    size = sum(p.stat().st_size for p in tree.rglob("*") if p.is_file())
    return Installed(tree=hashed, packages=count, size=size)


def _install(module_id: str, package: Package, *, network: Network,
             redact: Callable[[str], str], timeout_s: float) -> Installed:
    lpac = _supported()
    argv_tail = npm_argv(package)
    where = folder(module_id, package)
    remove(where)
    tree = where / TREE
    tree.mkdir(parents=True)
    (tree / "package.json").write_text(_manifest(package), encoding="utf-8", newline="\n")
    if package.lockfile is not None:
        shutil.copyfile(package.lockfile, tree / "package-lock.json")
    work = paths.scratch_dir() / f"npm-{secrets.token_hex(6)}"
    (work / "home").mkdir(parents=True)
    (work / "cache").mkdir()
    for name in ("userrc", "globalrc"):
        (work / name).write_text("", encoding="utf-8")
    name = identity.npm_container(profiles.install(), module_id)
    said = Log(redact)
    try:
        container = profiles.ensure(name)
        grants.grant(where, container.sid, grants.MODIFY)
        grants.grant(work, container.sid, grants.MODIFY)
        node = runtime.node(runtime.ready())
        npm = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        loader, extra = _own(where, network)
        held = pipe.Pipe(container.sid, profiles.install())
        env = launch.environment([node.parent], {
            **_home_env(work / "home"), **extra, "STEVE_PIPE": held.name,
            "npm_config_cache": str(work / "cache"),
            "npm_config_userconfig": str(work / "userrc"),
            "npm_config_globalconfig": str(work / "globalrc"),
            "npm_config_registry": f"https://{REGISTRY}/",
            "npm_config_update_notifier": "false", "npm_config_logs_max": "0",
            "npm_config_progress": "false"})
        argv = [str(node), *runtime.NODE_FLAGS, "--require", str(loader), str(npm),
                *argv_tail]
        try:
            process = launch.start(argv, container=container, cwd=tree, limits=NPM_LIMITS,
                                   env=env, capabilities=runtime.capabilities(lpac),
                                   lpac=lpac)
        except OSError:
            held.close()
            raise
        with process:
            served = relay.Relay(held, pid=process.pid,
                                 reach=relay.Reach({REGISTRY: (443,)}), network=network,
                                 note=said.add).start()
            drain = threading.Thread(target=said.drain, args=(process.stderr,), daemon=True)
            drain.start()
            try:
                with process.deadline(timeout_s):
                    said.drain(process.stdout)
                    code = process.wait(30)
            finally:
                served.stop()
                drain.join(5)
        if code != 0:
            why = "it ran past its time" if process.timed_out else f"exit {code}"
            last = " | ".join(list(said.lines)[-6:])
            raise NotInstalled(f"npm did not install {package.name}@{package.version} "
                               f"({why}): {last}")
    finally:
        remove(work)
        profiles.delete(name)
    hashed = tree_hash(where)
    if package.tree is not None and hashed != package.tree:
        # A release's lockfile names each package's integrity, but not
        # what npm made of them; a tree other than the one reviewed is
        # not started, so it is not kept.
        remove(where)
        raise NotInstalled(f"npm installed {package.name}@{package.version} as files other "
                           "than the ones approved")
    return _described(where, hashed)


def counted(module_id: str, named: str) -> int:
    """How many packages the install of `named` (name@version) brought, by
    its lockfile; 0 if it is not installed."""
    where = folder(module_id, named) / TREE / "package-lock.json"
    try:
        lock = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return sum(1 for key in lock.get("packages", {}) if key.startswith("node_modules/"))


def installed(module_id: str, package: Package) -> bool:
    """Whether `package` is installed as approved, so it may start."""
    where = folder(module_id, package)
    return (package.tree is not None and (where / TREE).is_dir()
            and tree_hash(where) == package.tree)


# --- running ------------------------------------------------------------------

@dataclass(eq=False)
class Running:
    """A server started, and how Steve speaks to it."""

    module_id: str
    key: str
    container: str
    process: launch.Process
    relay: relay.Relay
    client: mcp.Stdio
    log: Log
    _drain: threading.Thread = field(repr=False)

    def alive(self) -> bool:
        return self.process.returncode() is None

    def stop(self) -> None:
        """End it, its connections, and its temporary files."""
        self.relay.stop()
        self.process.close()
        self._drain.join(5)
        remove(_storage(self.container) / "Temp")


def start(module_id: str, key: str, package: Package, *, network: Network,
          redact: Callable[[str], str], timeout_s: float = START_S,
          watch: Callable[[str], None] | None = None) -> Running:
    """Start the server installed for `package` for one account, its tree
    checked against the hash approved, and initialise it. Blocking.
    `watch` sees each raw line it says from its first (`Log.watch`)."""
    lpac = _supported()
    where = folder(module_id, package)
    if not installed(module_id, package):
        raise NotInstalled(f"{package.name}@{package.version} is not installed as approved")
    entry = where / TREE / "node_modules" / package.name / package.entry
    home_dir = home(module_id, key)
    home_dir.mkdir(parents=True, exist_ok=True)
    name = container_name(module_id, key)
    container = profiles.ensure(name)
    for path, mask in ((where, grants.READ), (home_dir, grants.MODIFY)):
        if not grants.holds(path, container.sid):
            grants.grant(path, container.sid, mask)
    loader, extra = _own(where, network)
    node = runtime.node(runtime.ready())
    held = pipe.Pipe(container.sid, profiles.install())
    env = launch.environment([], {**package.env, **_home_env(home_dir), **extra,
                                  "STEVE_PIPE": held.name})
    argv = [str(node), *runtime.NODE_FLAGS, "--require", str(loader), str(entry),
            *package.args]
    try:
        process = launch.start(argv, container=container, cwd=where / TREE,
                               limits=SERVER_LIMITS, env=env,
                               capabilities=runtime.capabilities(lpac), lpac=lpac)
    except OSError:
        held.close()
        raise
    said = Log(redact)
    if watch is not None:
        said.watch(watch)
    served = relay.Relay(held, pid=process.pid, reach=package.reach, network=network,
                         note=said.add).start()
    drain = threading.Thread(target=said.drain, args=(process.stderr,), daemon=True,
                             name="server-stderr")
    drain.start()
    running = Running(module_id, key, name, process, served,
                      mcp.Stdio(process.stdin, process.stdout, timeout=timeout_s), said,
                      drain)
    try:
        running.client.open()
    except Exception:
        running.stop()
        raise
    return running


def forget(module_id: str, key: str) -> None:
    """An account's server gone: stopped, then its home, and its container
    with the container's own storage. Nothing if it never ran."""
    POOL.stop(module_id, key)
    home_dir = home(module_id, key)
    if not home_dir.exists():
        return
    remove(home_dir)
    profiles.delete(container_name(module_id, key))


# --- the servers running ------------------------------------------------------

class Pool:
    """The servers running, one per (module, account): started when a call
    needs one, stopped when idle (`stop_idle`), and stopped before their
    account's files are deleted (`forget`), since a running server holds
    its home open and would write it again. One that ended by itself is
    started afresh on the next call, and counted (`crashes`)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 starter: Callable[..., Running] | None = None) -> None:
        self._clock = clock
        self._starter = starter
        self._lock = threading.Lock()
        self._held: dict[tuple[str, str], Running] = {}
        self._used: dict[tuple[str, str], float] = {}
        self._gates: dict[tuple[str, str], threading.Lock] = {}
        self.crashes: dict[tuple[str, str], int] = {}

    def _gate(self, at: tuple[str, str]) -> threading.Lock:
        with self._lock:
            return self._gates.setdefault(at, threading.Lock())

    def running(self, module_id: str, key: str) -> Running | None:
        with self._lock:
            return self._held.get((module_id, key))

    def client(self, module_id: str, key: str, package: Package, *, network: Network,
               redact: Callable[[str], str],
               watch: Callable[[str], None] | None = None) -> mcp.Stdio:
        """The account's server's client, started if it is not running.
        Blocking: a start installs nothing, but waits for the server.
        `watch` sees what it says from then on, from its first line if it
        starts here; the caller unwatches it (`Log.unwatch`)."""
        at = (module_id, key)
        with self._gate(at):
            running = self.running(module_id, key)
            if running is not None and not running.alive():
                self.crashes[at] = self.crashes.get(at, 0) + 1
                log.warning("%s's server for %s ended by itself", module_id, key)
                self._drop(at)
                running = None
            if running is None:
                running = (self._starter or start)(module_id, key, package,
                                                   network=network, redact=redact,
                                                   watch=watch)
                with self._lock:
                    self._held[at] = running
            elif watch is not None:
                running.log.watch(watch)
            with self._lock:
                self._used[at] = self._clock()
            return running.client

    def _drop(self, at: tuple[str, str]) -> None:
        with self._lock:
            running = self._held.pop(at, None)
            self._used.pop(at, None)
        if running is not None:
            try:
                running.stop()
            except Exception:  # the next start begins clean either way
                log.exception("stopping %s's server for %s failed", *at)

    def stop(self, module_id: str, key: str) -> None:
        at = (module_id, key)
        with self._gate(at):
            self._drop(at)

    def stop_idle(self, idle_s: float = IDLE_S) -> list[tuple[str, str]]:
        """Stop each server no call has used for `idle_s`; those stopped."""
        now = self._clock()
        with self._lock:
            idle = [at for at, used in self._used.items() if now - used >= idle_s]
        for at in idle:
            self.stop(*at)
        return idle

    def stop_module(self, module_id: str) -> None:
        """Every server of one module: switched off, it runs none."""
        with self._lock:
            held = [at for at in self._held if at[0] == module_id]
        for at in held:
            self.stop(*at)

    def stop_all(self) -> None:
        with self._lock:
            held = list(self._held)
        for at in held:
            self.stop(*at)


POOL = Pool()
