"""The module runtime: a Python of Steve's own, and Node, for containers to run.

A module's container could be given the backend's own Python. It would then
find the backend's `site-packages` and import every dependency Steve has:
the model clients, the keyring, the Google libraries, none of them written
for code that is not trusted. And in a checkout the Python is the `.venv`
launcher, which starts a second process to run the real interpreter; the
job allows a module one process, so nothing would run at all.

So containers run a Python made for them (06-modules.md, section 2):

- **what it holds:** the standard library with `tzdata`, and `steve_sdk`
  in `packages/`; pytest and what it needs in `testing/`, for test runs
  only, off the path otherwise;
- **how it starts:** `-I -S`, with its path fixed by a `._pth` file beside
  the executable, so no environment variable, user folder or current
  folder adds to it;
- **who may read it:** containers carrying the runtime capability
  (`identity.RUNTIME_CAPABILITY`), granted once on the runtime's folder.
  Never the install's own Python.

Node sits beside it, for the local servers of section 7, pinned like an
add-on (core/addons.py) and unpacked into the runtime's folder, so the
same grant covers it.

`python -m core.sandbox build` makes it in a checkout, into the git-ignored
`.runtime/`, from the Python the checkout runs on; step 8 ships it with
the installer. A checkout's `steve_sdk` changes as it is worked on, so
`ready` copies it again when the copy is stale. An install's never is.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.metadata
import json
import msvcrt
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from core import addons, identity, paths
from core.sandbox import grants, profiles

# Node's long-term-support line, its release of 14 October 2026. The zip's
# SHA-256 is the one nodejs.org's SHASUMS256.txt published for it; the tree
# is `addons.tree_hash` of what it unpacked to, taken on 15 October.
NODE = addons.Addon(
    name="node",
    version="24.22.0",
    url="https://nodejs.org/dist/v24.22.0/node-v24.22.0-win-x64.zip",
    size=37_733_703,
    sha256="f9683a19877e12a9b167305c8db71040d812876359de21f0d2e97c3f321f80c3",
    top="node-v24.22.0-win-x64",
    tree="2fdd6719d0e00806e6e30b01e32f4a81c70701779a621ccde9c3dbe50329c19b",
    what="Node",
)

# What a module may import beyond the standard library, and what test runs
# add: pytest, with whatever its own metadata says it requires here. A list
# kept by hand missed pygments when pytest 9.1 began to require it, and no
# module's tests could start.
PACKAGES = ("tzdata",)
TESTING = ("pytest",)
# The standard library's parts no module needs: its own tests, the GUI and
# the installers. About half the files, each one a file a grant walks.
_LEFT_OUT = {"site-packages", "test", "idlelib", "tkinter", "turtledemo", "ensurepip",
             "turtle.py", "_tkinter.pyd", "tcl86t.dll", "tk86t.dll"}
BUILT = "built.json"
LOCK_S = 120.0


class NotBuilt(RuntimeError):
    """The runtime is missing or incomplete, and why, with what makes it."""


def home() -> Path:
    """The runtime's folder: the checkout's git-ignored `.runtime/`, or
    beside the installed code (decision D7)."""
    if paths.layout() == "source":
        return paths.SOURCE / ".runtime"
    return paths.install_dir() / "runtime"


def python(root: Path | None = None) -> Path:
    return (root or home()) / "python" / "python.exe"


def node(root: Path | None = None) -> Path:
    return (root or home()) / "node" / "node.exe"


def testing(root: Path | None = None) -> Path:
    """The folder a test run adds to the path, and nothing else does."""
    return (root or home()) / "python" / "testing"


def capability() -> str:
    """The SID every module's container carries to read the runtime."""
    return profiles.capability(identity.RUNTIME_CAPABILITY)


# How the runtime's Python is started: isolated, without `site`, writing no
# bytecode into folders it may not write, and in UTF-8 whatever the locale.
FLAGS = ("-I", "-S", "-B", "-X", "utf8")
# How Node is started. It resolves each module's real path through every
# folder above it, and a container may not read `C:\`: without these it
# fails before running a line (EPERM, lstat 'C:\'; day one's proof, 15
# October 2026).
NODE_FLAGS = ("--preserve-symlinks", "--preserve-symlinks-main")


def capabilities(lpac: bool) -> tuple[str, ...]:
    """The SIDs a container carries to run the runtime: its capability, and
    under LPAC `registryRead`, without which python.exe does not load
    (spike S1: 0xC0000022)."""
    return (capability(), *((profiles.capability("registryRead"),) if lpac else ()))


def sdk_hash(folder: Path) -> str:
    """One SHA-256 over the package's source files, names and contents:
    not its `__pycache__`, which changes when it is run, not when it is
    edited. A runtime made before this was the add-ons' hash recorded
    another value, and is made again once."""
    return addons.tree_hash(folder, skip=frozenset({"__pycache__"}))


def _record(root: Path) -> dict:
    with contextlib.suppress(OSError, ValueError):
        return json.loads((root / BUILT).read_text(encoding="utf-8"))
    return {}


def required(roots: tuple[str, ...]) -> list[str]:
    """`roots` and every distribution they require on this computer, by
    their metadata, with no extras: each name once, in packaging's form. A
    root not installed here is left out."""
    # packaging comes with pytest, which a checkout has; it is imported here
    # so that a backend without either can still start its modules.
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    found: list[str] = []
    todo = [canonicalize_name(name) for name in roots]
    while todo:
        name = todo.pop(0)
        if name in found:
            continue
        try:
            needs = importlib.metadata.requires(name) or []
        except importlib.metadata.PackageNotFoundError:
            continue
        found.append(name)
        for line in needs:
            need = Requirement(line)
            if need.marker is None or need.marker.evaluate({"extra": ""}):
                todo.append(canonicalize_name(need.name))
    return found


def problem(root: Path | None = None) -> str | None:
    """What is missing from the runtime, or None if it is whole."""
    root = root or home()
    made = _record(root)
    if not python(root).is_file() or not made:
        return f"the module runtime is not built at {root}: run python -m core.sandbox build"
    if not node(root).is_file() or made.get("node") != NODE.version:
        return (f"the module runtime has no Node {NODE.version}:"
                " run python -m core.sandbox build")
    if missing := [name for name in _wanted() if name not in made.get("packages", {})]:
        return (f"the module runtime has no {', '.join(missing)}:"
                " run python -m core.sandbox build")
    return None


@functools.cache
def _wanted() -> tuple[str, ...]:
    return tuple(PACKAGES) + tuple(required(TESTING))


@contextlib.contextmanager
def _lock(root: Path) -> Iterator[None]:
    """One process at a time changes the runtime: the test workers share it."""
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".lock", "a+b") as held:
        held.seek(0)
        deadline = time.monotonic() + LOCK_S
        while True:
            try:
                msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise NotBuilt(f"the module runtime at {root} stayed locked") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)


def ready() -> Path:
    """The runtime's folder, whole, current and open to containers.

    NotBuilt if it was never made: a checkout makes it with `python -m
    core.sandbox build`. A checkout's `steve_sdk` is copied again if the
    copy is stale, and the grant made if it is missing, under a lock.

    Looked at first without the lock, and a runtime that is current is
    left with nothing written. Every module's run asks for it, and in an
    install it lives beside Steve's code, which nothing writes but the
    installer (06-modules.md, exit criteria). Until step 6's item 16 each
    run took the lock, and the first created its file there."""
    root = home()
    if (why := problem(root)) is not None:
        raise NotBuilt(why)
    if not _stale_sdk(root) and grants.holds(root, capability()):
        return root
    with _lock(root):
        if _stale_sdk(root):
            _copy_sdk(root)
        sid = capability()
        if not grants.holds(root, sid):
            grants.grant(root, sid, grants.READ)
    return root


def _stale_sdk(root: Path) -> bool:
    """Whether a checkout's `steve_sdk` changed since the runtime's copy
    was made. An install's copy is the installer's, and never stale."""
    return (paths.is_source_tree()
            and _record(root).get("sdk") != sdk_hash(paths.SOURCE / "steve_sdk"))


def _copy_sdk(root: Path) -> None:
    into = root / "python" / "packages" / "steve_sdk"
    shutil.rmtree(into, ignore_errors=True)
    shutil.copytree(paths.SOURCE / "steve_sdk", into,
                    ignore=shutil.ignore_patterns("__pycache__"))
    made = _record(root)
    made["sdk"] = sdk_hash(paths.SOURCE / "steve_sdk")
    _write_record(root, made)


def _write_record(root: Path, made: dict) -> None:
    (root / BUILT).write_text(json.dumps(made, indent=1, sort_keys=True) + "\n",
                              encoding="utf-8", newline="\n")


def _copy_distribution(name: str, into: Path) -> str:
    """Every file pip installed for `name`, into `into`: its version."""
    dist = importlib.metadata.distribution(name)
    base = Path(dist.locate_file("")).resolve()
    for file in dist.files or ():
        source = Path(dist.locate_file(file)).resolve()
        if not source.is_relative_to(base) or "__pycache__" in source.parts:
            continue  # the console scripts, outside site-packages
        target = into / source.relative_to(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return dist.version


def _copy_python(into: Path) -> None:
    """The interpreter this checkout runs on, less what `_LEFT_OUT` names,
    with its path fixed by a `._pth` file."""
    base = Path(sys.base_prefix)
    into.mkdir(parents=True)
    for item in base.iterdir():
        if item.is_file() and item.suffix.lower() in (".exe", ".dll") \
                and item.name.lower() != "pythonw.exe":
            shutil.copy2(item, into / item.name)
    for folder in ("DLLs", "Lib"):
        shutil.copytree(base / folder, into / folder,
                        ignore=lambda _dir, names: [n for n in names if n in _LEFT_OUT])
    major, minor = sys.version_info[:2]
    (into / f"python{major}{minor}._pth").write_text("Lib\nDLLs\npackages\n",
                                                      encoding="ascii", newline="\n")


def build(root: Path | None = None, *, say: Callable[[str], None] = print,
          grant: bool = True) -> Path:
    """Make the runtime at `root` (its `home` by default), whole: Python,
    its packages, Node, and the capability's grant."""
    root = root or home()
    root.mkdir(parents=True, exist_ok=True)
    with _lock(root):
        python_home = root / "python"
        if python_home.exists():
            shutil.rmtree(python_home)
        say(f"copying Python {sys.version.split()[0]} from {sys.base_prefix}")
        _copy_python(python_home)
        versions = {name: _copy_distribution(name, python_home / "packages")
                    for name in PACKAGES}
        versions |= {name: _copy_distribution(name, python_home / "testing")
                     for name in required(TESTING)}
        shutil.copytree(paths.SOURCE / "steve_sdk", python_home / "packages" / "steve_sdk",
                        ignore=shutil.ignore_patterns("__pycache__"))
        say(f"fetching Node {NODE.version} from nodejs.org, checked against its pinned hash")
        addons.install(NODE, folder=root / "node")
        _write_record(root, {"python": sys.version.split()[0], "node": NODE.version,
                             "packages": versions,
                             "sdk": sdk_hash(paths.SOURCE / "steve_sdk")})
        if grant:
            say(f"opening it to containers that carry {identity.RUNTIME_CAPABILITY}")
            grants.grant(root, capability(), grants.READ)
    say(f"built the module runtime at {root}")
    return root
