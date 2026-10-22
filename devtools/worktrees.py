"""Scratch copies of this repository, for running tests on code you are not
looking at.

Two jobs need one: the mutation runner (`devtools.mutate`) breaks code on
purpose and the proof runner (`devtools.prove`) runs a new test against the
code from before the change. Both used to be done in the tree being worked
in -- stash, edit, run, restore -- and a session that died halfway left the
tree edited.

So the copy is a `git worktree add --detach`, which shares the object store
(a second or two, not a clone), and the scratch work happens there. Nothing
in this module writes to `root` except git's own registry of worktrees,
which `remove` and `prune` clean up.
"""

from __future__ import annotations

import _winapi
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from core import identity, paths

# Where an installed Steve keeps what it runs on: its data, and its code in
# `Programs` (decision D7). A development checkout is never live: the
# installed app runs its own copy, so the only trees to refuse are those.
# (The tree these tools started from ran its live agent's workers from its
# own working tree, so for it "left edited" meant "running edited".)
_LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
LIVE = (_LOCAL / identity.NAME, _LOCAL / "Programs" / identity.NAME)

Runner = Callable[..., subprocess.CompletedProcess]


def is_live(root: Path) -> bool:
    """Whether `root` is, or is inside, an installed Steve's folders."""
    try:
        root = root.resolve()
        return any(root.is_relative_to(live.resolve()) for live in LIVE)
    except OSError:
        return False


def refuse_live(root: Path) -> None:
    if is_live(root):
        raise SystemExit("refused: this is an installed Steve. Run it from a checkout of your own.")


def add(root: Path, path: Path, rev: str = "HEAD", runner: Runner = subprocess.run) -> None:
    """A detached worktree of `rev` at `path`, which must not be in use."""
    if path.exists():
        _unshare_builds(path)
        shutil.rmtree(path, ignore_errors=True)
    runner(["git", "-C", str(root), "worktree", "add", "-q", "--detach", str(path), rev],
           check=True, capture_output=True)
    share_builds(root, path)


# The module runtime (core/sandbox/runtime.py) is built into a checkout's
# git-ignored `.runtime/`, so a scratch tree has none, and the sandbox's
# escape tests fail there for want of it: each mutation of a wall would be
# "caught" by the missing runtime, before the wall was tried. Building one
# for each tree costs a minute and Node's download, so a tree is given a
# junction to this checkout's. One thing crosses it: a tree whose
# `steve_sdk` differs copies its own in when its tests start (`runtime.ready`).
# The screens' tests are the same case: they build the UI with the checkout's
# git-ignored `ui/node_modules` and skip without it, and a mutation run read
# every skip as a pass, so a broken stylesheet was "missed" untried.
SHARED = (".runtime", "ui/node_modules")


def share_builds(root: Path, path: Path) -> None:
    """Point `path`'s runtime and UI packages at `root`'s, where `root` has them."""
    for rel in SHARED:
        built, link = root / rel, path / rel
        if built.is_dir() and link.parent.is_dir() and not os.path.lexists(link):
            _winapi.CreateJunction(str(built), str(link))


def _unshare_builds(path: Path) -> None:
    """The junctions alone, before anything removes the tree: a removal that
    followed one would empty this checkout's runtime or packages."""
    for rel in SHARED:
        link = path / rel
        if os.path.isjunction(link):
            os.rmdir(link)


def carry_changes(root: Path, path: Path, runner: Runner = subprocess.run) -> None:
    """Make `path` hold what `root` holds now: the diff of its tracked files
    against HEAD, staged or not, and its untracked files. A worktree of HEAD
    alone would test the last commit, not the work in front of you."""
    diff = runner(["git", "-C", str(root), "diff", "HEAD", "--binary"],
                  check=True, capture_output=True).stdout
    if diff.strip():
        runner(["git", "-C", str(path), "apply", "--binary", "--whitespace=nowarn", "-"],
               input=diff, check=True, capture_output=True)
    untracked = runner(["git", "-C", str(root), "ls-files", "-o", "--exclude-standard", "-z"],
                       check=True, capture_output=True).stdout
    copy_files(root, path, (os.fsdecode(rel) for rel in untracked.split(b"\0") if rel))


def copy_files(root: Path, path: Path, rels: Iterable[str]) -> None:
    """Each file `root` has at these paths, copied to the same place in `path`."""
    for rel in rels:
        source = root / rel
        if source.is_file():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def remove(root: Path, path: Path, runner: Runner = subprocess.run) -> None:
    _unshare_builds(path)
    runner(["git", "-C", str(root), "worktree", "remove", "--force", str(path)],
           capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


def prune(root: Path, runner: Runner = subprocess.run) -> None:
    """Forget registered worktrees whose folders are gone (a run that was killed)."""
    runner(["git", "-C", str(root), "worktree", "prune"], capture_output=True)


# pytest, seeing only the code of the tree it runs in. The venv's editable
# install appends a finder to `sys.meta_path` that maps agent/, core/ and the
# other packages to this checkout, consulted after the normal lookup. So a
# module the scratch tree lacks -- the one a change under proof added -- was
# quietly imported from here, and `prove` ran the old code with the new module
# in it. The finder is dropped before anything of the project is imported.
_TREE_ONLY = (
    "import sys\n"
    "def ours(f): return not getattr(f, '__module__', '').startswith('__editable__')\n"
    "sys.meta_path[:] = [f for f in sys.meta_path if ours(f)]\n"
    "sys.path_hooks[:] = [h for h in sys.path_hooks if ours(h)]\n"
    "sys.path_importer_cache.clear()\n"
    "import pytest\n"
    "sys.exit(pytest.main(sys.argv[1:]))\n"
)


def pytest_argv(python: str, *args: str) -> list[str]:
    """`python -m pytest *args`, importing nothing from outside the tree it
    is run in (see `_TREE_ONLY`)."""
    return [python, "-c", _TREE_ONLY, *args]


def pytest_env() -> dict[str, str]:
    """The environment a test run in a scratch tree needs. A worktree has no
    data/, and the embedding model lives there: read from this checkout's
    model folder, which nothing here writes to."""
    env = {**os.environ, "PYDANTIC_AI_NO_BANNER": "1"}
    models = paths.models_dir()
    if "STEVE_MODEL_CACHE" not in env and _readable_dir(models):
        env["STEVE_MODEL_CACHE"] = str(models)
    return env


def _readable_dir(path: Path) -> bool:
    """Is `path` a folder this account can look at? As the coding sandbox's
    account it cannot look inside the user's profile at all: `is_dir()` raised
    PermissionError, and the twenty tests of the three devtools modules that
    build this environment failed in the first run of the suite there after
    they were added (8 October 2026) -- a baseline that fails, so every
    attempt that night would have been refused as the harness's own."""
    try:
        return path.is_dir()
    except OSError:
        return False
