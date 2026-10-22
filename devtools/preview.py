"""The app's account and module screens in a browser, against a backend on the fakes.

The app reaches its backend only through the desktop shell, which holds the
token, so a screen built for accounts could not be looked at before it ran
against the user's real ones: Add account, each health state and removal's
choices were checked by the compiler alone. The Modules page is the same:
an approval card, a rollback or an import could otherwise first be seen on
a real module. This starts the backend half in pytest
(`tests/unit/preview_ui.py`, where the test fences hold) and Vite in its
"preview" mode, whose proxy passes the app's requests on with that
backend's token; the page itself never holds it.

    python -m devtools.preview [--port 5174] [--minutes 30]

Open http://localhost:<port>/ and choose Settings, then Accounts; or
Modules. Ctrl-C stops both halves; the backend also stops by itself after
--minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from core import paths

ROOT = Path(__file__).resolve().parent.parent
HARNESS = "tests/unit/preview_ui.py"
STARTS_WITHIN_S = 120  # pytest's start, the world's accounts, one test


def _stop(process: subprocess.Popen | None) -> None:
    # Only a process still running: an ended one's id may be another's now.
    if process is not None and process.poll() is None:
        if os.name == "nt":
            # Vite runs as `npx.cmd`, whose node is a child of its own:
            # terminating the batch file alone left that node serving the
            # page after the preview had stopped, holding the port.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           capture_output=True, check=False)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m devtools.preview",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=5174, help="Vite's port")
    parser.add_argument("--minutes", type=float, default=30, help="how long the backend serves")
    args = parser.parse_args(argv)

    # A preview stopped by killing its process tree leaves its file; each is
    # read once, at its start, so an older one is never needed again.
    for left in paths.scratch_dir().glob("preview-*.json"):
        left.unlink(missing_ok=True)
    told = paths.scratch_dir() / f"preview-{os.getpid()}.json"
    backend = vite = None
    try:
        backend = subprocess.Popen(
            [sys.executable, "-m", "pytest", HARNESS, "-q", "-s", "-p", "no:cacheprovider"],
            cwd=ROOT, env={**os.environ, "PYDANTIC_AI_NO_BANNER": "1",
                           "STEVE_UI_PREVIEW_FILE": str(told),
                           "STEVE_UI_PREVIEW_MINUTES": str(args.minutes)})
        deadline = time.monotonic() + STARTS_WITHIN_S
        while not told.exists():
            if backend.poll() is not None or time.monotonic() > deadline:
                print("the preview's backend did not start; its output is above")
                return 1
            time.sleep(0.2)
        served = json.loads(told.read_text(encoding="utf-8"))
        vite = subprocess.Popen(
            ["npx.cmd" if os.name == "nt" else "npx", "vite", "--mode", "preview",
             "--port", str(args.port), "--strictPort"],
            cwd=ROOT / "ui", env={**os.environ,
                                 "STEVE_UI_PREVIEW_BACKEND": f"http://127.0.0.1:{served['port']}",
                                 "STEVE_UI_PREVIEW_TOKEN": served["token"]})
        while backend.poll() is None and vite.poll() is None:
            time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        _stop(vite)
        _stop(backend)
        told.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
