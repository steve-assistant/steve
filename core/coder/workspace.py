"""What one role may see and touch in one folder.

The coder, the test builder and the judge work in the same folder with
different permissions, and every file operation any of them makes goes
through here. There is no other door: none of them has a shell.

The folder is a module's draft (06-modules.md, section 8). Beside it, read
only, the roles see what a module is written against, under `_reference/`
(`library`): the SDK with its templates, the module guide and the built-in
modules, and nothing else of Steve's. A draft that could read the rest of
the checkout would be written against code a module never runs beside.

- Nobody reads what is not code: `data/` (their database, memories, tokens),
  `.git`, virtual environments, and the hidden graders in core/coder/bench/.
- Nobody writes a frozen path or the reference. The coder writes anything
  else; the test builder writes only new test files under tests/.
- The test builder does not see the coder's tests: the paths in `hidden`
  read as their state before the coder's change (or as absent), so its
  tests are its own reading of the code, not an echo of the coder's.

Paths are relative to the folder, with forward slashes. An absolute path, a
drive, or a `..` that climbs out is refused before the filesystem is
touched.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from core.coder import snapshot

# Never read, by any role.
UNREADABLE = ("data/", ".git/", ".venv/", "node_modules/", "core/coder/bench/",
              "__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/")
UNREADABLE_NAMES = {".env", "policy.lock.bak"}
MAX_FILE_BYTES = 400_000
# Where the library is seen. Never on disk in the folder: the coder cannot
# write there, so nothing in a draft can stand in for the reference.
REFERENCE = "_reference/"


class Refused(ValueError):
    """A path this role may not use, with the reason."""


@dataclass
class Workspace:
    root: Path
    may_write: Callable[[str], bool]
    # For the test builder: the coder's tests, shown as they were before.
    hidden: dict[str, str | None] = field(default_factory=dict)
    # For the test builder: it adds files, and never rewrites one it did not make.
    new_files_only: bool = False
    # For the coder on a revision round: the test builder's tests, which it
    # may read and must not change -- a failing independent test is fixed in
    # the code, not by editing the test.
    read_only: set[str] = field(default_factory=set)
    created: set[str] = field(default_factory=set)
    # Read only, seen as `_reference/<name>`: a folder or one file each.
    library: dict[str, Path] = field(default_factory=dict)

    # -- paths ----------------------------------------------------------------

    def rel(self, path: str) -> str:
        """The path as it actually is on disk, relative to the root.

        As it is on disk, because Windows does not care how a name is
        spelled and this guard would: `Data/agent.db`, `data./agent.db` and
        `core/policy.yaml:x` all reach files the text `data/` and `core/**`
        do not match (audit of 27 September 2026). A colon (an alternate data
        stream) or a part ending in a dot or space is refused outright; the
        rest is resolved to its real spelling before any rule is applied.
        """
        text = str(path or "").replace("\\", "/").strip()
        if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
            raise Refused(f"{path!r}: give a path relative to the repository root")
        if ":" in text:
            raise Refused(f"{path!r}: no colons in a path")
        parts = []
        for part in PurePosixPath(text).parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise Refused(f"{path!r} climbs out of the repository")
                parts.pop()
                continue
            if part != part.rstrip(". "):
                raise Refused(f"{path!r}: a name may not end in a dot or a space")
            parts.append(part)
        if not parts:
            raise Refused(f"{path!r} is the repository itself")
        root = self.root.resolve()
        full = (root / "/".join(parts)).resolve()
        if root != full and root not in full.parents:
            raise Refused(f"{path!r} resolves outside the repository")
        return full.relative_to(root).as_posix()

    def readable(self, rel: str) -> bool:
        return is_readable(rel)

    def _file(self, rel: str) -> Path:
        if rel.startswith(REFERENCE) or rel == REFERENCE.rstrip("/"):
            return self._reference(rel)
        full = (self.root / rel).resolve()
        root = self.root.resolve()
        if root != full and root not in full.parents:
            raise Refused(f"{rel!r} resolves outside the repository")
        return full

    def _reference(self, rel: str) -> Path:
        name, _, rest = rel.removeprefix(REFERENCE).partition("/")
        if name not in self.library:
            raise FileNotFoundError(rel)
        base = self.library[name].resolve()
        full = (base / rest).resolve() if rest else base
        if base != full and base not in full.parents:
            raise Refused(f"{rel!r} resolves outside the reference")
        return full

    # -- reading --------------------------------------------------------------

    def read(self, path: str) -> str:
        rel = self.rel(path)
        if not self.readable(rel):
            raise Refused(f"{rel} is not code and is not readable here")
        if rel in self.hidden:
            before = self.hidden[rel]
            if before is None:
                raise FileNotFoundError(rel)
            return before
        full = self._file(rel)
        if full.stat().st_size > MAX_FILE_BYTES:
            raise Refused(f"{rel} is larger than {MAX_FILE_BYTES} bytes")
        return full.read_text(encoding="utf-8", errors="replace")

    def exists(self, rel: str) -> bool:
        if rel in self.hidden:
            return self.hidden[rel] is not None
        try:
            return self._file(rel).is_file()
        except FileNotFoundError:
            return False

    def files(self) -> list[str]:
        """Every readable file in the folder, found by walking it: git's
        list, which this was until step 6, would run a program in a folder
        the change's own tests can write."""
        found = set(snapshot.walk(self.root, is_readable))
        for name, base in self.library.items():
            if base.is_file():
                found.add(REFERENCE + name)
            else:
                found.update(f"{REFERENCE}{name}/{rel}" for rel in snapshot.walk(base, is_readable))
        for rel, before in self.hidden.items():
            if before is None:
                found.discard(rel)
            else:
                found.add(rel)
        return sorted(f for f in found if f in self.hidden or f.startswith(REFERENCE)
                      or (self.root / f).is_file())

    # -- writing --------------------------------------------------------------

    def _writable(self, path: str) -> str:
        rel = self.rel(path)
        if (not self.readable(rel) or rel in self.hidden or rel in self.read_only
                or rel.startswith(REFERENCE) or not self.may_write(rel)):
            raise Refused(f"{rel} may not be written by this role")
        if self.new_files_only and rel not in self.created and self._file(rel).exists():
            raise Refused(f"{rel} already exists; this role only adds new files")
        return rel

    def write(self, path: str, content: str) -> str:
        """Always LF: a CRLF file makes the frozen-core lock read as tampered."""
        rel = self._writable(path)
        full = self._file(rel)
        existed = full.exists()
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        if not existed:
            self.created.add(rel)
        return rel

    def delete(self, path: str) -> str:
        rel = self._writable(path)
        self._file(rel).unlink()
        self.created.discard(rel)
        return rel


def is_readable(rel: str) -> bool:
    """Whether any role may read the path; a folder is asked with a
    trailing `/`. Lowercased as well as resolved: a path a walk lists
    (`Workspace.files`) is not resolved, and a spelling that has no file
    yet cannot be."""
    low = rel.lower()
    name = low.rstrip("/").rsplit("/", 1)[-1]
    return not (any((low + "/").startswith(p) or low.startswith(p) for p in UNREADABLE)
                or name in UNREADABLE_NAMES)


# ---------------------------------------------------------------------------
# Who may write what
# ---------------------------------------------------------------------------


def coder_may_write(rel: str) -> bool:
    """Anything not frozen. Checked in the path's own spelling and in lower
    case: the frozen globs are lower case, and a new file's name has not
    been resolved against the disk."""
    from core.policy import load_policy

    policy = load_policy()
    return not (policy.is_frozen(rel) or policy.is_frozen(rel.lower()))


def builder_may_write(rel: str) -> bool:
    """Test modules under tests/, never a conftest: a conftest is code
    that runs for every test, including the coder's, and could make them
    all pass. (With `new_files_only`, only ones it made itself.)"""
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith("tests/") and name.startswith("test_") and name.endswith(".py")
