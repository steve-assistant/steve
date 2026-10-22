"""Does a new test fail without the change? Run it against the code from before.

CLAUDE.md: "a test that passes the first time proves nothing. For a guard
test, check it fails against the unfixed code (stash or copy the fix aside,
run, restore)." The judge's prompt says a change's tests "would fail without
it", which the judge decides by reading. Nothing checked, and upgrade #80
showed it: its new regression "succeeds only on the newly introduced
phrase... the prior implementation already generated and ran a 325th
fallback", a test that passed on the old code, which running it there would
have shown in seconds. The maintainer, 8 October: "mutation check 20 things".

This is the stash-run-restore step done once, without stashing. It finds the
test functions a change added or altered (by their syntax, not their line
numbers: a moved test is not a new one; a changed fixture or helper makes
every test in its file count), makes a scratch worktree of the base, puts the
changed test files in it, and runs them there against the old code and here
against the new. For each:

    FAILS AT BASE    it fails without the change: it proves something.
    ERRORS AT BASE   it cannot even be set up there (it needs the new code):
                     it proves the change is needed, and nothing finer.
    PASSES AT BASE   it passes without the change: it proves nothing new.
                     Fine for a refactor's tests, a finding for a fix's.

A test that errors there may only lack a module the change adds -- a guard
that scans the tree with a new tool, say. Those run once more at the base
with the change's new files beside the old code, which nothing old imports:

    FAILS AT BASE    (given the new files) the old tree breaks it: a guard
                     shown failing on what it guards.
    NEEDS NEW FILES  it passes on the old tree once they are there: it tests
                     them, and still proves the change is needed.

Exit 1 when code changed and not one test fails or errors without it (the
change is unproven), or when a test fails with it (the change is broken).
Hand-made breakage -- "does the test notice if this line is wrong?" -- is
devtools.mutate's.

    python -m devtools.prove [--base REV]
"""

from __future__ import annotations

import argparse
import ast
import copy
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from devtools import affected, junit, utf8_stdout, worktrees

ROOT = Path(__file__).resolve().parents[1]

Runner = worktrees.Runner


def _without_docstring(node: ast.AST) -> ast.AST:
    node = copy.deepcopy(node)
    body = getattr(node, "body", None)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        del body[0]
    return node


def _used(node: ast.AST) -> frozenset[str]:
    """Every name a piece of code could lean on: variables, parameters (which
    is how a test names a fixture) and strings (`usefixtures("store")`)."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.arg):
            names.add(sub.arg)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            names.add(sub.value)
    return frozenset(names)


@dataclass
class Inventory:
    """A test file's contents, as syntax."""

    tests: dict[str, tuple[str, frozenset[str]]]  # test -> (its syntax, names it uses)
    named: dict[str, tuple[str, frozenset[str]]]  # a fixture, helper or constant, the same
    anonymous: tuple[str, ...]  # anything else at module level, as syntax


def inventory(source: str) -> Inventory:
    """The tests in a file, each as its syntax (decorators included, its
    docstring and its position not), and everything else in the file that a
    test may lean on -- fixtures, helpers, constants."""
    found = Inventory({}, {}, ())
    anonymous: list[str] = []

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            piece = _without_docstring(node)
            entry = (ast.dump(piece), _used(piece))
            is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_function and node.name.startswith("test_"):
                found.tests[prefix + node.name] = entry
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, f"{prefix}{node.name}::")
            elif isinstance(node, (ast.Import, ast.ImportFrom)) or (
                    isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
                continue
            elif prefix == "" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                    ast.ClassDef)):
                found.named[node.name] = entry
            elif prefix == "" and isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in (n.id for n in ast.walk(target) if isinstance(n, ast.Name)):
                        found.named[name] = entry
            else:
                anonymous.append(entry[0])

    visit(ast.parse(source).body, "")
    found.anonymous = tuple(sorted(anonymous))
    return found


def in_scope(base_source: str | None, head_source: str) -> set[str]:
    """The tests of `head_source` that are new or changed since `base_source`:
    those whose own syntax changed, and those that use a fixture, helper or
    constant that changed (or uses one that does, and so on)."""
    head = inventory(head_source)
    if base_source is None:
        return set(head.tests)
    try:
        base = inventory(base_source)
    except SyntaxError:
        return set(head.tests)  # a base that does not parse says nothing about what changed
    if head.anonymous != base.anonymous:
        return set(head.tests)  # something that is not a name changed: any test may lean on it
    changed = {n for n, (dump, _) in head.named.items() if base.named.get(n, ("",))[0] != dump}
    changed |= set(base.named) - set(head.named)
    grown = True
    while grown:
        grown = False
        for name, (_, used) in head.named.items():
            if name not in changed and used & changed:
                changed.add(name)
                grown = True
    return {name for name, (dump, used) in head.tests.items()
            if base.tests.get(name, ("",))[0] != dump or used & changed}


def _function_of(node: str) -> tuple[str, str]:
    """`tests/unit/test_x.py::TestK::test_c[a-b]` -> (file, `TestK::test_c`)."""
    path, _, rest = node.partition("::")
    return path, rest.split("[", 1)[0]


@dataclass(frozen=True)
class Proof:
    node: str
    at_base: str  # failed, error, passed or skipped; "+new" given the change's new files
    at_head: str


def _at_base(base: list[junit.Outcome]) -> Callable[[str], str]:
    """How a test came out at the base: an error for a file that did not even
    import there, or a test that did not run there."""
    before = {o.node: o.status for o in base}
    broken_files = {o.node for o in base if "::" not in o.node and o.status == "error"}
    return lambda node: ("error" if _function_of(node)[0] in broken_files
                         else before.get(node, "error"))


def unset(scope: dict[str, set[str]], base: list[junit.Outcome]) -> list[str]:
    """The test files with a new or changed test that could not be set up at
    the base: their file did not import, or the test errored or never ran."""
    broken_files = {o.node for o in base if "::" not in o.node and o.status == "error"}
    ran: dict[tuple[str, str], set[str]] = {}
    for o in base:
        if "::" in o.node:
            ran.setdefault(_function_of(o.node), set()).add(o.status)
    return sorted(path for path, names in scope.items()
                  if path in broken_files
                  or any("error" in ran.get((path, name), {"error"}) for name in names))


def judge(scope: dict[str, set[str]], base: list[junit.Outcome],
          head: list[junit.Outcome], given_new: list[junit.Outcome] = ()) -> list[Proof]:
    """Each new or changed test, as it came out at the base and at the head.
    `given_new`: the second run at the base, with the change's new files."""
    status = _at_base(base)
    again = {o.node: o.status for o in given_new}
    proofs = []
    for outcome in head:
        path, function = _function_of(outcome.node)
        if function not in scope.get(path, set()):
            continue
        at_base = status(outcome.node)
        if at_base == "error" and again.get(outcome.node) in ("failed", "passed"):
            at_base = again[outcome.node] + "+new"
        proofs.append(Proof(outcome.node, at_base, outcome.status))
    return proofs


def _lines(label: str, proofs: list[Proof], note: str = "") -> list[str]:
    """One line per test function, with how many of its parametrised ids."""
    counts: dict[str, int] = {}
    for proof in proofs:
        function = proof.node.split("[", 1)[0]
        counts[function] = counts.get(function, 0) + 1
    return [f"{label:<15} {function}{f' (x{n})' if n > 1 else ''}{note}"
            for function, n in counts.items()]


def verdict(proofs: list[Proof], code_changed: bool) -> tuple[int, list[str]]:
    """Exit code and the lines that say it."""
    fails = [p for p in proofs if p.at_base == "failed"]
    fails_given = [p for p in proofs if p.at_base == "failed+new"]
    errors = [p for p in proofs if p.at_base == "error"]
    needs_new = [p for p in proofs if p.at_base == "passed+new"]
    nothing = [p for p in proofs if p.at_base == "passed"]
    broken = [p for p in proofs if p.at_head in ("failed", "error")]
    lines = [*_lines("FAILS AT BASE", fails),
             *_lines("FAILS AT BASE", fails_given, "  (given the change's new files)"),
             *_lines("ERRORS AT BASE", errors, "  (it cannot be set up without the change)"),
             *_lines("NEEDS NEW FILES", needs_new,
                     "  (it passes on the old tree once they are there)"),
             *_lines("PASSES AT BASE", nothing, "  (it proves nothing new)"),
             *_lines("FAILS AT HEAD", broken)]
    needed = len(fails) + len(fails_given) + len(errors) + len(needs_new)
    lines.append(f"{needed} of {len(proofs)} new or changed tests need the "
                 f"change; {len(nothing)} pass without it")
    if broken:
        return 1, lines
    if code_changed and proofs and not needed:
        lines.append("UNPROVEN: code changed, and every new or changed test passes without it")
        return 1, lines
    if code_changed and not proofs:
        lines.append("note: code changed and no test was added or changed")
    return 0, lines


def _read(root: Path, rev: str, rel: str, runner: Runner) -> str | None:
    done = runner(["git", "-C", str(root), "show", f"{rev}:{rel}"], capture_output=True,
                  text=True, encoding="utf-8")
    return done.stdout if done.returncode == 0 else None


def _run_tests(cwd: Path, files: list[str], xml: Path, python: str, runner: Runner,
               timeout: int) -> list[junit.Outcome]:
    xml.unlink(missing_ok=True)
    try:
        runner(worktrees.pytest_argv(python, *files, "-q", "-p", "no:cacheprovider", "--tb=line",
                                     "--continue-on-collection-errors", f"--junitxml={xml}"),
               cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
               errors="replace", timeout=timeout, env=worktrees.pytest_env())
        return junit.read(xml, cwd)
    except (subprocess.TimeoutExpired, *junit.UNREADABLE):
        return []


def prove(root: Path, base: str, *, python: str = sys.executable, runner: Runner = subprocess.run,
          say: Callable[[str], None] = print, scratch: Path | None = None,
          timeout: int = 900) -> int:
    worktrees.refuse_live(root)
    changed = affected.changed_files(root, base)
    support = [f for f in changed if f.startswith("tests/") and (root / f).is_file()
               and f.endswith(".py")]
    files = [f for f in support if Path(f).name.startswith("test_")]
    code_changed = any(not f.startswith(("tests/", "docs/")) and not f.endswith(".md")
                       for f in changed)
    scope: dict[str, set[str]] = {}
    for rel in files:
        head_source = (root / rel).read_text(encoding="utf-8")
        scope[rel] = in_scope(_read(root, base, rel, runner), head_source)
    scope = {rel: names for rel, names in scope.items() if names}
    say(f"base {base[:10]}: {len(files)} test files changed, "
        f"{sum(len(n) for n in scope.values())} tests new or changed")
    if not scope:
        code, lines = verdict([], code_changed)
        for line in lines:
            say(line)
        return code

    scratch = scratch or Path(tempfile.gettempdir()) / "steve-prove"
    scratch.mkdir(parents=True, exist_ok=True)
    tree = scratch / f"base-{os.getpid()}"
    worktrees.add(root, tree, base, runner)
    given_new: list[junit.Outcome] = []
    try:
        worktrees.copy_files(root, tree, support)
        before = _run_tests(tree, sorted(scope), scratch / f"base-{os.getpid()}.xml",
                            python, runner, timeout)
        new = [f for f in affected.added_files(root, base) if (root / f).is_file()]
        if new and (files_unset := unset(scope, before)):
            worktrees.copy_files(root, tree, new)
            given_new = _run_tests(tree, files_unset, scratch / f"new-{os.getpid()}.xml",
                                   python, runner, timeout)
    finally:
        worktrees.remove(root, tree, runner)
        worktrees.prune(root, runner)
    after = _run_tests(root, sorted(scope), scratch / f"head-{os.getpid()}.xml",
                       python, runner, timeout)
    code, lines = verdict(judge(scope, before, after, given_new), code_changed)
    for line in lines:
        say(line)
    return code


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="python -m devtools.prove",
        description="Run the tests a change added or altered against the code from before it.")
    parser.add_argument("--base", default="", help="the code from before "
                        "(default: where this branch left main)")
    args = parser.parse_args(argv)
    return prove(ROOT, args.base or affected.default_base(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
