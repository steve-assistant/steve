"""Which tests a change can have broken: the ones that name it.

Between 28 September and 8 October 2026 the full suite was run 135 times,
11.3 hours of waiting, to learn about one edited file. A prototype over the
last forty commits showed which selection works. Following imports all the
way down selects a median 68% of the suite's time, because `agent.loop`,
`agent.toolset`, `core.secrets`, `core.policy` and `core.db` are reached by
137 to 146 of 176 test files -- so "everything that could be affected" is
everything. Asking only who names the changed file selects a median 12%.

So a test is picked when:

- it is changed itself, or a conftest or helper under tests/ it depends on is
  (a changed conftest.py picks its whole folder: it can change any test in it);
- it imports a changed module, or names one in a dotted string -- the form
  `monkeypatch.setattr("agent.provider.model_for", ...)` takes -- or the
  changed file is a package's `__init__`, which runs whenever any of the
  package is imported. When the change is inside the module's functions and
  classes alone, an importer is picked only if it names one of those it
  touched (`touched`): `core/paths.py` is imported by 74 test files, and one
  new function there picked 1,408 tests, 633 s of them (step 11's cleanup);
- it is called after the changed file (`agent/loop.py` -> `test_loop.py`),
  or one of its strings, not a docstring, holds the changed file's path (a
  test that reads `agent/prompts/system.md` or `core/policy.yaml`, or the
  source of a module). A docstring that mentions a file explains; it reads
  nothing;
- it is one of the repository-wide checks (`REPO_WIDE`), always: they read
  the whole tree -- lint, docs, tool classification, the frozen core, the
  names and hosts no file may carry -- and are where a change that touches no
  test's subject still breaks something. The nineteen ran in 40 s at `-n 2`
  on 13 October (493 tests, with other work on the laptop). In the live
  checkout the first fourteen took 72 s, until one of them stopped walking
  every session's worktree under `.claude`.

  A guard that reads every file and is not on the list runs only in the full
  suite. On 13 October step 4 put OpenRouter's address in the Hosts column
  of docs/security-model.md. test_no_vendor_names allows that address in
  four files, so it failed; but no test names a doc, so the check before the
  commit passed, and the suite at the step's end was the first to run it.
  Six more scans joined the list then: vendor and personal names, hosts, the
  other edition's names, data paths, and the files the policy's review list
  names.

  The guards that read every Python file of Steve's own joined on 15
  October. Step 6's item 14 read the people table outside `live` in
  agent/modshare.py; test_tagged_reads, which reads every such query, was
  not on the list, no test names that file, and the next item's check was
  the first to fail. The twelve added 170 tests and 24 s at `-n 3`.

This is for the loop while working, never the last word: a change can break a
test that does not name it, which is why the full suite still runs once, as
the final gate. `devtools.check` runs this first and the suite after.

    python -m devtools.affected [--base REV] [--explain]
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from devtools import utf8_stdout

# Tests that read the repository as a whole, so a change anywhere can fail
# one of them whatever it is about: a doc, a prompt, a TypeScript file no
# Python test imports. A test that scans every file git lists belongs here.
REPO_WIDE = (
    "tests/unit/test_commitments.py",
    "tests/unit/test_declared_dependencies.py",
    "tests/unit/test_docs_are_true.py",
    "tests/unit/test_keys.py",
    "tests/unit/test_live_guards.py",
    "tests/unit/test_no_personal_data.py",
    "tests/unit/test_no_vendor_names.py",
    "tests/unit/test_offloop.py",
    "tests/unit/test_repo_hygiene.py",
    "tests/unit/test_server_actions.py",
    "tests/unit/test_store.py",
    "tests/unit/test_test_quality.py",
    "tests/gate/test_account_health.py",
    "tests/gate/test_browser_logins.py",
    "tests/gate/test_every_tool_is_classified.py",
    "tests/gate/test_frozen_core.py",
    "tests/gate/test_hosts_named.py",
    "tests/gate/test_identity.py",
    "tests/gate/test_module_imports.py",
    "tests/gate/test_no_provider_names.py",
    "tests/gate/test_no_secrets_tracked.py",
    "tests/gate/test_only_the_app.py",
    "tests/gate/test_paths.py",
    "tests/gate/test_sent_mail_only.py",
    "tests/gate/test_spend.py",
    "tests/gate/test_tagged_reads.py",
    "tests/gate/test_what_review_protected.py",
    "tests/gate/test_who_may_approve.py",
    "tests/gate/test_write_path_only.py",
)

# The screen tests' window is built from the whole of the app's sources
# (`screens` in tests/unit/ui/conftest.py), and the app's own tests run its
# built program, so a change to any of them can fail every screen test.
# Picked only by the names they hold, item 10's check on 18 October ran 8
# of the 41.
SCREENS = "tests/unit/ui/"
SCREENS_FROM = ("ui/src/", "ui/src-tauri/", "ui/index.html", "ui/package.json",
                "ui/vite.config.ts")

_DOTTED = re.compile(
    r"^(?:agent|core|tools|voice|channels|auth|devtools|tests)(?:\.[A-Za-z_]\w*)+$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


@dataclass
class Selection:
    tests: dict[str, list[str]] = field(default_factory=dict)  # file -> why it was picked
    named: set[str] = field(default_factory=set)  # changed files some test is picked for
    unmatched: list[str] = field(default_factory=list)  # changed, and no test names it
    changed: list[str] = field(default_factory=list)  # every file the change touched

    def pick(self, test: str, why: str, because: str = "") -> None:
        reasons = self.tests.setdefault(test, [])
        if why not in reasons:
            reasons.append(why)
        if because:
            self.named.add(because)


def module_of(rel: str) -> str:
    """`agent/x.py` -> `agent.x`; `agent/toolset/__init__.py` -> `agent.toolset`."""
    dotted = rel.removesuffix(".py").replace("/", ".")
    return dotted.removesuffix(".__init__")


def _stem(rel: str) -> str:
    """What a test file would be called after this one: its name, or a package's."""
    path = Path(rel)
    return path.parent.name if path.name == "__init__.py" else path.stem


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          encoding="utf-8", check=True)
    return done.stdout


def python_files(root: Path) -> list[str]:
    """The tree's Python files as they stand: git's index still lists a file
    deleted but not yet staged, and reading it stopped the check."""
    listed = _git(root, "ls-files", "-co", "--exclude-standard", "-z", "*.py")
    return sorted(f for f in listed.split("\0") if f and (root / f).is_file())


def default_base(root: Path) -> str:
    """Where this work began: the last commit it shares with main, or HEAD
    (only what is uncommitted) when there is no main to compare with."""
    try:
        return _git(root, "merge-base", "HEAD", "main").strip() or "HEAD"
    except subprocess.CalledProcessError:
        return "HEAD"


def added_files(root: Path, base: str) -> list[str]:
    """The files `base` did not have: added since, committed or not."""
    diff = _git(root, "diff", "--name-only", "--no-renames", "--diff-filter=A", "-z", base)
    untracked = _git(root, "ls-files", "-o", "--exclude-standard", "-z")
    return sorted({f for f in [*diff.split("\0"), *untracked.split("\0")] if f})


def changed_files(root: Path, base: str) -> list[str]:
    """Every file that differs from `base` in the working tree, deleted ones
    included, and the files git does not track yet."""
    diff = _git(root, "diff", "--name-only", "--no-renames", "-z", base)
    untracked = _git(root, "ls-files", "-o", "--exclude-standard", "-z")
    return sorted({f for f in [*diff.split("\0"), *untracked.split("\0")] if f})


def _owners(source: str) -> list[tuple[int, int, str]]:
    """Each top-level function and class: its first line (decorators
    included), its last, and its name."""
    spans = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            spans.append((first, node.end_lineno or node.lineno, node.name))
    return spans


def _names_touched(lines: list[str], spans: list[tuple[int, int, str]],
                   first: int, count: int) -> set[str] | None:
    """The functions and classes lines `first`..`first + count - 1` fall in;
    None if one of them is the module's own code. A blank line or a comment
    between them changes nothing."""
    names: set[str] = set()
    for number in range(first, first + count):
        owner = next((name for start, end, name in spans if start <= number <= end), None)
        if owner is not None:
            names.add(owner)
        elif (number <= len(lines) and (line := lines[number - 1].strip())
              and not line.startswith("#")):
            return None
    return names


def touched(root: Path, base: str, rel: str) -> frozenset[str] | None:
    """The top-level functions and classes a change to `rel` touched, or None
    for every importer to count: the module's own lines changed (imports,
    constants, code run at import), or it is new, gone, or does not parse."""
    try:
        old = _git(root, "show", f"{base}:{rel}")
        new = (root / rel).read_text(encoding="utf-8")
        old_spans, new_spans = _owners(old), _owners(new)
        diff = _git(root, "diff", "-U0", "--no-renames", base, "--", rel)
    except (subprocess.CalledProcessError, OSError, SyntaxError, ValueError):
        return None
    names: set[str] = set()
    for hunk in _HUNK.finditer(diff):
        was, was_n, now, now_n = (int(g) if g is not None else 1 for g in hunk.groups())
        for source, spans, first, count in ((old, old_spans, was, was_n),
                                            (new, new_spans, now, now_n)):
            found = _names_touched(source.splitlines(), spans, first, count)
            if found is None:
                return None
            names |= found
    return frozenset(names)


def _literals(tree: ast.AST) -> str:
    """Every string in a file, docstrings left out, one a line."""
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)}
    return "\n".join(node.value for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and id(node) not in docs)


def _resolve(name: str, modules: set[str]) -> str | None:
    """The longest prefix of a dotted name that is a module of the project."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        if (candidate := ".".join(parts[:i])) in modules:
            return candidate
    return None


def references(tree: ast.AST, package: str, modules: set[str]) -> set[str]:
    """The project's modules a file imports or names in a dotted string."""
    found: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                up = package.split(".")
                up = up[: len(up) - (node.level - 1)] if node.level > 1 else up
                base = ".".join([*up, *([base] if base else [])])
            names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and _DOTTED.match(node.value)):
            names = [node.value]
        for name in names:
            if (resolved := _resolve(name, modules)) is not None:
                found.add(resolved)
    return found


def _is_test_file(rel: str) -> bool:
    return rel.startswith("tests/") and Path(rel).name.startswith("test_")


def _matches(ref: str, changed: str, packages: set[str]) -> bool:
    return ref == changed or (changed in packages and ref.startswith(changed + "."))


def _stale_support(support: dict[str, str], changed: list[str],
                   refs: dict[str, set[str]], packages: set[str]) -> set[str]:
    """The conftests and helpers under tests/ that changed, and every other
    one that imports one that did (module name -> file), until nothing grows."""
    stale = {module for module, rel in support.items() if rel in changed}
    grown = True
    while grown:
        grown = False
        for module, rel in support.items():
            if module not in stale and any(
                    _matches(r, s, packages) for r in refs[rel] for s in stale):
                stale.add(module)
                grown = True
    return stale


def _reasons(rel: str, text: str, strings: str, refs: set[str], changed: list[str],
             packages: set[str], stale: dict[str, str],
             names: dict[str, frozenset[str] | None]) -> Iterator[tuple[str, str]]:
    """Why a test file is picked, each with the changed file it is picked for."""
    if rel in changed:
        yield "changed", rel
    name = Path(rel).stem
    for path in (p for p in changed if not p.startswith("tests/")):
        if path.endswith(".py"):
            module = module_of(path)
            within = names.get(path)
            if any(_matches(r, module, packages) for r in refs) and (
                    within is None or any(re.search(rf"\b{re.escape(n)}\b", text)
                                          for n in within)):
                yield f"imports {module}", path
            stem = _stem(path)
            if name == f"test_{stem}" or name.startswith(f"test_{stem}_"):
                yield f"is called after {path}", path
        if path in strings:
            yield f"names {path}", path
        if rel.startswith(SCREENS) and path.startswith(SCREENS_FROM):
            yield "the screens are built from it", path
    for module, helper in sorted(stale.items()):
        if Path(helper).name == "conftest.py":
            if rel.startswith(Path(helper).parent.as_posix() + "/"):
                yield f"{helper} changed", helper
        elif any(_matches(r, module, packages) for r in refs):
            yield f"imports {helper}", helper


def select(root: Path, changed: list[str], *, files: list[str] | None = None,
           repo_wide: tuple[str, ...] = REPO_WIDE, base: str = "") -> Selection:
    """The test files that name what changed, and why each was picked. Given
    the `base` it changed from, an importer of a module whose functions alone
    changed is picked only if it names one (`touched`)."""
    files = files if files is not None else python_files(root)
    modules = {module_of(f) for f in files}
    # A deleted module still names what its importers now fail to import.
    modules |= {module_of(f) for f in changed if f.endswith(".py")}
    packages = {m for m in modules if any(o.startswith(m + ".") for o in modules)}

    texts: dict[str, str] = {}
    strings: dict[str, str] = {}
    refs: dict[str, set[str]] = {}
    for rel in (f for f in files if f.startswith("tests/")):
        texts[rel] = (root / rel).read_text(encoding="utf-8")
        try:
            tree = ast.parse(texts[rel])
        except SyntaxError:
            refs[rel], strings[rel] = set(), texts[rel]
        else:
            refs[rel] = references(tree, module_of(rel).rpartition(".")[0], modules)
            strings[rel] = _literals(tree)
    names = {p: touched(root, base, p) for p in changed
             if base and p.endswith(".py") and not p.startswith("tests/")
             and Path(p).name != "__init__.py"}

    support = {module_of(f): f for f in files if f.startswith("tests/")
               and not _is_test_file(f) and Path(f).name != "__init__.py"}
    stale = _stale_support(support, changed, refs, packages)

    chosen = Selection(changed=list(changed))
    for rel in (f for f in files if _is_test_file(f)):
        for why, because in _reasons(rel, texts[rel], strings[rel], refs[rel], changed,
                                     packages, {m: support[m] for m in stale}, names):
            chosen.pick(rel, why, because)
    present = set(files)
    for rel in repo_wide:
        if rel in present:
            chosen.pick(rel, "repo-wide check")
    # A doc is read by test_docs_are_true, which always runs: not "unnamed".
    chosen.unmatched = [f for f in changed if f not in chosen.named
                        and not f.startswith("tests/") and not f.endswith(".md")]
    return chosen


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="python -m devtools.affected",
        description="The test files a change names, one a line, for pytest to be given.")
    parser.add_argument("--base", default="",
                        help="compare with this revision (default: where this branch left main)")
    parser.add_argument("--explain", action="store_true", help="say why each file was picked")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    base = args.base or default_base(root)
    chosen = select(root, changed_files(root, base), base=base)
    for rel in sorted(chosen.tests):
        print(f"{rel}  # {'; '.join(chosen.tests[rel])}" if args.explain else rel)
    if chosen.unmatched:
        print("# no test names " + ", ".join(chosen.unmatched)
              + ": only the repo-wide checks cover it, so the full suite is the check",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
