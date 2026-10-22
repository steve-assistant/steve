"""A module's whole life leaves Steve's own folder as it was (06-modules.md,
exit criteria: "The install folder is never written").

The install folder holds Steve's code: an update replaces it whole, and in
a source tree it is the repository. A module lives in the data folder
alone -- its staged copy, its releases, its state, its container's files --
so removing Steve's data removes every trace of it, and an update keeps
it. Self-upgrade v1 changed Steve's own code; v2 builds modules and never
edits it. A lifecycle step that put a copy, a cache or a log beside the
code would undo that, and nothing else would say so: the module would
still run.

So one module is followed with nothing faked but the models: built
through the pipeline's own checks (its tests in a container, and lint),
admitted with its tests run in the sandbox, approved, switched on, called,
upgraded, rolled back, exported and imported over itself, switched off and
removed -- under an audit hook that records every write, by this process,
under the install folder. The module's own code runs in its container,
which cannot write there (`test_module_sandbox.py`). Docker is refused to
every test and every process a test starts (tests/gate/childfence), so
this life also runs with Docker absent (06-modules.md, "No Docker").
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent import modadmit, modshare
from core import audit, db, lifecycle, modctx, modules, paths, screen
from core.coder import testruns
from core.sandbox import probe, run, runtime, testrun

pytestmark = [pytest.mark.processes, pytest.mark.appcontainer]

MANIFEST = """manifest_version = 1
sdk = 1
id = "tally"
version = "{version}"
label = "Tally"
for = "Adding numbers up"
code = "tally.py"

[[tools]]
name = "add"
class = "read"
description = "The sum of two whole numbers."
params = [
  {{ name = "a", type = "integer", description = "One number" }},
  {{ name = "b", type = "integer", description = "The other" }},
]
"""
# The first version answers the sum; the second says what it is.
CODE = {"1": "def add(ctx, a, b):\n    return a + b\n",
        "2": "def add(ctx, a, b):\n    return {\"sum\": a + b}\n"}
TEST = {"1": "from tally import tally\n\n\ndef test_two_and_three_make_five():\n"
             "    assert tally.add(None, 2, 3) == 5\n",
        "2": "from tally import tally\n\n\ndef test_two_and_three_make_five():\n"
             "    assert tally.add(None, 2, 3) == {\"sum\": 5}\n"}


async def _reviewed(source, module_id, said):
    return audit.Verdict(True, "it adds two numbers", model="fake")


# The models' part of admission: the injection screen and the review.
# The trial is the real one, in the sandbox.
MODELS = {"screener": lambda conn, artifact: screen.Verdict(verdict="as_is"),
          "reviewer": _reviewed}

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
# What each event that changes a file or folder gives, by position: the
# paths it changes.
_CHANGES = {"os.mkdir": (0,), "os.rmdir": (0,), "os.remove": (0,), "os.truncate": (0,),
            "os.chmod": (0,), "os.utime": (0,), "os.rename": (0, 1), "os.replace": (0, 1),
            "os.symlink": (1,), "os.link": (1,), "shutil.copyfile": (1,),
            "shutil.copytree": (1,), "shutil.copymode": (1,), "shutil.copystat": (1,),
            "shutil.rmtree": (0,), "shutil.move": (0, 1)}


class Watch:
    """Writes by this process, while it watches: under the install folder,
    and anywhere (which shows the hook sees them)."""

    def __init__(self, install: Path, data: Path) -> None:
        self.install, self.data = install, data
        self.inside: list[str] = []
        self.anywhere = 0

    def saw(self, event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = args
            if not (flags & _WRITE_FLAGS or any(c in (mode or "") for c in "wax+")):
                return
            changed = [path]
        elif event in _CHANGES:
            changed = [args[i] for i in _CHANGES[event]]
        else:
            return
        for path in changed:
            if isinstance(path, (str, bytes, os.PathLike)):
                self.anywhere += 1
                self._check(Path(os.path.abspath(os.fsdecode(path))), event)

    def _check(self, path: Path, event: str) -> None:
        if (path.is_relative_to(self.install) and not path.is_relative_to(self.data)
                and "__pycache__" not in path.parts):
            self.inside.append(f"{event}: {path}")


_watching: list[Watch] = []


def _hook(event: str, args: tuple) -> None:
    if _watching:
        _watching[0].saw(event, args)


def _watch() -> Watch:
    """An audit hook cannot be removed, so one is added per process, the
    first time, and records only while a watch is set."""
    if not getattr(sys, "_steve_life_watched", False):
        sys._steve_life_watched = True
        sys.addaudithook(_hook)
    # In a source tree the data folder may sit inside the checkout; it is
    # the module's, and not the install's.
    found = Watch(paths.SOURCE.resolve(), paths.data_dir().resolve())
    _watching.append(found)
    return found


def _draft(folder: Path, version: str) -> Path:
    files = {"manifest.toml": MANIFEST.format(version=version), "tally.py": CODE[version],
             "tests/test_tally.py": TEST[version]}
    for rel, text in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8", newline="\n")
    return folder


def _built(conn, folder: Path, *, version_of: str = "") -> int:
    """The pipeline's checks on the draft, then admission, as a build ends
    (a new version of `version_of`, where it names one): the admission id."""
    checks = testrun.for_drafts()
    for argv in (testruns.suite_argv("python"),
                 ["python", "-m", "ruff", "check", ".", "--output-format", "concise"]):
        ran = checks(argv, folder)
        assert ran.returncode == 0, ran.output
    return asyncio.run(modadmit.admit(conn, folder, origin="built", version_of=version_of,
                                      **MODELS))


def _add(conn, a: int, b: int):
    held = modules.module(conn, "tally")
    return run.call(conn, held.manifest, "tool", {"name": "add", "args": {"a": a, "b": b}},
                    redact=modctx.scrub)


@pytest.fixture
def conn(monkeypatch):
    # The verdict the worker's start gives on this computer: its check is
    # tests/gate/test_module_sandbox.py's.
    monkeypatch.setattr(probe, "_verdict", probe.Support(True, probe.LPAC, ""))
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    conn = db.open_store(paths.db_path())
    modules.register(conn)
    yield conn
    _watching.clear()
    conn.close()
    lifecycle.drop_container("tally")


def test_a_modules_whole_life_writes_nothing_in_the_install_folder(conn, tmp_path):
    """Built, approved, run, upgraded, rolled back, shared, switched off and
    removed: every write lands in the data folder, and none beside Steve's
    code. The runtime is readied first, as the worker's start does: in a
    checkout it is the install's own, and a stale SDK is copied there."""
    runtime.ready()
    watch = _watch()

    lifecycle.approve(conn, _built(conn, _draft(tmp_path / "v1", "1")), given="")
    modules.set_enabled(conn, "tally", True)
    first = _add(conn, 2, 3)
    lifecycle.approve(conn, _built(conn, _draft(tmp_path / "v2", "2"), version_of="tally"),
                      given="")
    second = _add(conn, 2, 3)
    lifecycle.rollback(conn, "tally", "1")
    rolled_back = _add(conn, 2, 3)
    name, data = modshare.export_zip(conn, "tally")
    lifecycle.approve(conn, asyncio.run(modshare.import_zip(conn, data, replace=True,
                                                            **MODELS)), given="")
    # Replaced by an import, it is installed afresh: off until switched on.
    modules.set_enabled(conn, "tally", True)
    imported = _add(conn, 2, 3)
    modules.set_enabled(conn, "tally", False)
    lifecycle.remove(conn, "tally")
    _watching.clear()

    assert (first, second, rolled_back, imported) == (5, {"sum": 5}, 5, 5)
    assert name == "tally-1.zip" and modules.module(conn, "tally") is None
    assert watch.anywhere > 0, "the hook saw the life's writes"
    assert not watch.inside, "\n".join(sorted(set(watch.inside)))
