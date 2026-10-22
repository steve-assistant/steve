"""The folder as it was: what a change is measured from, without git.

Until step 6 the coding pipeline worked in a git clone of Steve's own tree
and measured a change from a commit: `git diff <base>` was the change, and
`git show <base>:<path>` a file before it. The clone was the sandbox
account's to write, so a hook or a filter driver a test left there would
have run as the user on the next git command, and every one went through a
wrapper that sealed them first (8 October 2026 audit). A module draft is a
folder, not a repository, and its base is a snapshot: the bytes of every
file a role may read, taken before the coder starts. Reading a change runs
no program, so nothing a test left in the folder runs when it is read.
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path


def walk(root: Path, keep: Callable[[str], bool]) -> Iterator[str]:
    """Every file under `root` that `keep` allows, as a relative POSIX
    path, without entering a folder `keep` refuses (asked with a trailing
    `/`). A link or a junction is never followed: it can point out of the
    folder, and what a role sees stays in it."""
    stack = [""]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(root / here if here else root))
        except OSError:
            continue
        for entry in entries:
            rel = f"{here}/{entry.name}" if here else entry.name
            if entry.is_symlink() or entry.is_junction():
                continue
            if entry.is_dir(follow_symlinks=False):
                if keep(rel + "/"):
                    stack.append(rel)
            elif entry.is_file(follow_symlinks=False) and keep(rel):
                yield rel


@dataclass(frozen=True)
class Snapshot:
    root: Path
    files: dict[str, bytes]
    keep: Callable[[str], bool]

    def had(self, rel: str) -> str | None:
        """The file's text when the snapshot was taken, or None if there
        was no such file."""
        data = self.files.get(rel)
        return None if data is None else _text(data)

    def now(self) -> Snapshot:
        return take(self.root, self.keep)

    def changed(self, later: Snapshot | None = None) -> list[str]:
        """The files added, changed or removed since, sorted."""
        later = later or self.now()
        names = self.files.keys() | later.files.keys()
        return sorted(n for n in names if self.files.get(n) != later.files.get(n))

    def diff(self, *, leave_out: set[str] | frozenset[str] = frozenset(),
             later: Snapshot | None = None) -> str:
        """A unified diff from the snapshot to the folder now, new and
        removed files included, leaving out the paths in `leave_out`."""
        later = later or self.now()
        out: list[str] = []
        for rel in self.changed(later):
            if rel in leave_out:
                continue
            before, after = self.files.get(rel), later.files.get(rel)
            out.extend(difflib.unified_diff(
                [] if before is None else _text(before).splitlines(),
                [] if after is None else _text(after).splitlines(),
                "/dev/null" if before is None else f"a/{rel}",
                "/dev/null" if after is None else f"b/{rel}",
                lineterm=""))
        return "\n".join(out)


def take(root: Path, keep: Callable[[str], bool]) -> Snapshot:
    """The folder as it is: every file `keep` allows (`walk`)."""
    files: dict[str, bytes] = {}
    for rel in walk(root, keep):
        try:
            files[rel] = (root / rel).read_bytes()
        except OSError:
            continue  # gone, or held open, between the listing and the read
    return Snapshot(root, files, keep)


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
