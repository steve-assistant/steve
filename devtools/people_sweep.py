"""Which names in the tree might be real people? A list to mark by hand.

The tests were written against one person's life, and the people in them
were the people in it: a classmate, a recruiter, a landlord, each by name.
A real person's name in a public repository is theirs, not ours to publish,
and no word list catches it, because nobody knows the names in advance. So
this lists everything shaped like a name and a person decides which are
real. Each real one gets a stand-in from the fictional cast, in a map kept
under `private/` (git-ignored), because the map itself holds the names.

What counts as name-shaped, kept simple on purpose:

- two capitalised words together ("Ingrid Lowe"), where the second is not
  one the tree also uses in lower case ("Google Calendar" is not a name:
  "calendar" is an ordinary word here). The first may be: "Mark Holloway"
  is a name although "mark" is a word;
- one such word after a verb that takes a person, or a greeting ("Send
  Ingrid", "Hi Ingrid"), or before a verb or a possessive ("Ingrid said",
  "Ingrid's"): a first name alone is the one a pair never catches.

A capitalised word may have a capital inside it or an apostrophe
("McAllister", "O'Hara"): read as plain `[A-Z][a-z]+`, the first was never
seen and the second was seen cut short.

Only lower-case words between two others in running prose count as
ordinary, because code is full of names: a test's `ingrid = ...` must not
make Ingrid a word. Addresses and links are cut out first, so a name
written beside its address ("cc ingrid ingrid.lowe@...") is not prose
either. The list is long and mostly harmless; it is read once, not trusted.

    python -m devtools.people_sweep [PATH ...] [--min 1]
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What is never read: private/ is where the map lives, and the plans,
# which name the real people on purpose.
SKIP = ("private/", "uv.lock", "ui/package-lock.json", "core/policy.lock")
TEXT = (".py", ".md", ".json", ".yaml", ".yml", ".txt", ".toml", ".ts", ".tsx",
        ".js", ".html", ".css", ".sql", ".ics", ".eml", ".csv", ".jsonl")

_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|https?://\S+|\b[\w-]+(?:\.[\w-]+)+\b")
# A lower-case word between two others: prose, not code. A test's variable
# `ingrid = ...` would otherwise make its own person's name an ordinary word.
_PROSE_WORD = re.compile(r"(?<=[a-z,] )([a-z]+)(?= [a-z])")
# A capitalised word, with a short prefix before an inner capital
# ("McAllister", "MacLeod", "DeVries", "O'Hara"), and maybe hyphenated.
_PART = r"(?:[A-Z][a-z]{1,2}|[A-Z]['’])?[A-Z][a-z]+"
_CAP = rf"{_PART}(?:-{_PART})?"
# Overlapping, so "Ask Mark Holloway" is read as both of its pairs.
_PAIR = re.compile(rf"\b({_CAP}) (?=({_CAP})\b)")
_AFTER = re.compile(
    rf"\b(?:[Ss]end|[Ee]mail|[Aa]sk|[Tt]ell|[Cc]all|[Tt]ext|[Mm]eet|[Ii]nvite|[Rr]eply to|"
    rf"[Ff]orward to|[Tt]hank|[Rr]emind|[Cc]c|[Ww]ith|[Ff]rom|[Hh]i|[Hh]ello|[Hh]ey|[Dd]ear|"
    rf"[Tt]hanks,|[Bb]est,|[Cc]heers,)\s+({_CAP})\b")
_BEFORE = re.compile(
    rf"\b({_CAP})(?:['’]s\b| (?:said|says|asked|asks|wrote|writes|replied|told|wants|"
    rf"thinks|is|was|has|had|will|would|can|sent|called|emailed|texted)\b)")


def tracked(paths: list[str]) -> list[Path]:
    """The tracked text files under `paths` (the whole tree by default)."""
    out = subprocess.run(["git", "ls-files", "-z", *paths], cwd=ROOT, capture_output=True,
                         check=True).stdout.decode("utf-8", "replace")
    return [ROOT / p for p in out.split("\0")
            if p and p.endswith(TEXT) and not p.startswith(SKIP)]


def ordinary(texts: dict[Path, str], at_least: int = 2) -> set[str]:
    """Words the tree uses in lower case in its prose, outside addresses and
    links."""
    seen: Counter = Counter()
    for text in texts.values():
        seen.update(_PROSE_WORD.findall(_ADDRESS.sub(" ", text)))
    return {w for w, n in seen.items() if n >= at_least}


def candidates(texts: dict[Path, str], common: set[str]) -> dict[str, set[str]]:
    """Each name-shaped string, with the files it is in."""
    found: dict[str, set[str]] = defaultdict(set)

    def rare(word: str) -> bool:
        return all(part.lower() not in common for part in word.split("-"))

    for path, text in texts.items():
        rel = path.relative_to(ROOT).as_posix()
        for first, last in _PAIR.findall(text):
            # The surname decides: many first names are words too.
            if rare(last):
                found[f"{first} {last}"].add(rel)
        for name in _AFTER.findall(text) + _BEFORE.findall(text):
            if rare(name):
                found[name].add(rel)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="limit to these paths (default: all)")
    parser.add_argument("--min", type=int, default=1, help="list names in at least N files")
    args = parser.parse_args(argv)

    texts = {}
    for path in tracked(args.paths):
        try:
            texts[path] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    every = texts if not args.paths else {p: p.read_text(encoding="utf-8", errors="replace")
                                          for p in tracked([])}
    found = candidates(texts, ordinary(every))
    for name, files in sorted(found.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(files) >= args.min:
            shown = ", ".join(sorted(files)[:3]) + (" ..." if len(files) > 3 else "")
            print(f"{name}\t{len(files)}\t{shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
