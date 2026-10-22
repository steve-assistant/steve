"""The yardstick's weekly run (agent/autonomy_eval.py), from the night window.

Weekly, like the model scout's round (agent/night/scouting.py), so a change
-- a new model in the lineup, a prompt, a new module -- shows in the
numbers within a week of landing, and the morning digest says them in a
line.

In a process of its own, never in the worker: the yardstick replaces their
mail, calendar, Drive, the web and every action with fakes for the life of
its process, and in the worker those would be the live agent's. So the
night starts `python -m agent.autonomy_eval run` as a child with a timeout,
its output to a file rather than a pipe, and kills the whole tree if it
overruns (docs/protocols.md section 5).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sys
import time
from pathlib import Path

from agent import autonomy_eval
from agent.night import child
from agent.night.report import NightReport
from core import paths

log = logging.getLogger("night.yardstick")


def log_path() -> Path:
    return paths.logs_dir() / "yardstick.log"


# A week, less a few hours, as the scout: a night window that slides a
# little later each week still finds it due.
EVERY_S = 6.5 * 24 * 3600
# Ten scenarios three times each cost about five cents on 4 October 2026.
BUDGET_USD = 0.50
TIMEOUT_S = 45 * 60


def due(now: float | None = None) -> bool:
    """A week since the last run's report, or never run; never, installed.
    Its scenarios and its world are under tests/, which does not ship, so
    an installed Steve's run could only fail, and say so every week."""
    if not paths.is_source_tree():
        return False
    reports = autonomy_eval.reports_dir()
    found = sorted(reports.glob("*.json")) if reports.exists() else []
    if not found:
        return True
    return (time.time() if now is None else now) - found[-1].stat().st_mtime >= EVERY_S


def command() -> list[str]:
    """The weekly run: the scenarios, and no module builds. One build may
    spend half a dollar (`coder.attempt_budget_usd`), and what the builds
    cost together is unknown until a real run measures it: building them
    weekly is the maintainer's call, on that figure."""
    return [sys.executable, "-m", "agent.autonomy_eval", "run", "--budget", str(BUDGET_USD),
            "--build-budget", "0"]


def _run() -> int:
    """The yardstick, as a child process: its exit code, or -1 if it was
    killed for overrunning."""
    return child.run(command(), log_path(), TIMEOUT_S)


async def measure(report: NightReport, *, now: float | None = None) -> float:
    """This week's run, if one is due, into the night's report. Returns what
    it spent; never raises."""
    if not due(now):
        return 0.0
    started = time.time()
    try:
        code = await asyncio.to_thread(_run)
        found = autonomy_eval.latest()
        fresh = (found is not None
                 and dt.datetime.fromisoformat(found["at"]).timestamp() >= started - 5)
        if code != 0 or not fresh:
            why = "it ran out of time" if code == -1 else f"it exited {code}"
            report.measured.append(f"The yardstick did not finish: {why} "
                                   "(data/logs/yardstick.log).")
            return 0.0
        report.measured.append(autonomy_eval.line(found))
        return float(found.get("cost_usd") or 0.0)
    except Exception as exc:  # noqa: BLE001 - the rest of the night still reports
        log.exception("the yardstick failed")
        report.measured.append(f"The yardstick stopped: {type(exc).__name__}: {exc}"[:200])
        return 0.0
