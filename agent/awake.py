"""Noticing that the computer slept, and catching up when it wakes.

Nothing of Steve's runs while the computer sleeps, and until step 4 nothing
noticed afterwards that it had:

- the background loops paused on `asyncio.sleep`, timed by the event loop's
  clock, so after a night asleep a loop could wait out the rest of its
  period before it looked at what had come due;
- the night's wait for its window, slept through, ended when the computer
  woke or later, and the night's suites then started beside the user's
  morning;
- a reply that waited eight hours on sleep came with no word of why, and the
  app could not say when Steve had last been asleep.

So a heartbeat, every minute (`tick`). Each beat moves the current stretch's
last beat on. A beat that finds the last one more than `GAP` old, by the
wall clock, starts a new stretch, and the gap before it was either the
computer asleep -- if this process had beaten before it, it was frozen with
the machine -- or the backend not running. Then every loop's pause ends
(`runtime.nap`, `runtime.woke`), so overdue reminders, a briefing still worth
sending and the outbox's retries go at once; and the backend holds a power
request for `HOLD_AFTER_WAKE`, so Windows does not put a machine the wake
timer brought up (agent/wake.py) back to sleep before they have gone.

And `KEEP_AWAKE` (core/knobs.py, off by default): while it is on and the
computer runs on mains power, the backend holds the same request, the one
`wake.kept_awake` makes, so an idle computer stays up for its messages. It
does not overrule a closed lid or the user's Sleep, and on battery it lets
go.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent import runtime, sqltime, wake
from core import db

log = logging.getLogger("awake")

# How often the heartbeat beats.
BEAT_S = 60

# A beat this much later than the one before means the two were not one
# stretch awake. Three beats: a busy event loop or a worker swap (up to
# forty seconds) can hold one back, and should not read as sleep.
GAP = timedelta(minutes=3)

# How long the backend holds the computer up after a wake. Windows puts a
# machine its wake timer woke back to sleep after about two idle minutes,
# and the work due on waking may take longer than that.
HOLD_AFTER_WAKE = timedelta(minutes=5)

# How long the stretches are kept: a week of them, for a look back.
KEEP = timedelta(days=7)

# A message read this long after it was sent waited, and one sent before the
# wake and read within `SAID_WITHIN` of it waited on the sleep. Later than
# that, something else held it up, and the sleep is not the reason to give.
WAITED = timedelta(minutes=10)
SAID_WITHIN = timedelta(minutes=30)


@dataclass(frozen=True)
class Slept:
    """A gap between two stretches awake, in local time."""

    since: datetime
    until: datetime
    how: str  # "asleep", or "stopped": the backend was not running


class _Hold:
    """A power request taken and let go between beats, as asked."""

    def __init__(self, why: str):
        self.why = why
        self._stack: contextlib.ExitStack | None = None

    @property
    def held(self) -> bool:
        return self._stack is not None

    def hold(self, wanted: bool) -> None:
        if wanted and self._stack is None:
            self._stack = contextlib.ExitStack()
            self._stack.enter_context(wake.kept_awake(self.why))
            log.info("holding the computer awake: %s", self.why)
        elif not wanted and self._stack is not None:
            self._stack.close()
            self._stack = None
            log.info("no longer holding the computer awake: %s", self.why)


@dataclass
class _Here:
    """What this process knows of its own beats."""

    beaten: bool = False
    woke_at: datetime | None = None
    # The wake (its stretch's `since`) a reply has said "sorry for the wait"
    # for: once per wake, not once per message that waited.
    said_for: str | None = None
    after_wake: _Hold = field(default_factory=lambda: _Hold("catching up after a wake"))
    kept: _Hold = field(default_factory=lambda: _Hold("kept awake while plugged in"))


_here = _Here()


def _local(text: str) -> datetime:
    return sqltime.moment(text).astimezone().replace(tzinfo=None)


def beat(conn: sqlite3.Connection, now: datetime | None = None) -> Slept | None:
    """Record that the backend is awake now. The gap this ends, if the last
    beat is more than `GAP` old; None if it is not, and on the first beat
    ever."""
    now = now or datetime.now()
    at = sqltime.stamp(now)
    slept = None
    with db.transaction(conn):
        last = conn.execute("SELECT id, last_beat FROM awake ORDER BY id DESC LIMIT 1").fetchone()
        before = _local(last["last_beat"]) if last else None
        if before is not None and now - before <= GAP:
            conn.execute("UPDATE awake SET last_beat = ? WHERE id = ?", (at, last["id"]))
        else:
            if before is not None:
                slept = Slept(before, now, "asleep" if _here.beaten else "stopped")
            conn.execute(
                "INSERT INTO awake (since, last_beat, gap_from, gap) VALUES (?, ?, ?, ?)",
                (at, at, last["last_beat"] if last else None, slept and slept.how))
            conn.execute("DELETE FROM awake WHERE last_beat < ?", (sqltime.stamp(now - KEEP),))
    _here.beaten = True
    return slept


def on_mains() -> bool:
    """Whether the computer runs on mains power now. False on battery, and
    where it cannot tell: not Windows, or Windows does not know."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class _Status(ctypes.Structure):
        _fields_ = [("ac_line", ctypes.c_ubyte), ("battery_flag", ctypes.c_ubyte),
                    ("battery_percent", ctypes.c_ubyte), ("saver", ctypes.c_ubyte),
                    ("life_s", wintypes.DWORD), ("full_life_s", wintypes.DWORD)]

    status = _Status()
    if not ctypes.WinDLL("kernel32").GetSystemPowerStatus(ctypes.byref(status)):
        return False
    return status.ac_line == 1


def tick(conn: sqlite3.Connection, *, keep_awake: bool,
         now: datetime | None = None) -> Slept | None:
    """One beat, and what follows from it: on a wake, every loop's pause
    ended and the computer held up for `HOLD_AFTER_WAKE`; and the computer
    held up while `keep_awake` is on and it runs on mains power."""
    now = now or datetime.now()
    slept = beat(conn, now)
    if slept is not None:
        _here.woke_at = now
        log.info("%s from %s to %s: catching up",
                 "asleep" if slept.how == "asleep" else "not running",
                 f"{slept.since:%a %H:%M}", f"{slept.until:%a %H:%M}")
        runtime.woke()
    _here.after_wake.hold(_here.woke_at is not None and now - _here.woke_at < HOLD_AFTER_WAKE)
    _here.kept.hold(keep_awake and on_mains())
    return slept


def let_go() -> None:
    """Release every power request: the heartbeat's duty is over."""
    _here.after_wake.hold(False)
    _here.kept.hold(False)


def status(conn: sqlite3.Connection) -> dict:
    """For the app: since when the backend has been awake, and the gap
    before that, as SQLite's UTC text. None for either before there is
    one."""
    row = conn.execute("SELECT since, gap_from, gap FROM awake ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {"since": None, "slept": None}
    slept = ({"from": row["gap_from"], "until": row["since"], "how": row["gap"]}
             if row["gap"] else None)
    return {"since": row["since"], "slept": slept}


def waited_line(conn: sqlite3.Connection, sent_at: datetime,
                now: datetime | None = None) -> str | None:
    """The line a reply begins with when the message it answers waited on
    the computer asleep, or on the backend not running: sent before the
    latest wake, read more than `WAITED` after it was sent, and within
    `SAID_WITHIN` of the wake. Once per wake. None otherwise.

    From code, not the model: it is a fact about the process, which the
    model cannot see."""
    now = now or datetime.now()
    try:
        row = conn.execute("SELECT since, gap FROM awake ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    if row is None or row["gap"] is None or _here.said_for == row["since"]:
        return None
    woke = _local(row["since"])
    if not (sent_at < woke and now - sent_at > WAITED and now - woke < SAID_WITHIN):
        return None
    _here.said_for = row["since"]
    if row["gap"] == "asleep":
        return f"Sorry for the wait: the computer was asleep until {woke:%H:%M}."
    return f"Sorry for the wait: I wasn't running until {woke:%H:%M}."
