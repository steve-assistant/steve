"""The loop before the last run: cheap checks first, the suite once, and what
failed said in a line each.

Between 28 September and 8 October 2026 the full suite was run 135 times,
11.3 hours of waiting at a median 247 s, and 34 of the runs (25%) failed.
Most of those a twenty-second look would have caught: `ruff` (1 and 7
October, twice each), a frozen file edited and the lock not rewritten (4
October twice, 6 October: three known tests failing after four minutes), a
new tool not classified (five runs). Four more ended in the same 17 errors
from one slow supervisor start under load, and the output was piped through
`tail`, which kept the summary and dropped the `E` lines, so each had to be
run again to learn what it said. The maintainer: "fail 2 of them for fake reasons and
1 for real".

    python -m devtools.check              lint, the frozen lock, the tests the change names
    python -m devtools.check --processes  and the ones of them that start processes:
                                          before a commit
    python -m devtools.check --full       and then the whole suite, once: at the end of
                                          a series of commits, and before a push

1. `ruff check .`
2. The frozen lock against the frozen files. A mismatch stops here, naming the
   files: "run `python -m core.policy --write-lock` once the change is final"
   -- not a four-minute run to fail the same three tests (`--skip-lock` while a
   frozen file is still being edited).
3. CI's secrets step (`SECRETS_HOOK`) on the files the change touches. A
   value known to be no secret is recorded with its line, and an edit above
   it moves it: the hook rewrites `.secrets.baseline`, to commit with the
   change, instead of CI failing on it half an hour later.
4. The tests the change names, with the repository-wide checks
   (devtools/affected.py), not the ones that start real processes unless
   `--processes` asks for them. A commit is checked with them: from
   13 October 2026 the whole suite runs once per series of commits, not
   before each, and a supervisor change would otherwise be committed with
   none of its tests run. A change that names most of the suite
   (`WHOLE_SUITE_AT`) runs all of it instead, on every worker. A test stuck
   past `devtools.stuck.STUCK_S` is stopped and named with its own frames,
   and the run goes on; the run's own limits (`LIMIT_S`) are a backstop.
   Every run names its slowest tests (`SLOW_S`), and the command that says
   where the slowest one's time went.
5. The shell's own tests, `cargo test --lib` in `ui/src-tauri/`, when the
   change touches a file there, and always with `--full`. Until 19 October
   2026 they ran nowhere: the sign-in's idle tests were run by hand, once.
6. `--full`: everything under tests/, nothing deselected -- the final gate --
   with no pytest cache written into the tree.
7. Anything pytest failed is run again, alone, one process. It fails alone: a real
   failure. It passes alone: it was the load, or another test; said so, not
   hidden, and not blamed on the change. Exit 0 clean, 1 a real failure, 2
   only load-sensitive ones.

Failures are printed from pytest's junit file, one test and its first `E`
line each, never the dots.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from devtools import affected, junit, stuck, utf8_stdout, worktrees

ROOT = Path(__file__).resolve().parents[1]
SHOWN = 25  # failures printed in full; the rest are counted
ALONE_AT_MOST = 40  # failures rerun alone; a worse run is not a load problem
# A change that names this share of the test files is checked by the whole
# suite, on every worker. On 14 October 2026 item 8 named 122 of 256 and they
# ran on two workers into the 900 s limit, at 97% with no failure; the whole
# suite on three took 7 min 14 s.
WHOLE_SUITE_AT = 0.4
# Each run's limit, in seconds. Only a run that never ends meets one: a
# stuck test is stopped first, after `devtools.stuck.STUCK_S`. The whole
# suite takes about 7 minutes on three workers; a selection below
# `WHOLE_SUITE_AT`, about 4 on two.
LIMIT_S = {"affected": 600, "whole": 1200, "alone": 600, "rust": 1200}
# The shell, and how its tests are built: with the features the app is built
# with (`npm run build`'s pages embedded), so the test build reuses the app's
# compiled crates rather than compiling tauri and its plugins a second time.
SHELL = "ui/src-tauri/"
RUST_TESTS = ("cargo", "test", "--lib", "--manifest-path", SHELL + "Cargo.toml",
              "--features", "tauri/custom-protocol")
# CI's secrets step, as its check job runs it over every tracked file
# (.github/workflows/ci.yml).
BASELINE = ".secrets.baseline"
SECRETS_HOOK = ("uvx", "--from", "detect-secrets==1.5.0", "detect-secrets-hook",
                "--baseline", BASELINE)
RUST_RESULT = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed", re.M)
RUST_FAILED = re.compile(r"^test (\S+) \.\.\. FAILED", re.M)
# A test's own frame in a stack that `devtools.stuck` printed:
# `File "...\tests\unit\test_x.py", line 42 in test_y`.
ON_A_STACK = re.compile(r'File "[^"]*?[\\/](tests[\\/][^"]+\.py)", line (\d+) in (test_\w+)')
# Any frame of a stack that `devtools.stuck` wrote, and how many of a stuck
# test's own (the project's, in its thread) are printed.
FRAME = re.compile(r'File "([^"]+)", line (\d+) in (\S+)')
FRAMES_SHOWN = 12
# A test this slow is named after every run, the slowest first, with the
# command that says where its time went (devtools/slow.py). On 14 October
# 2026 the zero-downtime tests took 117 s of a 407 s run, and nothing on
# screen said so; a timing audit by hand found it.
SLOW_S = 5.0
SLOWEST_SHOWN = 5
# How xdist deals the tests out on workers, unless `--dist` asks otherwise:
# one by one, as its own `load` does, except that tests marked with one
# `xdist_group` go to one worker together. test_zero_downtime's supervisor
# is a module fixture, and it was started on every worker that ran one of
# the module's tests: two or three supervisors, each about 10 s under load
# (14 October 2026).
DIST = "loadgroup"

Runner = worktrees.Runner


@dataclass
class Stage:
    """What one pytest run found."""

    real: list[junit.Outcome] = field(default_factory=list)  # fail alone as well
    load_only: list[junit.Outcome] = field(default_factory=list)  # pass alone
    unchecked: list[junit.Outcome] = field(default_factory=list)  # too many to rerun
    ran: int = 0
    crashed: str = ""  # pytest left no junit file: the end of its output


def _frozen_problems(root: Path) -> list[str]:
    from core import policy

    return policy.verify_lock(policy.load_policy(root / "core" / "policy.yaml"),
                              root, root / "core" / "policy.lock")


def _named_by_the_change(root: Path, base: str) -> affected.Selection:
    base = base or affected.default_base(root)
    return affected.select(root, affected.changed_files(root, base), base=base)


def _test_files(root: Path) -> int:
    return sum(1 for f in affected.python_files(root)
               if f.startswith("tests/") and Path(f).name.startswith("test_"))


def _said(done: subprocess.CompletedProcess) -> str:
    """Everything a finished process printed, its two streams on separate lines."""
    return "\n".join(part for part in (done.stdout, done.stderr) if part)


class Check:
    def __init__(self, root: Path = ROOT, *, python: str = sys.executable,
                 runner: Runner = subprocess.run, say: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic, workers: int = 3, base: str = "",
                 dist: str = "", scratch: Path | None = None,
                 lock_problems: Callable[[Path], list[str]] = _frozen_problems,
                 select_tests: Callable[[Path, str], affected.Selection] = _named_by_the_change,
                 count_tests: Callable[[Path], int] = _test_files):
        self.root, self.python, self.runner, self.say = root, python, runner, say
        self.clock, self.workers, self.base, self.dist = clock, workers, base, dist
        self.scratch = scratch or Path(tempfile.gettempdir()) / "steve-check"
        self.lock_problems, self.select_tests = lock_problems, select_tests
        self.count_tests = count_tests

    # -- running things --------------------------------------------------

    def _run(self, argv: list[str], timeout: int) -> subprocess.CompletedProcess:
        try:
            return self.runner(argv, cwd=str(self.root), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout,
                               env={**worktrees.pytest_env(), "PYTHONIOENCODING": "utf-8"})
        except subprocess.TimeoutExpired as late:
            tail = "\n".join(part for part in (late.stdout, late.stderr)
                             if isinstance(part, str) and part)
            return subprocess.CompletedProcess(argv, 124, tail, f"timed out after {timeout} s")

    def _pytest(self, targets: list[str], xml: Path, *, workers: int, extra: list[str],
                timeout: int) -> subprocess.CompletedProcess:
        self.scratch.mkdir(parents=True, exist_ok=True)
        xml.unlink(missing_ok=True)
        stuck_dir = self.scratch / f"stuck-{xml.stem}"
        shutil.rmtree(stuck_dir, ignore_errors=True)
        argv = [self.python, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider",
                "--tb=short", f"--junitxml={xml}", "-p", "devtools.stuck",
                "-o", f"stuck_dir={stuck_dir}", *extra]
        if workers:
            argv += ["-n", str(workers)]
            if "--dist" not in extra:
                argv += ["--dist", DIST]
        done = self._run(argv, timeout)
        self._say_stuck(stuck_dir)
        return done

    def _say_stuck(self, folder: Path) -> None:
        """Each stuck test, from the stacks `devtools.stuck` left, with the
        project's frames in the threads that ran one of the tests. The
        folder is kept only when it holds a stuck test's stacks."""
        found = stuck.stacks(folder)
        if not found:
            shutil.rmtree(folder, ignore_errors=True)
        for node, path, dump in found:
            self.say(f"  stuck, and stopped: {junit.plain(node)}")
            for frame in self._own_frames(dump)[:FRAMES_SHOWN]:
                self.say(f"    {frame}")
            self.say(f"    every thread: {path}")

    def _own_frames(self, dump: str) -> list[str]:
        """The project's frames, most recent first, in each thread of
        `dump` that was running a test."""
        frames = []
        for thread in re.split(r"\n\s*\n", dump):
            ours = []
            for file, line, name in FRAME.findall(thread):
                try:
                    rel = Path(file.replace("\\", "/")).relative_to(self.root.as_posix())
                except ValueError:
                    continue
                if rel.parts[:1] != (".venv",):
                    ours.append(f"{rel.as_posix()}:{line} in {name}")
            if any(frame.startswith("tests/") for frame in ours):
                frames += ours
        return frames

    # -- the stages ------------------------------------------------------

    def lint(self) -> bool:
        started = self.clock()
        done = self._run([self.python, "-m", "ruff", "check", "."], timeout=300)
        if done.returncode == 0:
            self.say(f"ruff           ok ({self.clock() - started:.0f} s)")
            return True
        self.say("ruff           FAILED")
        for line in _said(done).splitlines()[:30]:
            self.say(f"  {line}")
        return False

    def lock(self, skip: bool) -> bool:
        if skip:
            self.say("frozen lock   skipped (the full gate will fail on it until it is rewritten)")
            return True
        problems = self.lock_problems(self.root)
        if not problems:
            self.say("frozen lock   ok")
            return True
        self.say("frozen lock   DOES NOT MATCH the frozen files:")
        for problem in problems:
            self.say(f"  - {problem}")
        self.say("  run `python -m core.policy --write-lock` once the change is final, "
                 "then `python -m core.policy` to see \"frozen core intact\"")
        return False

    def secrets(self, changed: list[str]) -> bool:
        """CI's secrets step on the files the change touches. The baseline
        records the line of each value known to be no secret, and an edit
        above one moves it: twice on 22 October 2026 CI's check job rewrote
        the baseline, failed and kept nothing, half an hour a round. A file
        whose lines did not change cannot move a value, so these files are
        enough. The baseline the hook rewrites here stays, LF as every file
        in the repository, to be committed with the change."""
        files = [f for f in changed if f != BASELINE and (self.root / f).is_file()]
        if not files:
            self.say("secrets        ok (no file to scan)")
            return True
        started = self.clock()
        try:
            done = self._run([*SECRETS_HOOK, *files], timeout=300)
        except FileNotFoundError:
            self.say("secrets        skipped: no `uvx` on PATH (it comes with uv); CI will run it")
            return True
        if done.returncode == 0:
            self.say(f"secrets        ok ({self.clock() - started:.0f} s)")
            return True
        baseline = self.root / BASELINE
        with contextlib.suppress(OSError):
            written = baseline.read_bytes()
            if b"\r\n" in written:
                baseline.write_bytes(written.replace(b"\r\n", b"\n"))
        self.say("secrets        FAILED")
        for line in _said(done).splitlines()[:30]:
            self.say(f"  {line}")
        self.say(f"  a moved line: {BASELINE} is rewritten, so commit it with the change; "
                 "a new value: remove it, or mark a test's fake `# pragma: allowlist secret`")
        return False

    def rust(self) -> bool:
        """The shell's tests. Its pages must be built first: the features the
        app is built with embed them, and a missing `ui/dist` is said, not
        skipped, since a skip would read as a pass."""
        if not (self.root / "ui" / "dist").is_dir():
            self.say("rust tests     need the app's pages: run `npm run build` in ui/ first")
            return False
        start = self.clock()
        done = self._run([*RUST_TESTS, "-j", str(self.workers)], LIMIT_S["rust"])
        said = _said(done)
        results = RUST_RESULT.findall(said)
        if done.returncode == 0 and results:
            passed = sum(int(p) for p, _ in results)
            self.say(f"rust tests     ok: {passed} tests ({self.clock() - start:.0f} s)")
            return True
        failed = RUST_FAILED.findall(said)
        self.say("rust tests     " + (f"failed: {', '.join(failed)}" if failed else
                                      f"did not run to the end (exit {done.returncode})"))
        for line in said.splitlines()[-SHOWN:]:
            self.say(f"  {line}")
        return False

    def _alone(self, bad: list[junit.Outcome], label: str) -> Stage:
        """Each failure again, in one process: does it fail by itself?"""
        stage = Stage()
        if len(bad) > ALONE_AT_MOST:
            stage.unchecked = bad
            return stage
        xml = self.scratch / f"alone-{label}-{os.getpid()}.xml"
        self.say(f"  running the {len(bad)} that failed again, alone ...")
        self._pytest([o.node for o in bad], xml, workers=0, extra=[], timeout=LIMIT_S["alone"])
        try:
            again = {o.node: o for o in junit.read(xml, self.root)}
        except junit.UNREADABLE:
            again = {}
        for outcome in bad:
            second = again.get(outcome.node)
            if second is not None and second.status in ("failed", "error"):
                stage.real.append(second)
            elif second is not None and second.status == "passed":
                stage.load_only.append(outcome)
            else:
                stage.real.append(outcome)  # not found again: not shown to pass
        return stage

    def pytest_stage(self, name: str, targets: list[str], *, extra: list[str], workers: int,
                     timeout: int) -> Stage:
        started = self.clock()
        xml = self.scratch / f"{name.replace(' ', '-')}-{os.getpid()}.xml"
        done = self._pytest(targets, xml, workers=workers, extra=extra, timeout=timeout)
        try:
            outcomes = junit.read(xml, self.root)
        except junit.UNREADABLE:
            return self._crashed(name, done, "DID NOT FINISH")
        bad = junit.bad(outcomes)
        seconds = self.clock() - started
        if not bad and done.returncode in (0, 5):  # 5: every test was deselected
            self.say(f"{name:<14} ok: {len(outcomes)} tests ({seconds:.0f} s)")
            self._say_slowest(outcomes)
            return Stage(ran=len(outcomes))
        if not bad:
            return self._crashed(name, done, "exited with no failing test")
        self.say(f"{name:<14} {len(bad)} of {len(outcomes)} tests failed ({seconds:.0f} s)")
        stage = self._alone(bad, name)
        stage.ran = len(outcomes)
        self._report(stage, xml)
        self._say_slowest(outcomes)
        return stage

    def _say_slowest(self, outcomes: list[junit.Outcome]) -> None:
        slow = junit.slowest([o for o in outcomes if o.seconds >= SLOW_S], SLOWEST_SHOWN)
        if not slow:
            return
        over = sum(o.seconds >= SLOW_S for o in outcomes)
        self.say(f"  {over} took {SLOW_S:.0f} s or more; the slowest:")
        for outcome in slow:
            self.say(f"    {outcome.seconds:5.1f} s  {outcome.node}")
        self.say(f"  where its time went: python -m devtools.slow {slow[0].node}")

    def _crashed(self, name: str, done: subprocess.CompletedProcess, what: str) -> Stage:
        """No verdict to read: what pytest printed last is all there is."""
        printed = _said(done)
        tail = printed.strip().splitlines()[-40:]
        self.say(f"{name:<14} {what} (pytest exit {done.returncode}); its last lines:")
        for line in tail:
            self.say(f"  {line}")
        # Three workers' stacks run to hundreds of lines, so the last forty
        # may hold none of the test's own frames: they are picked out.
        for path, line, test in sorted(set(ON_A_STACK.findall(printed))):
            self.say(f"  on a stack: {path.replace(chr(92), '/')}::{test}, line {line}")
        return Stage(crashed="\n".join(tail) or "no output")

    def _report(self, stage: Stage, xml: Path) -> None:
        shown = 0
        for label, group in (("FAILS ALONE", stage.real),
                             ("PASSES ALONE (sensitive to load or to another test)",
                              stage.load_only),
                             ("NOT RERUN (too many to be the load)", stage.unchecked)):
            if group:
                self.say(f"  {label}: {len(group)}")
            for outcome in group:
                if shown < SHOWN:
                    # An error is a test that could not be set up, not one that failed.
                    self.say(f"    {outcome.node}{' (error)' if outcome.status == 'error' else ''}")
                    self.say(f"      {outcome.reason}")
                shown += 1
        if shown > SHOWN:
            self.say(f"    ... and {shown - SHOWN} more, all in {xml}")

    # -- the whole run ---------------------------------------------------

    def _tidy(self) -> None:
        """The junit files of earlier runs, kept two days: long enough to read
        one back, short enough that the developer's temp folder is not where they pile up
        (it held 721,477 empty folders on 6 October)."""
        cutoff = time.time() - 2 * 86400
        for old in self.scratch.glob("*.xml"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        for old in self.scratch.glob("stuck-*"):  # kept only when a test was stuck
            with contextlib.suppress(OSError):
                if old.stat().st_mtime < cutoff:
                    shutil.rmtree(old, ignore_errors=True)

    def run(self, *, full: bool = False, skip_lock: bool = False,
            processes: bool = False) -> int:
        self._tidy()
        if not self.lint():
            return 1
        if not self.lock(skip_lock):
            return 1
        chosen = self.select_tests(self.root, self.base)
        if not self.secrets(chosen.changed):
            return 1
        named = sorted(f for f, why in chosen.tests.items() if why != ["repo-wide check"])
        self.say(f"affected       {len(chosen.tests)} test files: {len(named)} name the change, "
                 f"{len(chosen.tests) - len(named)} repo-wide checks")
        if chosen.unmatched:
            self.say(f"  no test names {', '.join(chosen.unmatched)}")
        exit_code = 0
        whole = bool(chosen.tests) and len(chosen.tests) >= WHOLE_SUITE_AT * self.count_tests(
            self.root)
        if whole:
            self.say(f"  that is most of the suite: all of it instead, on {self.workers} workers")
        # Not the tests that start processes, unless asked: they are slow, and
        # the full run below -- the one that counts -- never leaves one out.
        # With nothing named, pytest given no target runs everything: never ask it.
        # Under --full, most of the suite named goes straight to the full run:
        # an affected run first was most of it again, and every --full on
        # record spent 250 to 440 s running 7,250 tests twice (step 11).
        first = (self.pytest_stage("affected", ["tests"] if whole else sorted(chosen.tests),
                                   extra=[] if processes else ["-m", "not processes"],
                                   workers=self.workers if whole else min(self.workers, 2),
                                   timeout=LIMIT_S["whole" if whole else "affected"])
                 if chosen.tests and not (full and whole) else Stage())
        if first.real or first.unchecked or first.crashed:
            return 1
        if first.load_only:
            exit_code = 2
        shell = full or any(f.startswith(SHELL) for f in chosen.changed)
        if shell and (self.root / SHELL / "Cargo.toml").is_file() and not self.rust():
            return 1
        if full:
            second = self.pytest_stage("full suite", ["tests"],
                                       extra=["--dist", self.dist] if self.dist else [],
                                       workers=self.workers, timeout=LIMIT_S["whole"])
            if second.real or second.unchecked or second.crashed:
                return 1
            if second.load_only:
                exit_code = 2
        self.say("clean" if exit_code == 0 else
                 "no real failure; what passes alone is named above, and is not the change's")
        return exit_code


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="python -m devtools.check",
        description="Lint, the frozen lock, the tests a change names -- and, with --full, "
                    "the whole suite once.")
    parser.add_argument("--processes", action="store_true",
                        help="the named tests that start processes too: before a commit")
    parser.add_argument("--full", action="store_true",
                        help="then the whole suite, nothing deselected: the final gate")
    parser.add_argument("--base", default="", help="what the change is measured against "
                        "(default: where this branch left main)")
    parser.add_argument("--workers", type=int, default=3,
                        help="pytest -n for the full suite (the loop's own run uses at most 2)")
    parser.add_argument("--dist", default="", help="xdist's --dist for the full suite, "
                        "e.g. worksteal: the longest module is 58 s, and the default deals "
                        "tests out in advance (not measured against the default yet)")
    parser.add_argument("--skip-lock", action="store_true",
                        help="while a frozen file is still being edited")
    args = parser.parse_args(argv)
    check = Check(ROOT, workers=args.workers, base=args.base, dist=args.dist)
    return check.run(full=args.full, skip_lock=args.skip_lock, processes=args.processes)


if __name__ == "__main__":
    raise SystemExit(main())
