"""What pytest said: the failing tests' ids, their tracebacks, and its
output without the progress dots.

These were the parts of core/coder/baseline.py that read a run rather than
compare it with the base commit; that comparison went with git in step 6.
Read from the text a run prints, because the run is the sandbox's and its
files are not to be trusted.
"""

from __future__ import annotations

import re

SUITE_TIMEOUT_S = 900

# `-rfE`, never `-rf`: with `f` alone pytest leaves setup and collection
# errors out of its summary, so the seventeen errors test_zero_downtime
# raised on a loaded laptop would have named no test.
REPORT = "-rfE"
# The most test ids kept on an outcome: a change that breaks a conftest fails
# thousands of tests, and the outcome is read by people.
KEEP_IDS = 25

# A node id starts with its file's path. `(.+?)` alone also read pytest's
# captured-log lines -- `ERROR    agent.mail:test_x.py:5 search failed` -- as a
# failing test (8 October 2026 audit).
_REPORTED = re.compile(r"^(?:FAILED|ERROR) ([\w./\\-]+\.py(?:::.+?)?)(?: - .*)?$",
                       re.MULTILINE)
_PROGRESS = re.compile(r"^[.sSxXFEp]+\s*\[\s*\d+%\]$|^bringing up nodes\.\.\.$|^\[gw\d+\] ")
_COLOUR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SECTION = re.compile(r"^=+ (?:ERRORS|FAILURES) =+$")
_SUMMARY = re.compile(r"^=+ short test summary info =+$")


def suite_argv(python: str, ignore: tuple[str, ...] | list[str] = ()) -> list[str]:
    """The whole suite, as every check runs it."""
    return [python, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "--tb=short",
            REPORT, *[f"--ignore={f}" for f in ignore]]


def failing(output: str) -> list[str]:
    """Node ids pytest reported failed or erroring, once each, in order.
    Whole: a parametrized id can hold a space (`test_x[a b]`), which a
    `\\S+` cut in half. Read from pytest's short summary when there is one:
    above it are the tracebacks and captured output, where a test may print
    anything."""
    output = _uncoloured(output)
    heads = list(_SUMMARY.finditer(output))
    if heads:
        output = output[heads[-1].end():]
    found: list[str] = []
    for match in _REPORTED.finditer(output):
        node = match.group(1).strip()
        if node and node not in found:
            found.append(node)
    return found


def listed(ids: list[str]) -> list[str]:
    """`ids` as an outcome keeps them: the first `KEEP_IDS`, then one entry
    saying how many more."""
    if len(ids) <= KEEP_IDS:
        return list(ids)
    return [*ids[:KEEP_IDS], f"... and {len(ids) - KEEP_IDS} more"]


def _uncoloured(output: str) -> str:
    """`output` without terminal colour codes. Under pytest-xdist the
    workers colour an assertion's diff even when nothing is a terminal, so
    the lines a coder is told to fix read `E     ESC[91m- cESC[39;49;00m`."""
    return _COLOUR.sub("", output)


def condensed(output: str) -> str:
    """pytest's output without its progress lines. The judge's brief keeps
    the head of each check, and for a suite of 3,661 tests the head was
    dots: the line saying what passed was the part cut off."""
    return "\n".join(line for line in _uncoloured(output).splitlines()
                     if not _PROGRESS.match(line.strip()))


def failures(output: str, limit: int = 8_000) -> str:
    """The tracebacks -- each failing assertion with its `E` lines -- and
    the summary, from a run's output: what the coder needs to fix a test,
    without the rest of the suite around it."""
    output = _uncoloured(output)
    lines = output.splitlines()
    start = next((i for i, line in enumerate(lines) if _SECTION.match(line)), None)
    if start is None:
        text = output.strip()
    else:
        body = [line for line in lines[start:] if not _PROGRESS.match(line.strip())]
        text = "\n".join(line for line in body if not _SUMMARY.match(line)).strip()
    return text if len(text) <= limit else text[:limit] + f"\n[... {len(text) - limit} more]"
