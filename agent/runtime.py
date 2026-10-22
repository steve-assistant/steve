"""The worker's live state: its connections, links and channels, in one place.

Set by `agent/server.py` as the worker starts and takes up its duties; read by
anything that needs the process's database or Signal bridge --
the toolset's executors, the background loops, the HTTP routes. And the
pause every background loop takes (`nap`), which the heartbeat ends when the
computer wakes (agent/awake.py).

**Why a module of its own.** These used to be globals of `agent/server.py`,
so anything that needed them imported the server. But the worker runs as
`python -m agent.server`, which makes that file `__main__`: importing it by
name produced a *second* copy whose globals were all None, and a tool
reported its link offline while it was up. An alias papers
over that in the server; this removes the reason to import the server at all.
Nothing imports `agent.server` except the process that runs it.

**Everything here is None, or empty, outside a running worker** -- in a CLI
run or a test. Code that reads it says what it does then;
`toolset.clients.commitment_store` opens its own connection, for instance.

Only `conn` is thread-bound: it belongs to the event loop's thread. In a
thread, use `agent.offloop.with_own_conn` instead.
"""

from __future__ import annotations

import sqlite3
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio

    import uvicorn
    from starlette.websockets import WebSocket

    from agent.jobs import JobWorker
    from agent.loop import Conversation
    from channels.bridge import ChannelBridge
    from voice.pipeline import VoiceSession

# The worker's database connection, on the event loop's thread.
conn: sqlite3.Connection | None = None

# The job queue, and the HTTP server it runs beside.
jobs: JobWorker | None = None
http: uvicorn.Server | None = None

# Signal, when it is set up.
signal: ChannelBridge | None = None
signal_state: dict[str, Any] = {"state": "cold"}

# Telegram, when it has a bot's token: polling, paired or waiting for a code.
telegram: ChannelBridge | None = None
telegram_state: dict[str, Any] = {"state": "cold"}

# The microphone and speakers: one session, because there is one of each.
voice: VoiceSession | None = None
voice_warm: dict[str, Any] = {"state": "cold"}
# The desk turn this task is running, if any (voice/engine.Turn): where a tool
# their assistant calls in it records what it asks of the desk -- notes of the
# room, a live conversation -- and whether it began as a cut-in. A context
# variable, so a turn on another channel running beside it sees none; and
# the asks live on the turn, so they die with it. Before 9 October 2026 they
# were four names here, two of them module globals, and a turn that never
# read them left them set for the next unrelated "hey Steve" (audit of 8
# October 2026).
desk_turn: ContextVar[Any] = ContextVar("desk_turn", default=None)

# Tray websockets listening for finished jobs and the outbox's new messages.
listeners: set[WebSocket] = set()
# Set when a message is written, so the outbox duty sends it now rather than
# at its next round (agent/worker/links.notify_user). Made by the duty, on
# its own loop.
outbox_wake: asyncio.Event | None = None
# Those of them in the middle of a turn: kept through a swap until answered,
# where the idle ones are closed so the app reconnects to the new worker
# (agent/worker/routes.ws_session).
turning: set[WebSocket] = set()

# Open conversations by session id, so a reconnecting client keeps its
# history and its prompt cache.
conversations: dict[int, Conversation] = {}

# What the attention loop's last tick did, for `/attention/status`.
attention_state: dict[str, Any] = {"last_tick": None}

# The background loops waiting out their pause (`nap`), each on a future that
# `woke` ends early.
_napping: set[asyncio.Future] = set()


async def nap(seconds: float) -> bool:
    """Wait `seconds`, or until the heartbeat notices the computer woke
    (`woke`), whichever is first. True if woken.

    The background loops wait on this, not `asyncio.sleep`. A pause timed by
    the event loop's clock knew nothing of a night asleep: after one, a
    loop could wait out the rest of its period before it looked at what had
    come due (agent/awake.py)."""
    import asyncio

    woken = asyncio.get_running_loop().create_future()
    _napping.add(woken)
    try:
        await asyncio.wait_for(woken, max(seconds, 0))
        return True
    except TimeoutError:
        return False
    finally:
        _napping.discard(woken)


def woke() -> None:
    """End every nap under way, and wake the outbox: the computer has just
    woken, and what came due while it slept goes now. A nap begun after
    this is not ended by it."""
    for woken in list(_napping):
        if not woken.done():
            woken.set_result(True)
    if outbox_wake is not None:
        outbox_wake.set()
