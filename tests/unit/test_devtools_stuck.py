"""A stuck test is stopped and named, and the rest of the run goes on.

Item 8's check (14 October 2026) waited 900 s on something, said "timed
out", and named nothing; the same tests took 226 s when run again. These
run pytest for real, with `devtools.stuck` loaded as the check loads it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from devtools import check, junit, stuck

ROOT = Path(__file__).resolve().parents[2]
TESTS = (
    "import time\n\n\n"
    "def test_stuck():\n    time.sleep(60)\n\n\n"
    "def test_fine():\n    pass\n"
)


def _pytest(tree: Path, *args: str) -> tuple[subprocess.CompletedProcess, float]:
    (tree / "tests").mkdir(exist_ok=True)
    (tree / "tests" / "test_x.py").write_text(TESTS, encoding="utf-8")
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
         "-p", "devtools.stuck", "-o", "stuck_after_s=2", *args],
        cwd=tree, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(ROOT)})
    return done, time.monotonic() - started


@pytest.mark.processes
def test_a_stuck_test_under_xdist_fails_by_name_and_the_others_still_run(tmp_path):
    """The run reports the stuck test failed, runs the rest, and ends: the
    one stuck test no longer holds the run to its limit. Its stacks, which
    a worker's captured error stream lost, are in the folder named for
    them. Starts pytest and its workers, hence `processes`."""
    xml, folder = tmp_path / "out.xml", tmp_path / "stuck"

    done, took = _pytest(tmp_path, "-n", "2", f"--junitxml={xml}", "-o", f"stuck_dir={folder}")

    outcomes = {o.node: o for o in junit.read(xml, tmp_path)}
    # xdist says "error" for a crashed worker's test; the check reads both as failing.
    assert outcomes["tests/test_x.py::test_stuck"].status in ("failed", "error")
    assert "crashed" in outcomes["tests/test_x.py::test_stuck"].reason
    assert outcomes["tests/test_x.py::test_fine"].status == "passed"
    assert took < 50, f"the stuck test was not stopped ({took:.0f} s)"
    assert "crashed while running 'tests/test_x.py::test_stuck'" in done.stdout
    [(node, _, dump)] = stuck.stacks(folder)
    assert node == "tests/test_x.py::test_stuck"
    assert ("tests" + os.sep + "test_x.py", "5", "test_stuck") in check.ON_A_STACK.findall(dump)


@pytest.mark.processes
def test_a_stuck_test_run_alone_ends_the_run_with_its_stack(tmp_path):
    """Alone there is no worker to replace: the run ends, and the stack the
    check reads names the test."""
    done, took = _pytest(tmp_path, "-k", "stuck")

    assert done.returncode != 0 and took < 50
    assert ("tests" + os.sep + "test_x.py", "5", "test_stuck") in check.ON_A_STACK.findall(
        done.stdout + done.stderr)


def test_the_limit_on_a_test_is_well_under_the_runs_and_over_the_slowest_test():
    """Thirty seconds was the slowest test on 14 October, under three
    workers; the run's own limits are only a backstop."""
    assert 30 * 3 <= stuck.STUCK_S < min(check.LIMIT_S.values())
