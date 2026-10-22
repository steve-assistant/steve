"""The repository's files, as the tests that read the whole tree see them:
listed once per test process, each read and parsed once.

Twenty test files scan the tree for what must never be in it: a name, a
host, a data path, a blocking call. Each listed and parsed it again, its
own way. Parsing every Python file takes three or four seconds, and a
dozen tests paid it in each check: shared, these twenty files took 99 s
of a check's summed time rather than 123 s (21 October 2026). Eight
walked the disk, and so read whatever git ignores under the folders they
named; two asked git for tracked files only, and so passed a new file
that was not yet added.

So the list is git's, tracked and new, and never a walk of the disk. The
walk read, whole, each folder git ignores that its list of exceptions had
not named yet: every session's worktree under `.claude` (3,208 of 3,654
files, 58 s, 8 October 2026), then `spikes`, with a whole Python runtime,
then the installed tree in `build/`, whose 24,579 files stopped the check
past ten minutes (17 October). Git lists none of them, nor a nested
worktree, and a new file is listed before it is added: the suite runs
before a change goes into git, and a new file is where a copied name
arrives.
"""
from __future__ import annotations

import ast
import subprocess
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def listed_in(root: Path) -> tuple[str, ...]:
    """Every file git tracks or would add under `root`, relative to it with
    forward slashes, sorted. A file deleted in the working tree is not
    listed."""
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         cwd=root, capture_output=True, check=True).stdout.decode("utf-8")
    return tuple(sorted(rel for rel in set(out.split("\0")) if rel and (root / rel).is_file()))


@cache
def listed() -> tuple[str, ...]:
    """The repository's files, as `listed_in` gives them."""
    return listed_in(REPO)


def python(*under: str) -> tuple[str, ...]:
    """The listed Python files, all of them or those under the folders
    named (as "agent/", with the slash)."""
    return tuple(rel for rel in listed()
                 if rel.endswith(".py") and (not under or rel.startswith(under)))


@cache
def text(rel: str) -> str | None:
    """A listed file's text, or None for one that is not UTF-8 text."""
    try:
        return (REPO / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


@cache
def parsed(rel: str) -> ast.Module:
    """A Python file's syntax tree. Shared between the tests that read it,
    so none may change it."""
    return ast.parse(text(rel) or "", filename=rel)
