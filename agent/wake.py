"""Waking the laptop for the things it promised to do.

Every scheduled thing in this agent has had the same hole in it: the
reminder loop, the 3am window and the morning digest all wait on
`asyncio.sleep`, and a sleeping laptop does not run asyncio. So "remind me at
5pm" fired whenever the lid next opened, which for an alarm is the same as
not firing.

Windows will wake a machine for a scheduled task, and that is what this
arranges: one task, rewritten whenever the next due moment moves, set to
`WakeToRun`. The task itself runs nothing (`pythonw -c pass`). The point is
not what runs -- it is that the machine is awake, so the loops already
running can take their next tick. Until 13 October 2026 it pinged the
supervisor's `/health`, on a fixed port, to leave a line in the worker log;
the port is chosen at each start now and every request needs a token
(core/localapi.py). The backend's heartbeat notices the wake itself
(agent/awake.py), ends every loop's pause, and holds the machine up while
what came due goes.

Run through `pythonw.exe` rather than `cmd`, because a console window
flashing on the screen at three in the morning is its own kind of failure.

Three facts about this laptop shaped the design, all measured rather than
assumed:

  - It uses **S0 Modern Standby**, not S3. The machine is never fully off and
    stays network-connected, which is why Signal sometimes survived a closed
    lid and sometimes did not.
  - **Wake timers are enabled on AC and disabled on battery** (`RTCWAKE` DC
    index 0). On battery this feature does nothing, and no amount of code
    here changes that -- it is a power setting and it is the user's to make.
  - A task can be registered **without elevation**, as long as it runs as the
    current user. Anything needing admin would have made this unshippable.

Scheduled rather than an in-process waitable timer because a timer dies with
the process and does not survive hibernation, and the whole point is to be
reliable at four in the morning.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from core import identity, modules

log = logging.getLogger("wake")


def task_name() -> str:
    """This install's task (core/identity.wake_task)."""
    from core.sandbox import profiles

    return identity.wake_task(profiles.install())


def _quoted(text: str) -> str:
    """`text` as a PowerShell string in single quotes, which take a quote
    only doubled: a profile folder like C:\\Users\\O'Brien ended the
    program's path early, and the task was never written."""
    return "'" + text.replace("'", "''") + "'"

# Wake this long before the thing is due. Coming up from Modern Standby and
# getting a Python event loop scheduled again is not instant, and a reminder
# that lands a minute late reads as broken in a way that one arriving a few
# seconds early does not.
LEAD = timedelta(seconds=90)

# Do not bother waking for something further out than this. The schedule is
# rewritten every couple of minutes anyway, so a distant task would only be
# replaced long before it fired -- and a wake timer sitting days out is a
# thing to explain rather than a thing that helps.
HORIZON = timedelta(hours=26)

# Never wake for something already overdue: the machine is plainly awake if
# this code is running, and a trigger in the past fires immediately and
# repeatedly.
MIN_LEAD = timedelta(seconds=30)


def _pythonw() -> str:
    """The task's Python: windowless, and the running backend's own, so it
    does not depend on what is on PATH for a task running out of a scheduler
    session. It was the checkout's `.venv`, which an installed app does not
    have; beside the interpreter that runs the backend, it is the venv's in a
    source tree and the bundled runtime's when installed."""
    return str(Path(sys.executable).with_name("pythonw.exe"))


# Its whole action: a Python that starts and exits. Waking is the point.
# Isolated, like every Python Steve starts (core/worker_handle.py).
_ACTION = "-I -c pass"


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def next_due(
    conn: sqlite3.Connection, *, now: datetime | None = None, digest_hour: int = 8
) -> tuple[datetime, str] | None:
    """The next moment the agent has something to do, and what it is.

    Deliberately only the things that are *late if missed*. The watcher
    sweeps every few hours and is explicitly the unsolicited one -- waking a
    laptop at 2am to check whether any mail arrived is exactly the
    intrusiveness this agent is supposed to avoid.
    """
    from agent import night

    now = now or datetime.now()
    candidates: list[tuple[datetime, str]] = []

    soonest = conn.execute(
        "SELECT next_fire_at FROM reminders WHERE status = 'active'"
        " ORDER BY next_fire_at LIMIT 1"
    ).fetchone()
    if soonest and soonest["next_fire_at"]:
        try:
            candidates.append(
                (datetime.fromisoformat(soonest["next_fire_at"]), "reminder")
            )
        except ValueError:
            log.warning("unparseable reminder time: %r", soonest["next_fire_at"])

    # A routine the user set for a time is late if missed, like a reminder
    # (agent/routines.py).
    from agent import routines

    with contextlib.suppress(ValueError, sqlite3.Error):
        if (routine_at := routines.next_run(conn)) is not None:
            candidates.append((routine_at, "routine"))
    # And one an enabled module set, which the user approved with it.
    from agent import modroutines

    with contextlib.suppress(ValueError, sqlite3.Error):
        if (routine_at := modroutines.next_run(conn)) is not None:
            candidates.append((routine_at, "module routine"))

    candidates.append(
        (now + timedelta(seconds=night.seconds_until_next_window(now)), "night window")
    )

    digest = now.replace(hour=digest_hour, minute=0, second=0, microsecond=0)
    if digest <= now:
        digest += timedelta(days=1)
    candidates.append((digest, "morning digest"))

    from agent.attention import settings as attention

    evening = now.replace(
        hour=attention.EVENING.hour, minute=attention.EVENING.minute, second=0, microsecond=0
    )
    if evening <= now:
        evening += timedelta(days=1)
    candidates.append((evening, "evening briefing"))

    # A brief before a meeting is late if missed, like a reminder: it is the
    # one unprompted message that is useless ten minutes after its time.
    # `deliver_at` is SQLite UTC; the comparison here is local and naive.
    brief = conn.execute(
        "SELECT datetime(MIN(deliver_at), 'localtime') AS at FROM observations"
        " WHERE say = 'before' AND delivered_at IS NULL AND deliver_at IS NOT NULL"
        f" AND {modules.live(conn, 'observations')}"  # as `store.before_due` reads them
    ).fetchone()
    if brief and brief["at"]:
        candidates.append((datetime.fromisoformat(brief["at"]), "meeting brief"))

    upcoming = [
        (when, why)
        for when, why in candidates
        if now + MIN_LEAD < when <= now + HORIZON
    ]
    return min(upcoming) if upcoming else None


def _register(when: datetime) -> bool:
    """Write the task. Returns whether Windows accepted it."""
    shell = _powershell()
    if not shell:
        return False

    # `-StartWhenAvailable` so a wake that does not happen -- on battery, or
    # with the machine off -- still runs the moment it next can, rather than
    # being silently skipped.
    script = (
        "$ErrorActionPreference='Stop';"
        f"$a=New-ScheduledTaskAction -Execute {_quoted(_pythonw())} -Argument '{_ACTION}';"
        f"$t=New-ScheduledTaskTrigger -Once -At ([datetime]'{when.isoformat()}');"
        "$s=New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -StartWhenAvailable;"
        f"Register-ScheduledTask -TaskName '{task_name()}' -Action $a -Trigger $t"
        " -Settings $s -Force | Out-Null"
    )
    try:
        subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not arm the wake task: %s", exc)
        return False
    return True


def clear() -> bool:
    shell = _powershell()
    if not shell:
        return False
    with_suppressed = (
        f"Unregister-ScheduledTask -TaskName '{task_name()}' -Confirm:$false "
        "-ErrorAction SilentlyContinue"
    )
    try:
        subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", with_suppressed],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


# The moment currently armed, so a re-arm that changes nothing costs nothing.
# Without it this would spawn a PowerShell every couple of minutes forever.
_armed: datetime | None = None


def arm(
    conn: sqlite3.Connection, *, now: datetime | None = None, digest_hour: int = 8
) -> dict:
    """Point the wake task at whatever is due next.

    `digest_hour` is passed in rather than read here, so there is one place
    that decides when breakfast is. Two copies of that number would diverge
    the first time it was tuned, and the symptom would be a laptop waking at
    the old hour for a digest that arrives at the new one.
    """
    global _armed

    found = next_due(conn, now=now, digest_hour=digest_hour)
    if found is None:
        if _armed is not None:
            clear()
            _armed = None
        return {"armed": None, "why": None}

    when, why = found
    target = when - LEAD
    if _armed is not None and abs((target - _armed).total_seconds()) < 30:
        return {"armed": _armed.isoformat(timespec="seconds"), "why": why,
                "unchanged": True}

    if not _register(target):
        return {"armed": None, "why": why, "error": "could not register the task"}

    _armed = target
    log.info("armed a wake for %s (%s)", target, why)
    return {"armed": target.isoformat(timespec="seconds"), "why": why}


def disarm() -> bool:
    """Take the task down: the user has the wake timer off. Whether this
    process armed it or an earlier one did, so it is removed whatever is
    remembered."""
    global _armed

    _armed = None
    return clear()


# ---------------------------------------------------------------------------
# Staying awake for work that takes a while
# ---------------------------------------------------------------------------

# PowerSetRequest's request types. SystemRequired alone is documented as not
# honoured on Modern Standby machines, which this one is; ExecutionRequired
# is the one that is. Both, so the request means the same on either kind.
_SYSTEM_REQUIRED, _EXECUTION_REQUIRED = 1, 3


def _power_request(why: str):
    """A power request held until `_release`, or None where there is none."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _Detailed(ctypes.Structure):
        _fields_ = [("module", wintypes.HMODULE), ("id", wintypes.ULONG),
                    ("count", wintypes.ULONG), ("strings", ctypes.c_void_p)]

    class _Reason(ctypes.Union):
        _fields_ = [("simple", wintypes.LPWSTR), ("detailed", _Detailed)]

    class _Context(ctypes.Structure):
        _fields_ = [("version", wintypes.ULONG), ("flags", wintypes.DWORD),
                    ("reason", _Reason)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PowerCreateRequest.restype = wintypes.HANDLE
    kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_Context)]
    kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    kernel32.PowerSetRequest.restype = wintypes.BOOL
    text = ctypes.create_unicode_buffer(why[:100])
    context = _Context(0, 1)  # POWER_REQUEST_CONTEXT_VERSION, _SIMPLE_STRING
    context.reason.simple = ctypes.cast(text, wintypes.LPWSTR)
    handle = kernel32.PowerCreateRequest(ctypes.byref(context))
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    held = [kind for kind in (_SYSTEM_REQUIRED, _EXECUTION_REQUIRED)
            if kernel32.PowerSetRequest(handle, kind)]
    return (kernel32, handle, held, text)


def _release(request) -> None:
    kernel32, handle, held, _text = request
    for kind in held:
        kernel32.PowerClearRequest(handle, kind)
    kernel32.CloseHandle(handle)


class kept_awake:
    """Hold the laptop awake while a long piece of work runs.

    The wake task brings the machine up for the 3am window; nothing kept it
    up. On 28 September a pipeline attempt's frozen gate was two hours into
    the suite when the laptop went to sleep, and the step failed on its
    clock. A power request is what Windows asks of a process that is busy.
    It cannot overrule a closed lid or a power button, and on battery the
    wake timers are off anyway (see the module docstring).
    """

    def __init__(self, why: str):
        self.why, self.request = why, None

    def __enter__(self) -> bool:
        try:
            self.request = _power_request(self.why)
        except Exception:  # noqa: BLE001 - staying awake is worth trying, never a crash
            log.warning("could not ask Windows to stay awake", exc_info=True)
        return self.request is not None

    def __exit__(self, *exc) -> bool:
        if self.request is not None:
            try:
                _release(self.request)
            except Exception:  # noqa: BLE001
                log.warning("could not release the power request", exc_info=True)
        return False
