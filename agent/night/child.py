"""A night job in a process of its own: the yardstick and the desk's
rehearsal (agent/night/yardstick.py, agent/night/rehearsal.py).

Each ran its child the same way, in a copy of its own: output appended to a
log under the time it started, no console window, and -- when it overran --
the whole tree ended: the venv's python.exe is a launcher with the real
interpreter as its child.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(args: list[str], log: Path, timeout_s: float, *, env: dict | None = None) -> int:
    """`args` in the project folder, its output appended to `log`: its exit
    code, or -1 if it overran `timeout_s` and was ended."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as out:
        out.write(f"\n--- {dt.datetime.now():%Y-%m-%d %H:%M} ---\n")
        out.flush()
        child = subprocess.Popen(args, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT,
                                 env=env, creationflags=NO_WINDOW)
        try:
            return child.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
            return -1
