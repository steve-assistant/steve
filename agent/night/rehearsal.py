"""The desk's rehearsal, from the night window (devtools/rehearse/).

The maintainer, 10 October 2026, of the rehearsal rig: "That way it can do voice
tests overnight or while I'm gone". Every sound test of the desk so far
found its bug by ear, on the evening after it shipped; the rig runs the real
desk -- its wake detector, the echo canceller, Whisper, Kokoro, the state
machine -- in a room that is software, against their own failures written as
scenarios. So the night runs it, and the morning briefing says in a line
what passed.

**When.** On a night after the desk's code changed since the last
rehearsal (`DESK_PATHS`), and at least weekly. After the coding attempts,
whose test suites it would otherwise slow, and never while hands-free is on:
it shares the GPU and processor with a desk the user may be using, and a starved
audio thread is what glitched their speakers on 9 October.

**Only in a checkout.** The rig is a developer's: an install ships no
`devtools/`, and has no git to ask whether the desk changed (step 8), so
there every night would fail, and say so.

**In a process of its own, never in the worker**, like the yardstick: the
rig patches the voice modules, the runtime and the sound devices for the
life of its process, and in the worker those are the live agent's. Its
output goes to data/logs/rehearse.log; its verdict is the newest
data/rehearsals/<when>/summary.json.

**Free.** Their assistant and GPT-Live are the rig's scripted fakes: no model
is called. A failed scenario is a ledger issue (`rehearsal_failed`), which
the night's own attempts can take up like any other.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from agent.night import child
from agent.night.report import NightReport
from core import paths

log = logging.getLogger("night.rehearsal")

ROOT = Path(__file__).resolve().parents[2]


def out_dir() -> Path:
    return paths.data_dir() / "rehearsals"


def log_path() -> Path:
    return paths.logs_dir() / "rehearse.log"


# What the desk is made of: a change to any of these since the last
# rehearsal makes the night rehearse again.
DESK_PATHS = ("voice", "agent/worker/desk.py", "agent/toolset/desk.py", "core/desk.py",
              "devtools/rehearse")
EVERY_S = 6.5 * 24 * 3600
# Nine scenarios took about twelve minutes on 10 October 2026, models warm
# in fifteen seconds.
TIMEOUT_S = 40 * 60


def last() -> dict | None:
    """The newest rehearsal's summary, or None."""
    if not out_dir().exists():
        return None
    for folder in sorted((p for p in out_dir().iterdir() if p.is_dir()), reverse=True):
        found = folder / "summary.json"
        if found.is_file():
            try:
                return json.loads(found.read_text(encoding="utf-8"))
            except ValueError:
                continue
    return None


def _changed_since(sha: str) -> bool:
    """Whether the desk's code differs from `sha`'s. True when git cannot
    say: an unknown commit is a reason to rehearse, not to skip."""
    done = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", sha, "HEAD", "--",
                           *DESK_PATHS], capture_output=True, text=True, check=False,
                          creationflags=child.NO_WINDOW)
    return done.returncode != 0 or bool(done.stdout.strip())


def due(now: float | None = None) -> str | None:
    """Why the night should rehearse, or None if it need not."""
    if not paths.is_source_tree():
        return None
    found = last()
    if found is None:
        return "never rehearsed"
    if not found.get("sha") or _changed_since(found["sha"]):
        return "the desk changed"
    try:
        at = dt.datetime.fromisoformat(found["at"]).timestamp()
    except (KeyError, ValueError):
        return "the last rehearsal has no date"
    if (time.time() if now is None else now) - at >= EVERY_S:
        return "a week since the last"
    return None


def _hands_free_on() -> bool:
    from agent.worker import desk

    return desk.switched_on()


def _run() -> int:
    """The rig, as a child process: its exit code, or -1 if it was killed
    for overrunning."""
    return child.run([sys.executable, "-m", "devtools.rehearse", "all"], log_path(), TIMEOUT_S,
                     env={**os.environ, "PYDANTIC_AI_NO_BANNER": "1"})


def line(summary: dict) -> str:
    """The morning's line: how many passed, and what failed."""
    passed, of = summary.get("passed", 0), summary.get("of", 0)
    failed = summary.get("failed") or []
    said = f"Desk rehearsal: {passed} of {of} scenarios passed"
    if failed:
        said += "; failed: " + "; ".join(
            f"{f['scenario']} ({', '.join(f['checks'])})" for f in failed)
    return said + "."


async def rehearse(conn: sqlite3.Connection | None, report: NightReport, *,
                   now: float | None = None) -> float:
    """Tonight's rehearsal, if one is due, into the night's report. Returns
    what it spent -- nothing, the fakes answer -- and never raises."""
    try:
        why = due(now)
        if why is None:
            return 0.0
        if _hands_free_on():
            report.rehearsed.append("Desk rehearsal skipped: hands-free was on.")
            return 0.0
        started = time.time()
        code = await asyncio.to_thread(_run)
        found = last()
        fresh = found is not None and _when(found) >= started - 5
        if not fresh or code == -1:
            reason = "it ran out of time" if code == -1 else f"it exited {code}"
            report.rehearsed.append(f"Desk rehearsal did not finish: {reason} "
                                    "(data/logs/rehearse.log).")
            return 0.0
        report.rehearsed.append(line(found))
        if conn is not None:
            from agent import issues

            for failure in found.get("failed") or []:
                issues.record(conn, "rehearsal_failed", failure["scenario"],
                              f"the desk rehearsal's {failure['scenario']} failed: "
                              + ", ".join(failure["checks"]))
        log.info("rehearsed (%s): %s", why, report.rehearsed[-1])
        return 0.0
    except Exception as exc:  # noqa: BLE001 - the rest of the night still reports
        log.exception("the desk rehearsal failed")
        report.rehearsed.append(f"Desk rehearsal stopped: {type(exc).__name__}: {exc}"[:200])
        return 0.0


def _when(summary: dict) -> float:
    try:
        return dt.datetime.fromisoformat(summary["at"]).timestamp()
    except (KeyError, ValueError):
        return 0.0
