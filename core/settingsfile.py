"""A settings file that says which shape it is in, so a release that renames
or reshapes a setting carries the user's choice over instead of dropping it.

No settings file carried a version (`knobs.json`, `model_switches.json`,
`listening.json`, `passphrase.json`, `linkedin_pacer.json`). A setting
renamed in a later release would have been read as a name nobody knows and
dropped (`knobs.read` leaves out what it can't check), and the user's
choice lost on update without a word.

Now each file is `{"version": N, ...}`, and each has a list of steps, one
per change of shape: step `i` takes a file at version `i` to `i + 1`. A
file is read through the steps from its own version to the last, and
written at the last. A file with no version is version 0, the shape every
file had before this: so each list starts with the step that only stamps
it.

A file from a newer release, after a downgrade, is read as far as this one
understands it, and never written over: its newer settings would be lost
for good, and the user may go back up.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

Step = Callable[[dict], dict]

# One write at a time in a process. Each write reads the file first, and
# Windows refuses a replace while anyone holds it open: eight threads writing
# at once under a loaded machine took turns refusing each other past every
# retry, and a guess at the passphrase could go uncounted. Another process's
# write is rare, and its retries cover it.
_WRITING = threading.Lock()


def stamped(settings: dict) -> dict:
    """Version 0 to 1: the shape is the same, only the version is new."""
    return settings


class Newer(Exception):
    """The file was written by a newer release than this one."""


def version_of(raw: dict) -> int:
    version = raw.get("version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError(f"a settings file's version is {version!r}")
    return version


def load(path: Path, steps: Sequence[Step], *, newer_ok: bool = True) -> dict | None:
    """The file's settings in this release's shape, without the version;
    None when there is no file. ValueError when it isn't a settings file
    (not JSON, not an object, a version that isn't a count), OSError when it
    can't be read: the caller decides what an unreadable file means.
    Without `newer_ok`, Newer when a newer release wrote it: its shape may
    not be this one's, and read as this one's, a count it keeps
    differently reads as none (core/totp.py)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} does not hold an object")
    version = version_of(raw)
    if not newer_ok and version > len(steps):
        raise Newer(f"{path.name} was written by a newer Steve")
    settings = {k: v for k, v in raw.items() if k != "version"}
    for step in steps[version:]:
        settings = step(settings)
    return settings


def save(path: Path, settings: dict, steps: Sequence[Step], *, indent: int | None = None,
         private: bool = False) -> None:
    """Writes `settings` at this release's version, whole or not at all.
    Newer when the file there is from a newer release. `private`: the file
    is the current user's alone, restricted before anything is written into
    it (`core/localapi.owner_only`), whatever the folder grants others."""
    with _WRITING:
        _write(path, settings, steps, indent, private)


def _write(path: Path, settings: dict, steps: Sequence[Step], indent: int | None,
           private: bool) -> None:
    try:
        there = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        there = None
    if isinstance(there, dict):
        try:
            newer = version_of(there) > len(steps)
        except ValueError:
            newer = False
        if newer:
            raise Newer(f"{path.name} was written by a newer Steve; it is left as it is")
    body = {"version": len(steps), **{k: v for k, v in settings.items() if k != "version"}}
    write_whole(path, (json.dumps(body, indent=indent, sort_keys=True) + "\n").encode("utf-8"),
                private=private)


def write_whole(path: Path, data: bytes, *, private: bool = False) -> None:
    """`data` at `path`, whole or not at all: written beside it, on the
    disk, then put in its place. A file rewritten where it is can be read
    half-written, or left so by a crash, and a reader that takes a torn file
    for an empty one writes the emptiness back (core/browser/vault.py's
    saved logins did). `private` as for `save`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # A scratch name per write: the worker and a model run can write one
    # file at once, and so can two threads of one worker. Named per process
    # only, two threads shared one scratch file, and the one whose replace
    # came second found it gone or held, and raised.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    if private:
        from core import localapi

        tmp.touch()
        localapi.owner_only(tmp)  # kept by the replace
    with open(tmp, "wb") as out:
        out.write(data)
        # On the disk before the replace. Left in the cache, a power cut
        # could keep the replace and lose the text, and an empty
        # passphrase.json reads as no passphrase at all.
        out.flush()
        os.fsync(out.fileno())
    # Windows refuses a replace for the moment another holds the file: a
    # second writer's replace, or a reader's open. Each holds it briefly.
    for wait in (0.01, 0.03, 0.1, 0.3, 1.0, None):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if wait is None:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            time.sleep(wait)
