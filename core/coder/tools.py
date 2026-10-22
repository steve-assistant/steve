"""The coder's and test builder's hands: read, search, edit, test. No shell.

The old path gave a model a harness's shell with `--auto`: it could push,
delete files outside its worktree, or read anything the user can. These tools
are the whole of what a role can do. Every file operation goes through the
role's `Workspace`; every execution of the code -- tests, lint -- goes
through the runner it is given, which is the sandbox's.

Each tool returns text the model can act on, including a refusal: "refused:
core/policy.yaml may not be written by this role" is information, and an
exception would end the run.
"""

from __future__ import annotations

import ast
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from core.coder.snapshot import Snapshot
from core.coder.workspace import Refused, Workspace

MAX_READ_LINES = 400
MAX_OUTPUT_CHARS = 8_000
MAX_MATCHES = 60

# A path under tests/ -- no `.` or `..` parts, which would reach out of the
# folder -- optionally with node ids.
_TEST_TARGET = re.compile(
    r"^tests(/(?!\.{1,2}(/|$|::))[A-Za-z0-9_.\-]+)*(::[A-Za-z0-9_.\[\]\-]+)*$")
# Letters, digits, underscores and spaces only, which is all a -k of test
# names joined by "or" needs. Once capped at 80 characters, which refused
# the coder's six-name -k with a message blaming its characters.
_KEYWORD = re.compile(r"^[A-Za-z0-9_ ]{1,400}$")


def _tail(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else "...\n" + text[-limit:]


# -- an edit whose `old` does not match exactly ---------------------------------
#
# 135 of the 591 edit_file calls in the records of upgrades 59-88 (23%) were
# refused with "`old` occurs 0 times", most often in docs/flows.md, and six
# coders ran out of their 60 turns. The likeliest cause: a model copying
# `old` types back an em dash, an arrow or a curly quote as its plain form
# (flows.md has 29 arrows and 29 em dashes, agent/prompts/system.md 68 em
# dashes). So a refused `old` is tried once more with those read as plain,
# runs of spaces inside a line as one and trailing spaces dropped -- never
# indentation, which is Python's syntax -- and applied only when that finds
# exactly one place. Counted apart in the record (`LOOSE`), to show whether
# the guess was right. With no such place, the refusal names the nearest
# line, so the next try starts from what is there.

LOOSE = "(loose match)"
_PLAIN = {
    "—": "-", "–": "-", "‒": "-", "‑": "-", "‐": "-", "−": "-",
    "→": "->", "←": "<-", "⇒": "=>", "↔": "<->",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "…": "...", " ": " ", " ": " ", " ": " ", " ": " ",
}


def _plain(text: str) -> tuple[str, list[int], list[int]]:
    """`text` read loosely (above), and for each character of the result,
    the span of `text` it came from."""
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    line_start = True
    gap: tuple[int, int] | None = None  # spaces inside a line, not yet written
    for i, ch in enumerate(text):
        for c in _PLAIN.get(ch, ch):
            if c == "\r":
                continue
            if c in " \t" and not line_start:
                gap = (gap[0] if gap else i, i + 1)
                continue
            if c == "\n":
                gap, line_start = None, True  # spaces before a line's end are dropped
            else:
                if gap is not None:
                    out.append(" ")
                    starts.append(gap[0])
                    ends.append(gap[1])
                    gap = None
                if c not in " \t":
                    line_start = False
                if c == "-" and out and out[-1] == "-" and ends[-1] == i:
                    ends[-1] = i + 1  # "--" typed for an em dash
                    continue
            out.append(c)
            starts.append(i)
            ends.append(i + 1)
    return "".join(out), starts, ends


def _loose_match(text: str, old: str) -> tuple[int, int] | None:
    """The one span of `text` that `old` matches when both are read loosely,
    or None if there is not exactly one."""
    plain, starts, ends = _plain(text)
    want = _plain(old)[0]
    if not want.strip() or plain.count(want) != 1:
        return None
    at = plain.find(want)
    return starts[at], ends[at + len(want) - 1]


def _all(text: str, old: str) -> list[int]:
    found, at = [], text.find(old)
    while at != -1:
        found.append(at)
        at = text.find(old, at + len(old))
    return found


def _nearest(text: str, old: str) -> str:
    """Where the file has the line closest to the first line of `old`."""
    import difflib

    first = next((line.strip() for line in old.splitlines() if line.strip()), "")
    if not first:
        return ""
    lines = [line.strip() for line in text.splitlines()]
    close = difflib.get_close_matches(first, lines, n=1, cutoff=0.6)
    if not close:
        return "\nno line in the file is close to its first line: read the file again"
    n = lines.index(close[0]) + 1
    return f"\nthe closest line to its first line is line {n}: {close[0][:200]}"


@dataclass
class Call:
    """One tool call, as the record keeps it. The second live run's coder
    spent sixty turns on a task and changed nothing, and the names of its
    calls could not say whether it never tried to edit or every edit was
    refused."""

    name: str
    about: str  # the path or test target it was for
    outcome: str  # ok | refused | error | failed (tests or lint that did not pass)
    seconds: float
    note: str = ""  # the first line of anything but ok


def _outcome(name: str, result: str) -> tuple[str, str]:
    first = result.split("\n", 1)[0][:160]
    if name == "edit_file" and result.startswith("edited") and LOOSE in first:
        return "loose", first  # counted apart, so the record shows how often it saves a turn
    if result.startswith("refused:"):
        return "refused", first
    if result.startswith(("error:", "no such file:")):
        return "error", first
    if name in ("run_tests", "run_lint") and (
            result.startswith("TIMED OUT") or result.startswith("exit ")
            and not result.startswith("exit 0")):
        return "failed", first
    return "ok", ""


def summary(calls: list[Call]) -> str:
    """One line for the record: counts per tool, what went wrong, where the
    time went, and the last few refusals and errors word for word."""
    if not calls:
        return "no tool calls"
    by: dict[str, list[Call]] = {}
    for c in calls:
        by.setdefault(c.name, []).append(c)
    parts = []
    for name, cs in by.items():
        bad: dict[str, int] = {}
        for c in cs:
            if c.outcome != "ok":
                bad[c.outcome] = bad.get(c.outcome, 0) + 1
        extra = [f"{n} {o}" for o, n in bad.items()]
        took = sum(c.seconds for c in cs)
        if took >= 1:
            extra.append(f"{took:.0f}s")
        parts.append(f"{name} {len(cs)}" + (f" ({', '.join(extra)})" if extra else ""))
    n = len(calls)
    line = (f"{n} call{'' if n == 1 else 's'}, {sum(c.seconds for c in calls):.0f}s in tools: "
            + ", ".join(parts))
    wrong = [c for c in calls if c.outcome in ("refused", "error")][-3:]
    if wrong:
        line += "; last refused or failed: " + " | ".join(
            f"{c.name} {c.about}: {c.note}".replace("  ", " ") for c in wrong)
    return line


@dataclass
class Ran:
    """One run of a program: what the runner hands back."""

    returncode: int
    output: str
    seconds: float
    timed_out: bool = False


@dataclass
class Tools:
    ws: Workspace
    # The sandbox's runner, or a fake in tests: (argv, cwd, timeout_s=) -> Ran
    runner: Callable
    python: str
    test_timeout_s: float = 600
    # Where `show_diff` measures from: the folder when the attempt began.
    # Measured from the coder's finished work, the test builder saw nothing
    # (audit of 27 September 2026).
    base: Snapshot | None = None
    # The test builder runs only the tests it wrote. Running tests/unit
    # printed the coder's test names and failing assertions into its context.
    own_tests_only: bool = False
    calls: list[str] = field(default_factory=list)
    log: list[Call] = field(default_factory=list)

    def _guarded(self, name: str, fn, *args, about: str = ""):
        self.calls.append(name)
        started = time.perf_counter()
        try:
            result = fn(*args)
        except Refused as exc:
            result = f"refused: {exc}"
        except FileNotFoundError as exc:
            result = f"no such file: {exc}"
        except (ValueError, SyntaxError, re.error, OSError) as exc:
            # OSError too: a missing or unreadable directory ended the whole
            # role once, where it should have been something to report.
            result = f"error: {exc}"
        outcome, note = _outcome(name, result)
        self.log.append(Call(name, str(about)[:120], outcome,
                             time.perf_counter() - started, note))
        return result

    # -- reading --------------------------------------------------------------

    def list_files(self, pattern: str = "**/*.py") -> str:
        """Files matching a glob, the module's and the reference's, e.g.
        "*.py", "tests/*.py" or "_reference/modules/**/manifest.toml"."""
        def go():
            found = [f for f in self.ws.files() if PurePosixPath(f).full_match(pattern)]
            shown = found[:300]
            more = f"\n... and {len(found) - 300} more" if len(found) > 300 else ""
            return "\n".join(shown) + more if shown else "no files match"
        return self._guarded("list_files", go, about=pattern)

    def read_file(self, path: str, start: int = 1, end: int = 0) -> str:
        """Lines `start` to `end` (1-based, inclusive; 0 means to the end, at
        most 400 lines at a time), numbered."""
        def go():
            lines = self.ws.read(path).splitlines()
            first = max(1, start)
            last = min(len(lines), end if end else first + MAX_READ_LINES - 1,
                       first + MAX_READ_LINES - 1)
            body = "\n".join(f"{n:5d}  {lines[n - 1]}" for n in range(first, last + 1))
            note = f"\n[{len(lines)} lines in all]" if last < len(lines) or first > 1 else ""
            return (body or "(empty)") + note
        return self._guarded("read_file", go, about=path)

    def search(self, pattern: str, glob: str = "**/*.py") -> str:
        """Lines matching a regular expression, as path:line: text."""
        def go():
            rx = re.compile(pattern)
            hits = []
            for rel in self.ws.files():
                if not PurePosixPath(rel).full_match(glob):
                    continue
                try:
                    text = self.ws.read(rel)
                except (Refused, OSError):
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                        if len(hits) >= MAX_MATCHES:
                            return "\n".join(hits) + "\n[more matches not shown]"
            return "\n".join(hits) if hits else "no matches"
        return self._guarded("search", go, about=pattern)

    def outline(self, path: str) -> str:
        """The classes and functions in a Python file, with line numbers and
        the first line of each docstring."""
        def go():
            tree = ast.parse(self.ws.read(path))
            doc = (ast.get_docstring(tree) or "").splitlines()
            rows = [f"module: {doc[0]}"] if doc else []

            def walk(nodes, depth):
                for node in nodes:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        kind = "class" if isinstance(node, ast.ClassDef) else "def"
                        first = (ast.get_docstring(node) or "").split("\n", 1)[0][:100]
                        rows.append(f"{'  ' * depth}L{node.lineno} {kind} {node.name}"
                                    + (f" -- {first}" if first else ""))
                        if isinstance(node, ast.ClassDef):
                            walk(node.body, depth + 1)
            walk(tree.body, 0)
            return "\n".join(rows) or "(nothing defined)"
        return self._guarded("outline", go, about=path)

    # -- writing --------------------------------------------------------------

    def edit_file(self, path: str, old: str, new: str) -> str:
        """Replace one exact, unique piece of text in a file. Include enough
        surrounding lines that `old` occurs exactly once."""
        def go():
            text = self.ws.read(path)
            count = text.count(old) if old else 0
            if count == 1:
                self.ws.write(path, text.replace(old, new, 1))
                return f"edited {self.ws.rel(path)}"
            refused = (f"refused: `old` occurs {count} times in {path}; it must match exactly "
                       "once (copy it from read_file, without line numbers)")
            if count > 1:
                return refused + "\nit begins at lines " + ", ".join(
                    str(text.count("\n", 0, i) + 1) for i in _all(text, old)[:10])
            if old and (span := _loose_match(text, old)) is not None:
                start, end = span
                self.ws.write(path, text[:start] + new + text[end:])
                return (f"edited {self.ws.rel(path)} {LOOSE}: only after reading dashes, "
                        "arrows, quotes and spaces as their plain forms did `old` match, once; "
                        "the text there is now exactly your `new`")
            return refused + _nearest(text, old)
        return self._guarded("edit_file", go, about=path)

    def write_file(self, path: str, content: str) -> str:
        """Create a file, or replace one completely."""
        return self._guarded("write_file", lambda: f"wrote {self.ws.write(path, content)}",
                             about=path)

    def delete_file(self, path: str) -> str:
        return self._guarded("delete_file", lambda: f"deleted {self.ws.delete(path)}",
                             about=path)

    # -- checking -------------------------------------------------------------

    def run_tests(self, target: str = "tests", keyword: str = "") -> str:
        """pytest on a path or node id under tests/ (e.g. "tests/test_tool.py"
        or "tests/test_tool.py::test_x"), optionally filtered by -k words.
        Runs in the sandbox, with the module's files read-only. The whole
        suite is target "tests"."""
        def go():
            if not _TEST_TARGET.match(target or ""):
                return f"refused: {target!r} is not a test path under tests/"
            if self.own_tests_only and target.split("::")[0] not in self.ws.created:
                mine = ", ".join(sorted(self.ws.created)) or "none yet"
                return f"refused: run only the tests you wrote ({mine})"
            if keyword and not _KEYWORD.match(keyword):
                return (f"refused: {keyword!r} is not a plain -k expression (letters, "
                        "digits, underscores and spaces, at most 400 characters)")
            argv = [self.python, "-m", "pytest", target, "-q", "-p", "no:cacheprovider",
                    "--tb=short", "-rf"] + (["-k", keyword] if keyword else [])
            ran = self.runner(argv, self.ws.root, timeout_s=self.test_timeout_s)
            head = "TIMED OUT\n" if ran.timed_out else ""
            return f"{head}exit {ran.returncode}\n{_tail(ran.output)}"
        return self._guarded("run_tests", go, about=target)

    def run_lint(self) -> str:
        """ruff over the module (for the test builder, over the files it
        wrote)."""
        def go():
            # Linting the whole tree for the test builder would print findings
            # in the coder's tests -- hidden from it, but still on disk -- with
            # their names and lines: the leak run_tests is limited against.
            targets = ["."]
            if self.own_tests_only:
                targets = sorted(self.ws.created)
                if not targets:
                    return "nothing to lint: you have written no files yet"
            ran = self.runner([self.python, "-m", "ruff", "check", *targets, "--output-format",
                               "concise"], self.ws.root, timeout_s=120)
            return f"exit {ran.returncode}\n{_tail(ran.output)}"
        return self._guarded("run_lint", go)

    def show_diff(self) -> str:
        """Everything changed since the attempt began, new files included."""
        def go():
            if self.base is None:
                return "no changes: nothing to compare with"
            return _tail(self.base.diff(leave_out=set(self.ws.hidden)), 30_000) or "no changes"
        return self._guarded("show_diff", go)


# The tools each role gets. Named here, in frozen code, so a role's reach is
# decided by the harness and not by anything a model wrote.
CODER_TOOLS = ("list_files", "read_file", "search", "outline", "edit_file", "write_file",
               "delete_file", "run_tests", "run_lint", "show_diff")
TESTER_TOOLS = ("list_files", "read_file", "search", "outline", "write_file",
                "delete_file", "run_tests", "run_lint", "show_diff")


def register(agent, tools: Tools, names: tuple[str, ...]) -> None:
    for name in names:
        agent.tool_plain(name=name)(getattr(tools, name))
