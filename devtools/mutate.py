"""Mutation checks: break the code on purpose, and see whether a test notices.

CLAUDE.md asks it of every guard test -- "a test that passes the first time
proves nothing" -- and for ten days (28 September to 8 October 2026) it was
done with a new script each time: about sixty one-off `mutate*.py` in one
session's scratchpad, each writing the mutated file, running pytest in a
fresh process and putting the file back, each pointed by hand at a checkout
other than the live one, because the live agent spawns its workers from its
working tree and a mutated file there is a mutated agent. The maintainer, 8 October:
"unfreeze, build, mutation check 20 things, freeze, run 5000 tests".

This is that loop once. A spec lists the mutations:

    [{"name": "the version check removed",
      "file": "agent/modtools.py",
      "old": "held.version != version",
      "new": "False",
      "test": "tests/unit/test_modtools.py::test_perform_runs_exactly_what_..."}]

`old` must occur exactly once in the file, or the spec is refused before
anything runs (a replace that matched twice, or nowhere, proved nothing
and reported CAUGHT). Each test is first run unmutated: a test that already
fails would "catch" every mutation. Then each mutation runs in one of a few
slots -- `git worktree add --detach` of HEAD with the current diff and the
untracked files copied in, made once (devtools/worktrees.py) -- so mutations
run side by side and none ever touches the tree you are working in. One line
per mutation: CAUGHT, MISSED or BROKEN, with the first failure line. Exit 1
on any MISSED or BROKEN.

`test` is a pytest target, or several separated by spaces (or a JSON list; quote
an id that has a space in it, and use forward slashes); a mutation is caught if any of them fails. A
fast spec names node ids, not folders: each mutation pays pytest's start-up
(about four seconds) and then only its tests. `::test_name` takes all of a
test's parametrisations, so an id with brackets and spaces is rarely needed.

    python -m devtools.mutate spec.json [--slots 2] [--timeout 600] [--only NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from devtools import utf8_stdout, worktrees

ROOT = Path(__file__).resolve().parents[1]

Runner = worktrees.Runner


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    old: str
    new: str
    test: str  # node ids or paths, space-separated


@dataclass(frozen=True)
class Verdict:
    name: str
    outcome: str  # CAUGHT, MISSED or BROKEN
    why: str


def load(spec: Path) -> list[Mutation]:
    raw = json.loads(spec.read_text(encoding="utf-8"))
    items = raw["mutations"] if isinstance(raw, dict) else raw
    made = []
    for item in items:
        test = item["test"]
        made.append(Mutation(item["name"], item["file"], item["old"], item["new"],
                             shlex.join(test) if isinstance(test, list) else test))
    return made


def problems(root: Path, mutations: list[Mutation]) -> list[str]:
    """What is wrong with a spec, before anything runs."""
    found: list[str] = []
    names = [m.name for m in mutations]
    if len(set(names)) != len(names):
        found.append("two mutations share a name")
    for m in mutations:
        path = root / m.file
        if not path.is_file():
            found.append(f"{m.name}: no file {m.file}")
            continue
        count = path.read_bytes().decode("utf-8").count(m.old)
        if count != 1:
            found.append(f"{m.name}: `old` occurs {count} times in {m.file}, not once")
        if m.old == m.new:
            found.append(f"{m.name}: `new` is the same as `old`")
        if not m.test.strip():
            found.append(f"{m.name}: no test named")
    return found


def first_failure(output: str) -> str:
    """The line a human needs: the first `E ` line, else a FAILED/ERROR
    summary line, else the last line pytest printed."""
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    for prefix in ("E ", "FAILED ", "ERROR "):
        for line in lines:
            if line.lstrip().startswith(prefix):
                return line.strip()[:200]
    return lines[-1][:200] if lines else "(no output)"


def _count(output: str, word: str) -> int:
    """The number pytest's last summary line gives `word` ("3 passed")."""
    found = re.findall(rf"(\d+) {word}\b", output)
    return int(found[-1]) if found else 0


def verdict(name: str, returncode: int, output: str) -> Verdict:
    """pytest's exit code, read: 0 every test passed (the mutation was
    missed), 1 a test failed and 2 collection broke (caught -- the second
    trivially, so the line says why), 3 or more the run itself went wrong
    (a timeout, a spec that named no test). pytest also exits 0 when every
    test skipped, and a skipped test never met the mutation: that run is
    broken, not a miss, and a miss says how many of its tests skipped."""
    if returncode == 0:
        passed, skipped = _count(output, "passed"), _count(output, "skipped")
        if skipped and not passed:
            return Verdict(name, "BROKEN", f"every test skipped ({skipped}): none tried it")
        aside = f" ({skipped} skipped)" if skipped else ""
        return Verdict(name, "MISSED", "every test passed with the code broken" + aside)
    if returncode in (1, 2):
        return Verdict(name, "CAUGHT", first_failure(output))
    return Verdict(name, "BROKEN", f"pytest exit {returncode}: {first_failure(output)}")


class Slot:
    """A worktree of HEAD with the current changes in it, for mutating."""

    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def make(cls, root: Path, path: Path, runner: Runner = subprocess.run) -> Slot:
        worktrees.add(root, path, "HEAD", runner)
        worktrees.carry_changes(root, path, runner)
        return cls(path)

    def remove(self, root: Path, runner: Runner = subprocess.run) -> None:
        worktrees.remove(root, self.path, runner)

    def pytest(self, test: str, python: str, timeout: int, runner: Runner) -> tuple[int, str]:
        try:
            done = runner(worktrees.pytest_argv(python, *shlex.split(test), "-x", "-q",
                                                "-p", "no:cacheprovider"),
                          cwd=str(self.path), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env=worktrees.pytest_env())
        except subprocess.TimeoutExpired:
            return 3, f"timed out after {timeout} s"
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    def mutate_and_run(self, m: Mutation, python: str, timeout: int,
                       runner: Runner = subprocess.run) -> Verdict:
        path = self.path / m.file
        original = path.read_bytes()
        text = original.decode("utf-8")
        if text.count(m.old) != 1:
            return Verdict(m.name, "BROKEN", f"`old` occurs {text.count(m.old)} times in the slot")
        try:
            path.write_bytes(text.replace(m.old, m.new, 1).encode("utf-8"))
            code, output = self.pytest(m.test, python, timeout, runner)
        finally:
            path.write_bytes(original)
        return verdict(m.name, code, output)


def run(root: Path, mutations: list[Mutation], *, slots: int = 2, timeout: int = 600,
        python: str = sys.executable, runner: Runner = subprocess.run,
        make_slot: Callable[[Path, Path], Slot] | None = None,
        say: Callable[[str], None] = print) -> list[Verdict]:
    """Every mutation, in up to `slots` worktrees side by side."""
    worktrees.refuse_live(root)
    if wrong := problems(root, mutations):
        raise SystemExit("refused: the spec is wrong:\n  " + "\n  ".join(wrong))
    make_slot = make_slot or (lambda r, p: Slot.make(r, p, runner))
    base = Path(tempfile.gettempdir()) / "steve-mutate"
    count = max(1, min(slots, len(mutations)))
    free: list[Slot] = []
    lock = threading.Lock()

    def take() -> Slot:
        with lock:
            return free.pop()

    def give(slot: Slot) -> None:
        with lock:
            free.append(slot)

    try:
        for i in range(count):
            free.append(make_slot(root, base / f"{os.getpid()}-{i}"))

        # Each test once as it is: one that already fails would catch
        # every mutation and prove nothing.
        tests = sorted({m.test for m in mutations})

        def baseline(test: str) -> tuple[str, int, str]:
            slot = take()
            try:
                code, output = slot.pytest(test, python, timeout, runner)
            finally:
                give(slot)
            return test, code, output

        with ThreadPoolExecutor(count) as pool:
            unmutated = {t: (c, o) for t, c, o in pool.map(baseline, tests)}

        def one(m: Mutation) -> Verdict:
            code, output = unmutated[m.test]
            if code != 0:
                return Verdict(m.name, "BROKEN",
                               f"fails with nothing mutated: {first_failure(output)}")
            slot = take()
            try:
                return slot.mutate_and_run(m, python, timeout, runner)
            finally:
                give(slot)

        results: list[Verdict] = []
        with ThreadPoolExecutor(count) as pool:
            for result in pool.map(one, mutations):
                say(f"{result.outcome:<7} {result.name} -- {result.why}")
                results.append(result)
        return results
    finally:
        for slot in free:
            slot.remove(root, runner)
        worktrees.prune(root, runner)


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    parser = argparse.ArgumentParser(prog="python -m devtools.mutate",
                                     description="Break code on purpose; see which tests notice.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--slots", type=int, default=2,
                        help="worktrees running side by side "
                             "(default 2: this shares the developer's machine)")
    parser.add_argument("--timeout", type=int, default=600, help="seconds for one pytest run")
    parser.add_argument("--only", default="", help="run the mutations whose name contains this")
    args = parser.parse_args(argv)
    mutations = [m for m in load(args.spec) if args.only in m.name]
    if not mutations:
        raise SystemExit("no mutation to run")
    results = run(ROOT, mutations, slots=args.slots, timeout=args.timeout)
    bad = [r for r in results if r.outcome != "CAUGHT"]
    print(f"{len(results) - len(bad)} caught, {len(bad)} not")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
