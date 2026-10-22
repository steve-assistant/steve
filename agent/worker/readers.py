"""The local readers, installed from the app with a click.

When Jev can't answer -- no key for it, an outage, a cap reached -- PIGuard
(`core/detector.py`) and Tev1 (`core/decider.py`) read new mail and pages
in its place (`core/screen.py`). Where neither is here, such an item is cut
to an extract, or withheld. They were installed only by `python -m
core.detector setup` and `python -m core.decider setup`, commands a user of
the app does not have, so on a stranger's laptop with no key for Jev every
new item was cut short, and nothing said why or offered the fix.

- `GET /control/readers`: which of the two are here, whether an install is
  running, how the last one failed, and what they add and take
  (`features.OFFERS`).
- `POST /control/readers/install`: an install, in a thread of its own, as a
  module's build is (`agent/modbuild.py`): about 7 GB to fetch, which no
  request could wait for. One at a time; asked again while one runs, it
  starts nothing. Each setup checks what it fetches against its pinned hash
  and keeps a file already there and whole, so an install cut short starts
  again where it stopped.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import features
from core import decider, detector, identity

log = logging.getLogger(__name__)

# The install running now, or how the last one ended. The worker's own: a
# restart forgets an install cut short, and the readers' state says what is
# here.
_install: dict = {"thread": None, "error": None}


def _run() -> None:
    from agent.wake import kept_awake

    try:
        with kept_awake(f"{identity.NAME}: installing the local readers"):
            detector.setup()
            decider.setup()
    except Exception as exc:  # noqa: BLE001 - nothing above a thread to catch it
        log.exception("installing the local readers")
        _install["error"] = f"{type(exc).__name__}: {exc}"[:300]


def _installing() -> bool:
    thread = _install["thread"]
    return thread is not None and thread.is_alive()


async def state(_: Request) -> JSONResponse:
    adds, takes = features.OFFERS["READERS"]
    return JSONResponse({"piguard": await asyncio.to_thread(detector.installed),
                         "tev1": await asyncio.to_thread(decider.installed),
                         "installing": _installing(), "error": _install["error"],
                         "adds": adds, "takes": takes})


async def install(_: Request) -> JSONResponse:
    if not _installing():
        thread = threading.Thread(target=_run, name="readers-install", daemon=True)
        _install.update(thread=thread, error=None)
        thread.start()
    return JSONResponse({"ok": True})


ROUTES = [
    Route("/control/readers", state),
    Route("/control/readers/install", install, methods=["POST"]),
]
