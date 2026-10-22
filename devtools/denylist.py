"""The words the product must never contain, kept so the check does not publish them.

The guard (`tests/unit/test_no_personal_data.py`) fails on any tracked file
that names the maintainer whose own assistant the product was copied from,
the people in their mail, or their school, town or courses. A list of those
words in the repo would publish exactly what it guards. So the repo holds only their hashes
(`tests/denylist.txt`), and the plain list stays in the git-ignored
`private/denylist.txt`. A dictionary attack recovers a common word; it does
not recover a classmate's surname.

A term is one to four words, compared without case and with anything that
is not a letter or a digit as a word break: "Port Ellery", "port-ellery"
and "PORT_ELLERY" are one term. A digest is keyed to this project, so a table
made for some other list does not read this one.

    python -m devtools.denylist --add "a name" another   # adds, rehashes
    python -m devtools.denylist --build                  # rehashes the plain list
    python -m devtools.denylist --scan [paths...]        # what it finds, and where
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLAIN = REPO / "private" / "denylist.txt"
HASHED = REPO / "tests" / "denylist.txt"

# The longest term, in words.
LONGEST = 4

_WORD = re.compile(r"[a-z0-9]+")
_KEY = b"steve-denylist"


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def digest(term: str) -> str:
    """A term's hash: its words, lower case, one space between."""
    return _hash(" ".join(words(term)))


@lru_cache(maxsize=1 << 20)
def _hash(joined: str) -> str:
    return hashlib.blake2b(joined.encode(), digest_size=16, person=_KEY).hexdigest()


def load(path: Path = HASHED) -> frozenset[str]:
    """The hashes in the repo's denylist. Lines starting with # are notes."""
    if not path.exists():
        return frozenset()
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(s for line in lines if (s := line.strip()) and not s.startswith("#"))


def find(text: str, hashed: frozenset[str]) -> list[tuple[int, str]]:
    """Every denied term in `text`: its line and the words as they appear.

    A term split across two lines is still found, on the line where it
    starts."""
    if not hashed:
        return []
    found = []
    spots = list(_WORD.finditer(text.lower()))
    said = [m.group(0) for m in spots]
    for i in range(len(said)):
        for n in range(1, min(LONGEST, len(said) - i) + 1):
            term = " ".join(said[i:i + n])
            if _hash(term) in hashed:
                found.append((text.count("\n", 0, spots[i].start()) + 1, term))
    return found


def _plain() -> list[str]:
    if not PLAIN.exists():
        sys.exit(f"no plain list at {PLAIN}: it is git-ignored and lives only "
                 "on the maintainer's machine")
    lines = PLAIN.read_text(encoding="utf-8").splitlines()
    return [s for line in lines if (s := line.strip()) and not s.startswith("#")]


def build() -> int:
    """Rehash the plain list into the repo's file. Returns how many terms."""
    terms = _plain()
    for term in terms:
        if not 1 <= len(words(term)) <= LONGEST:
            sys.exit(f"a term is one to {LONGEST} words: {len(words(term))} in one of them")
    hashes = sorted({digest(t) for t in terms})
    HASHED.parent.mkdir(parents=True, exist_ok=True)
    HASHED.write_text(
        "# blake2b hashes of the words the product must never contain.\n"
        "# Made by `python -m devtools.denylist --build`; the plain list is private.\n"
        + "".join(f"{h}\n" for h in hashes),
        encoding="utf-8", newline="\n")
    return len(hashes)


def add(terms: list[str]) -> int:
    have = _plain()
    known = {digest(t) for t in have}
    new = [t.strip() for t in terms if t.strip() and digest(t) not in known]
    if new:
        with PLAIN.open("a", encoding="utf-8", newline="\n") as f:
            f.write("".join(f"{t}\n" for t in new))
    return build()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m devtools.denylist")
    parser.add_argument("--add", nargs="+", metavar="TERM")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--scan", nargs="*", metavar="PATH")
    args = parser.parse_args(argv)
    if args.add:
        print(f"{add(args.add)} terms in {HASHED.relative_to(REPO).as_posix()}")
    elif args.build:
        print(f"{build()} terms in {HASHED.relative_to(REPO).as_posix()}")
    elif args.scan is not None:
        hashed = load()
        paths = [Path(p) for p in args.scan] or [REPO]
        hits = 0
        for root in paths:
            for path in ([root] if root.is_file() else sorted(root.rglob("*"))):
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for line, term in find(text, hashed):
                    hits += 1
                    print(f"{path}:{line}: {term}")
        return 1 if hits else 0
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
