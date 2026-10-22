"""Where Steve keeps what it writes: the one place a data path is built.

About forty modules each built `REPO_ROOT / "data" / ...` for themselves.
That tied the data to the code: an installed Steve has no checkout, and
its code folder is replaced at every update. Each path also resolved when
its module was imported, so a test or a process that wanted the data
elsewhere had to find and patch every constant, and one it missed wrote to
the real folder (`core/db.connect` tells how that went for the database).

So every path here is a function, resolved when it is called, under one
folder chosen in this order:

1. `STEVE_DATA_DIR`, if set. Tests set it (tests/gate/fences.py), and so
   can anyone who wants the data elsewhere.
2. `%LOCALAPPDATA%\\Steve` for an installed Steve: one whose build wrote
   `installed.json` beside its packages (`is_installed`).
3. The checkout's own `data/`, when this code runs from a source tree
   (a `pyproject.toml` beside the packages, and no marker). Each
   worktree's processes stay inside that worktree, as they always have.
4. Neither: `LayoutUnknown`, and Steve does not start.

An install was once told from a checkout only by a missing
`pyproject.toml`, so one stray file in the install folder would have made
it write its data beside its code, and read secrets from the environment
(core/secrets.py). The marker is positive, and wins: a file left in the
wrong place cannot turn an install into a checkout. And code copied
somewhere with neither (a spike, a backup) refuses, rather than quietly
using the installed Steve's data.

The code's own files (`db/`, `agent/prompts/`, fixtures) stay at
`SOURCE`; they are shipped with the code, not written by it.

tests/gate/test_paths.py fails if any other module joins "data" onto a
path relative to its code.
"""

from __future__ import annotations

import os
from pathlib import Path

from core import identity

# The folder holding the packages: the checkout in development, the
# installed code otherwise. For shipped files, never for written ones.
SOURCE = Path(__file__).resolve().parent.parent

DATA_DIR_ENV = "STEVE_DATA_DIR"
MODEL_CACHE_ENV = "STEVE_MODEL_CACHE"
MEMORIES_DIR_ENV = "STEVE_MEMORIES_DIR"
ADDONS_DIR_ENV = "STEVE_ADDONS_DIR"


# Written by the build (devtools/package.py) beside an install's packages,
# and never present in a checkout. It holds the version and the lock's hash.
INSTALLED_MARKER = "installed.json"


class LayoutUnknown(RuntimeError):
    """This code is neither an install nor a checkout, so it has no data
    folder of its own."""


def is_installed() -> bool:
    return (SOURCE / INSTALLED_MARKER).is_file()


def is_source_tree() -> bool:
    return not is_installed() and (SOURCE / "pyproject.toml").is_file()


def layout() -> str:
    """"installed" or "source", or `LayoutUnknown`, saying why."""
    if is_installed():
        return "installed"
    if is_source_tree():
        return "source"
    raise LayoutUnknown(
        f"{SOURCE} is neither an installed Steve (no {INSTALLED_MARKER}) nor a checkout "
        "(no pyproject.toml), so it has no data folder of its own. Run the installed "
        f"Steve, or a checkout, or set {DATA_DIR_ENV}.")


def install_dir() -> Path:
    """The install's own folder, `%LOCALAPPDATA%\\Programs\\Steve` unless
    the user chose another: the packages are in its `backend\\app`."""
    if not is_installed():
        raise LayoutUnknown(f"{SOURCE} is not an installed Steve")
    return SOURCE.parent.parent


def _chosen(name: str) -> Path | None:
    """The folder the environment variable `name` sets, if it sets one.

    A relative one is made absolute here, once, and handed down through the
    environment: resolved by each process against its own working folder,
    it put the supervisor's data in one place and its workers' in another."""
    if not (chosen := os.environ.get(name, "").strip()):
        return None
    path = Path(chosen).expanduser()
    if not path.is_absolute():
        path = path.resolve()
        os.environ[name] = str(path)
    return path


def data_dir() -> Path:
    if (chosen := _chosen(DATA_DIR_ENV)) is not None:
        return chosen
    if layout() == "source":
        return SOURCE / "data"
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / identity.NAME


def overlap() -> str | None:
    """Why this install may not run, if its folder and its data folder
    overlap: one is the other, or holds it.

    Uninstalling with the data removes the data folder, and the program
    running the uninstall with it if it is inside; an update replaces the
    program's folders, and any data inside them. The installer's folder page
    refuses such a folder (ui/src-tauri/windows/hooks.nsh), but a silent
    install's `/D=` shows no page, and `STEVE_DATA_DIR` can name any folder.
    Until step 11 the page refused only the data folder itself (the audit's
    F8)."""
    if not is_installed():
        return None
    return folders_overlap(install_dir(), data_dir())


def folders_overlap(install: Path, data: Path) -> str | None:
    """`overlap`'s rule for two folders, resolved, and compared as Windows
    compares them, whatever their case."""
    install, data = install.resolve(), data.resolve()
    if install.is_relative_to(data) or data.is_relative_to(install):
        return (f"Steve is installed in {install} and keeps its data in {data}, and one "
                "holds the other: removing the data would remove the program, or an update "
                "the data. Install Steve into a folder of its own, such as "
                "%LOCALAPPDATA%\\Programs\\Steve.")
    return None


def db_path() -> Path:
    return data_dir() / "agent.db"


def logs_dir() -> Path:
    return data_dir() / "logs"


def memories_dir() -> Path:
    if (chosen := _chosen(MEMORIES_DIR_ENV)) is not None:
        return chosen
    return data_dir() / "memories"


def artifacts_dir() -> Path:
    return data_dir() / "artifacts"


def models_dir() -> Path:
    """Downloaded models: embeddings, Whisper, the local classifiers. Shared
    between worktrees through `STEVE_MODEL_CACHE`, being large and only
    ever read once downloaded."""
    if chosen := os.environ.get(MODEL_CACHE_ENV, "").strip():
        return Path(chosen)
    return data_dir() / "models"


def addons_dir() -> Path:
    """The programs Steve downloads for a feature the user chose: signal-cli
    and the Java it runs on (core/addons.py). Code, not data, so beside the
    code: the install folder's `addons` when installed, the
    checkout's git-ignored `.addons/` in a source tree. Kept out of the data
    folder, which a backup copies and an export hands over."""
    if (chosen := _chosen(ADDONS_DIR_ENV)) is not None:
        return chosen
    if layout() == "source":
        return SOURCE / ".addons"
    return install_dir() / "addons"


def modules_dir() -> Path:
    """Every module's own folder (core/modules.py). The built-ins' code is
    shipped at `SOURCE / "modules"`; only their state is written here."""
    return data_dir() / "modules"


def module_dir(module_id: str) -> Path:
    """One module's folder: its `state/`, and from step 6 its code's versions."""
    from steve_sdk.manifest import ID

    if not ID.fullmatch(module_id or ""):
        raise ValueError(f"not a module id: {module_id!r}")
    return modules_dir() / module_id


def module_state_dir(module_id: str) -> Path:
    """What a module stores for itself: sync cursors, etags, a small SQLite
    file. Created on asking."""
    path = module_dir(module_id) / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def module_runs_file(module_id: str) -> Path:
    """When each of a module's routines last ran and runs next, and what it
    last said (agent/modroutines.py): core's record, so beside `state/`,
    which the module itself writes, and never in it. Removing the module
    takes it."""
    return module_dir(module_id) / "routines.json"


def _version(version: str) -> str:
    from steve_sdk.manifest import VERSION

    if not VERSION.fullmatch(version or ""):
        raise ValueError(f"not a module version: {version!r}")
    return version


def module_code_dir(module_id: str, version: str) -> Path:
    """One version of a module from outside: its files as the user approved
    them (core/lifecycle.py), never written after."""
    return module_dir(module_id) / "code" / _version(version)


def module_copy_dir(module_id: str, version: str) -> Path:
    """The module's `state/` as `version` left it, copied aside before the
    next version started. Rolling back to `version` restores it."""
    return module_dir(module_id) / "copies" / _version(version)


def staged_dir() -> Path:
    """Modules checked and waiting for the user's card, a folder each by
    admission (core/lifecycle.py): apart from the modules, so that a module
    replaced, or removed meanwhile, takes none of them with it."""
    return data_dir() / "module_staged"


def module_drafts_dir() -> Path:
    """Modules Steve is building, a folder each by build
    (agent/modbuild.py): apart from the modules, so that no module's id can
    name a draft, and kept when a build stops so that it can go on."""
    return data_dir() / "module_drafts"


def module_draft_dir(build_id: int) -> Path:
    return module_drafts_dir() / str(int(build_id))


def run_dir() -> Path:
    """The run file naming the supervisor's port and token
    (core/localapi.py)."""
    return data_dir() / "run"


def scratch_dir() -> Path:
    """Files that live for one operation: a voice note on its way out, a
    sandbox's staging folder, an image's build context. Created on asking.

    Not `%TEMP%`, which every program of this Windows user shares: the
    personal edition wrote the same `agent-*` names there, a voice note named
    by `id()` could be overwritten by the other's, and what Steve left behind
    there was outside the folder that removing its data clears."""
    path = data_dir() / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path
