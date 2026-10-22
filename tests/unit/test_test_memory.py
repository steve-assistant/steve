"""A process a test starts must not reserve memory it will never use.

On 8 October 2026 upgrade #89 was approved by the judge and rejected at the
gate on tests/gate/test_zero_downtime.py::test_no_requests_dropped_during_swap:
"new worker never became healthy", 1 failed and 1170 passed, in the coding
sandbox. The worker's own log said "OpenBLAS error: Memory allocation still
failed after 10 retries, giving up." The sandbox's Job Object lets its whole
process tree commit 6 GiB, and `pytest tests/gate -n 3` was asking for 6.03
GiB: OpenBLAS reserves a work buffer for each of the machine's threads as soon
as numpy is imported -- 500 MiB for `import numpy` and nothing else, on this
laptop's sixteen -- and every pytest worker, supervisor-started worker and
test child paid it. The processes that started last had nothing to allocate
with. Capped to one thread, the same run peaks at 1.87 GiB.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIB = 2**20


def _numpy_commit_mib(env: dict) -> int:
    """Committed memory of a bare process that has imported numpy, as the
    Job Object counts it (`private` in psutil on Windows)."""
    code = "import numpy, psutil; print(psutil.Process().memory_info().private)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, env=env, cwd=str(REPO)).stdout
    return int(out) // MIB


def test_a_process_that_a_test_starts_does_not_reserve_a_buffer_for_every_core():
    """Asked of a child with this session's environment, which is what a
    worker, a supervisor or a test's subprocess gets. A generous bound: a
    capped numpy process commits 18 MiB here, an uncapped one 500, and a
    smaller machine's uncapped one is smaller -- on which this proves less,
    and fails nothing."""
    committed = _numpy_commit_mib(dict(os.environ))

    assert committed < 200, (
        f"a process that only imported numpy committed {committed} MiB; OpenBLAS is "
        "reserving a buffer per core again (OPENBLAS_NUM_THREADS, tests/gate/fences.py)")


def test_the_shared_fences_cap_openblas_before_anything_can_import_numpy():
    """Both conftests import `tests/gate/fences.py` first, so setting it there
    is early enough in every pytest process -- and a variable set after numpy
    has loaded does nothing. Asked of a fresh interpreter with the variable
    absent, importing only that module."""
    env = {k: v for k, v in os.environ.items() if k != "OPENBLAS_NUM_THREADS"}
    code = ("import os, tests.gate.fences; print(os.environ.get('OPENBLAS_NUM_THREADS'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, env=env, cwd=str(REPO)).stdout.strip()

    assert out == "1"


def test_a_developer_who_asks_for_more_blas_threads_gets_them():
    """`setdefault`, not an overwrite: someone profiling a numpy-heavy change
    can export their own number, and the fences do not undo it."""
    env = {**os.environ, "OPENBLAS_NUM_THREADS": "4"}
    code = ("import os, tests.gate.fences; print(os.environ.get('OPENBLAS_NUM_THREADS'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, env=env, cwd=str(REPO)).stdout.strip()

    assert out == "4"
