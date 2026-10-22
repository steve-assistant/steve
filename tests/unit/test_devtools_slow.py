"""A slow test is shown at the line where its time went.

On 14 October 2026 the zero-downtime tests took 117 s of a 407 s run, and
the timing audit could say which tests were slow and not why. These run
pytest for real, with `devtools.slow` loaded as its command loads it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS = (
    "import asyncio\nimport time\n\n\n"
    "def _wait():\n    time.sleep(0.6)\n\n\n"
    "def test_waits():\n    _wait()\n\n\n"
    "async def test_awaits():\n    await asyncio.sleep(0.6)\n\n\n"
    "def test_quick():\n    pass\n"
)


@pytest.mark.processes
def test_a_slow_test_is_shown_at_its_own_line_and_the_one_it_waited_on(tmp_path):
    """The test's line says which step was slow; the innermost line of the
    project says what it waited on. An async test waits off the thread's
    stack, so it is found on the event loop's tasks instead, or every
    sample would read "the loop is polling". Starts pytest, hence
    `processes`."""
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(TESTS, encoding="utf-8")

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
         "-p", "devtools.slow", "-p", "no:xdist"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(ROOT)})

    said = done.stdout.split("where the time went (devtools.slow)", 1)[-1]
    assert done.returncode == 0, done.stdout + done.stderr
    assert "tests/test_x.py:10 in test_waits  ->  tests/test_x.py:6 in _wait" in said
    assert "tests/test_x.py:14 in test_awaits  [awaits " in said
    assert "test_quick" in said  # timed, with no line long enough to show
    assert said.index("test_waits") < said.index("test_quick")
