"""A test stuck past its time is stopped and named, and the run goes on.

On 14 October 2026 item 8's check ran out of its 900 s at 97% and said
only "timed out": the whole run waited on something, and nothing said
what. The same tests run again took 226 s. A limit on the run cannot tell
a stuck test from a slow run, and it throws away what finished; a limit on
each test can.

Loaded by the check (`-p devtools.stuck`): a test still running after
`STUCK_S` has every thread's stack written out, and its process ends. Under
xdist, xdist names the test ("worker 'gw0' crashed while running ..."),
starts a new worker, and the run goes on. Run alone, the run ends.

The stacks go to a file of the process's own in `stuck_dir`, as the check
asks: written to a worker's error stream, they were lost, since that is
the stream pytest captures. Each file starts with the test then running,
so a file with more than that line holds a stuck test's stacks (`stacks`).
With no folder named, they go to the process's error stream, where the
check names the test from them too (`devtools/check.ON_A_STACK`). The
slowest test seen, on 14 October under three workers, took 30 s.

pytest's own `faulthandler_timeout` prints the stacks but lets the test
run on, and pytest-timeout would be a new dependency, so this is Python's
faulthandler with `exit=True`.
"""

from __future__ import annotations

import faulthandler
import os
import sys
from pathlib import Path

import pytest

STUCK_S = 120.0

# Where the stacks go with no folder named: the process's own error stream,
# kept before pytest's capture takes file descriptor 2 over. `-p` loads this
# module while the command line is read, before any capture starts.
_STDERR = os.fdopen(os.dup(sys.__stderr__.fileno()), "w")
_FILE: list = []  # this process's file in `stuck_dir`, once opened


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini("stuck_after_s", "a test running longer than this is stopped (devtools.stuck)",
                  default=str(STUCK_S))
    parser.addini("stuck_dir", "a folder for a stuck test's stacks, a file per process "
                  "(devtools.stuck)", default="")


def _out(config: pytest.Config, node: str):
    """Where this test's stacks would go: this process's file, emptied and
    headed with the test, or the error stream."""
    folder = config.getini("stuck_dir")
    if not folder:
        return _STDERR
    if not _FILE:
        Path(folder).mkdir(parents=True, exist_ok=True)
        # Open for the process's life: the stacks are written to it at any time.
        _FILE.append(open(Path(folder) / f"{os.getpid()}.txt", "w", encoding="utf-8"))  # noqa: SIM115
    out = _FILE[0]
    out.seek(0)
    out.truncate()
    out.write(f"{node}\n")
    out.flush()
    return out


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    faulthandler.dump_traceback_later(float(item.config.getini("stuck_after_s")),
                                      exit=True, file=_out(item.config, item.nodeid))
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


def stacks(folder: Path) -> list[tuple[str, Path, str]]:
    """Each stuck test in `folder`: its node id, its file, and the stacks."""
    found = []
    for path in sorted(folder.glob("*.txt")) if folder.is_dir() else []:
        try:
            node, _, dump = path.read_text(encoding="utf-8", errors="replace").partition("\n")
        except OSError:
            continue
        if dump.strip():
            found.append((node, path, dump))
    return found
