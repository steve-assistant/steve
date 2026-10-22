"""Where a slow test spends its time, line by line.

On 14 October 2026 the suite's timing audit could say which tests were slow
(`test_zero_downtime`: 117 s of a run's 407 s) and not why: a test that
starts a supervisor and waits on it shows only as "18 s". This runs tests
in one process with every test's stack sampled `EVERY_S` apart, and says,
for each test, how long it spent at which line: its own line (or its
fixture's) and the innermost line of the project under it, and what that
was doing at the bottom (the innermost Python function, or what an async
test awaits).

    python -m devtools.slow tests/gate/test_zero_downtime.py [-k name] [--top 6]

`devtools.check` names its slowest tests after every run, with this command
for the slowest. A wait shows as the line that waits, so the cause of a slow
wait (a child process starting, a poll's interval) is one step further: the
line names where to look. It found, the same day, a supervisor that published
its port before listening, a third of a second for each httpx client, and a
watchdog's fifteen-second pace (core/supervisor.py, core/localapi.tls).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import functools
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from devtools import utf8_stdout

ROOT = Path(__file__).resolve().parents[1]
EVERY_S = 0.05
SHOWN_AT_LEAST_S = 0.2  # lines under this are left out of a test's report


@functools.cache
def _ours(filename: str, root: Path) -> str:
    """`filename` relative to the tests' root, or "" for a file not the
    project's own: outside it, in its .venv, or this tool's."""
    try:
        rel = Path(filename).resolve().relative_to(root)
    except (ValueError, OSError):
        return ""
    return "" if rel.parts[:1] in ((".venv",), ("devtools",)) else rel.as_posix()


def _places(frames: list, root: Path) -> tuple[str, str]:
    """The outermost frame of the project's tests and the innermost of the
    project, from frames listed innermost first."""
    test = inner = ""
    for frame in frames:
        if rel := _ours(frame.f_code.co_filename, root):
            here = f"{rel}:{frame.f_lineno} in {frame.f_code.co_name}"
            inner = inner or here
            if rel.startswith("tests/"):
                test = here
    return test, inner


def _awaiting(task) -> tuple[list, str]:
    """A suspended task's frames, innermost first, down its chain of awaits,
    and what the innermost awaits."""
    frames, coro = [], getattr(task, "get_coro", lambda: None)()
    while coro is not None:
        frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
        if frame is None:
            break
        frames.append(frame)
        awaited = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None)
        if awaited is None or not (hasattr(awaited, "cr_frame") or hasattr(awaited, "gi_frame")):
            return frames[::-1], type(awaited).__name__ if awaited is not None else ""
        coro = awaited
    return frames[::-1], ""


def _where(frame, root: Path) -> tuple[str, str, str]:
    """A sample's three places: the outermost frame of the project's tests,
    the innermost frame of the project, and the innermost frame at all.

    An async test waits with its coroutine off the thread's stack, which
    then shows only the event loop polling: the loop's tasks are read
    instead, and the first one in the tests is the sample."""
    bottom = frame.f_code.co_name
    stack, loop = [], None
    while frame is not None:
        stack.append(frame)
        if loop is None and isinstance(frame.f_locals.get("self"), asyncio.AbstractEventLoop):
            loop = frame.f_locals["self"]
        frame = frame.f_back
    test, inner = _places(stack, root)
    if test or loop is None:
        return test, inner, bottom
    try:
        tasks = list(asyncio.all_tasks(loop))
    except RuntimeError:  # the loop's set of tasks changed under us; skip this sample
        return test, inner, bottom
    for task in tasks:
        frames, awaited = _awaiting(task)
        found, deepest = _places(frames, root)
        if found:
            return found, deepest, f"awaits {awaited}" if awaited else bottom
    return test, inner, bottom


class _Sampler:
    def __init__(self, thread_id: int, root: Path) -> None:
        self.thread_id, self.root = thread_id, root
        self.counts: collections.Counter[tuple[str, str, str]] = collections.Counter()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="devtools.slow")

    def _run(self) -> None:
        while not self.done.wait(EVERY_S):
            frame = sys._current_frames().get(self.thread_id)
            if frame is not None:
                self.counts[_where(frame, self.root)] += 1


_FOUND: dict[str, tuple[float, collections.Counter]] = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    sampler = _Sampler(threading.get_ident(), item.config.rootpath)
    started = time.monotonic()
    sampler.thread.start()
    try:
        yield
    finally:
        sampler.done.set()
        sampler.thread.join()
        _FOUND[item.nodeid] = (time.monotonic() - started, sampler.counts)


def report(found: dict[str, tuple[float, collections.Counter]], top: int) -> list[str]:
    lines = []
    for node, (took, counts) in sorted(found.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"{took:6.1f} s  {node}")
        for (test, inner, bottom), n in counts.most_common(top):
            if n * EVERY_S < SHOWN_AT_LEAST_S:
                break
            under = f"  ->  {inner}" if inner and inner != test else ""
            where = test or "(outside the tests)"
            lines.append(f"    {n * EVERY_S:5.1f} s  {where}{under}  [{bottom}]")
    return lines


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    top = int(config.getini("slow_top"))
    terminalreporter.section("where the time went (devtools.slow)")
    for line in report(_FOUND, top):
        terminalreporter.write_line(line)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini("slow_top", "lines shown per test (devtools.slow)", default="6")


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    parser = argparse.ArgumentParser(prog="python -m devtools.slow",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("targets", nargs="+", help="test files or node ids")
    parser.add_argument("-k", default=None, help="only tests matching this expression")
    parser.add_argument("--top", type=int, default=6, help="lines shown per test")
    args = parser.parse_args(argv)
    # One process: a worker's stacks are no clearer, and the load of others
    # is what this is meant to see past.
    cmd = [sys.executable, "-m", "pytest", *args.targets, "-q", "-p", "no:cacheprovider",
           "-p", "devtools.slow", "-o", f"slow_top={args.top}", "-p", "no:xdist"]
    if args.k:
        cmd += ["-k", args.k]
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
