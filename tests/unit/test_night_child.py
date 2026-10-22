"""A night job's own process (agent/night/child.py). The yardstick and the
desk's rehearsal both run through it, in the small hours with nobody
watching. These run a harmless child, never either job."""

from __future__ import annotations

import sys
import time

import psutil

from agent.night import child


def test_a_jobs_exit_code_comes_back_and_each_run_is_added_to_its_log(tmp_path):
    """The log is the only record of a run nobody watched: last week's must
    still be there under this week's."""
    log = tmp_path / "logs" / "job.log"

    first = child.run([sys.executable, "-c", "print('first run')"], log, 60)
    second = child.run([sys.executable, "-c", "import sys; print('second run'); sys.exit(3)"],
                       log, 60)

    assert (first, second) == (0, 3)
    kept = log.read_text(encoding="utf-8")
    assert kept.count("\n--- ") == 2 and kept.index("first run") < kept.index("second run")


def test_a_job_that_overruns_is_ended_and_said_to_have_overrun(tmp_path):
    """Waited on without end, one stuck job would hold up the rest of the
    night; left running, it would go on holding the laptop. The pid looked
    for is the interpreter's, not the venv launcher's that was started."""
    pid_file = tmp_path / "pid"
    script = (f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
              "time.sleep(60)")

    assert child.run([sys.executable, "-c", script], tmp_path / "job.log", 6) == -1

    interpreter = int(pid_file.read_text())
    deadline = time.monotonic() + 15
    while psutil.pid_exists(interpreter) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not psutil.pid_exists(interpreter), "the interpreter outlived the job"
