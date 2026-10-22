"""Waking the laptop for what it promised to do.

The hole this closes is the oldest one in the scheduler: reminders, the 3am
window and the morning digest all wait on `asyncio.sleep`, and a sleeping
laptop runs no asyncio. "Remind me at 5pm" fired whenever the lid next
opened, which for an alarm is indistinguishable from not firing at all.

Two failure directions, and they are not symmetric. Missing a wake is the
old behaviour and merely disappointing. Waking a laptop it did not need to
wake — at two in the morning, for a mail sweep nobody asked for — is the
intrusiveness this agent is built to avoid, and it is how the whole feature
gets turned off. So most of what follows is about what does *not* deserve a
wake.

No task is registered here. `_register` shells out to Windows, and a unit
test that scheduled real wake timers on the machine running it would be a
worse idea than the bug.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent import wake

# Captured before the autouse fixture below replaces it, so the one test
# that cares about the real thing can still reach it.
_REAL_REGISTER = wake._register


@pytest.fixture(autouse=True)
def never_touch_windows(monkeypatch):
    """Record what would have been armed instead of arming it."""
    armed: list[datetime] = []
    monkeypatch.setattr(wake, "_register", lambda when: armed.append(when) or True)
    monkeypatch.setattr(wake, "clear", lambda: True)
    monkeypatch.setattr(wake, "_armed", None)
    return armed


def _reminder(store, when: datetime, status: str = "active") -> None:
    store.execute(
        "INSERT INTO reminders (kind, text, next_fire_at, max_fires, status)"
        " VALUES ('remind', 'grab groceries', ?, 1, ?)",
        (when.isoformat(timespec="seconds"), status),
    )
    store.commit()


NOON = datetime(2026, 9, 21, 12, 0, 0)


# --------------------------------------------------------------------------
# What deserves a wake
# --------------------------------------------------------------------------


def test_a_reminder_is_worth_waking_for(store):
    """The case the user asked about. An alarm that waits for the lid is not an
    alarm."""
    _reminder(store, NOON + timedelta(hours=2))

    when, why = wake.next_due(store, now=NOON)

    assert why == "reminder"
    assert when == NOON + timedelta(hours=2)


def test_the_soonest_thing_wins(store):
    """One alarm, repointed. The night window is always out there, so a
    reminder before it has to take precedence or nothing ever does."""
    _reminder(store, NOON + timedelta(hours=1))

    _when, why = wake.next_due(store, now=NOON)

    assert why == "reminder"


def test_the_night_window_is_worth_waking_for(store):
    """It reads the day, and the day is over. Three in the morning is
    exactly when the lid is shut."""
    _when, why = wake.next_due(store, now=NOON.replace(hour=22))

    assert why == "night window"


def test_the_evening_look_ahead_is_worth_waking_for(store):
    """Nine in the evening is when tomorrow is still changeable."""
    when, why = wake.next_due(store, now=NOON)

    assert why == "evening briefing" and when.hour == 21


def test_a_brief_before_a_meeting_is_worth_waking_for(store):
    """Useless ten minutes after its time, like a reminder."""
    brief = (NOON + timedelta(hours=2)).astimezone().astimezone(UTC)
    with store:
        store.execute(
            "INSERT INTO observations (source, key, title, say, triaged_at, deliver_at)"
            " VALUES ('horizon', 'horizon:x:soon', 'Northwind chat', 'before', datetime('now'), ?)",
            (brief.strftime("%Y-%m-%d %H:%M:%S"),),
        )

    when, why = wake.next_due(store, now=NOON)

    assert why == "meeting brief" and when == NOON + timedelta(hours=2)


def test_the_digest_beats_the_night_window_once_the_night_has_passed(store):
    """At 4am the next window is a day away and breakfast is four hours
    off."""
    _when, why = wake.next_due(store, now=datetime(2026, 9, 21, 4, 0))

    assert why == "morning digest"


# --------------------------------------------------------------------------
# What does not
# --------------------------------------------------------------------------


def test_a_cancelled_reminder_does_not_wake_anything(store):
    _reminder(store, NOON + timedelta(minutes=30), status="cancelled")

    _when, why = wake.next_due(store, now=NOON)

    assert why != "reminder"


def test_an_overdue_reminder_does_not_arm_a_wake_in_the_past(store):
    """A trigger behind the clock fires at once and keeps firing. And the
    machine is plainly awake if this code is running at all."""
    _reminder(store, NOON - timedelta(hours=3))

    _when, why = wake.next_due(store, now=NOON)

    assert why != "reminder"


def test_something_far_out_is_left_alone(store):
    """A wake timer sitting days out is a thing to explain rather than a
    thing that helps, and the alarm is rewritten every couple of minutes
    anyway."""
    _reminder(store, NOON + timedelta(days=30))

    _when, why = wake.next_due(store, now=NOON)

    assert why != "reminder"


def test_watching_for_news_never_earns_a_wake(store):
    """The attention loop is the explicitly unsolicited one. Waking a laptop
    at 2am to see whether any mail arrived is the intrusiveness this agent
    exists not to have -- and the fastest way to get the feature switched
    off. It catches up when the lid next opens."""
    reasons = {wake.next_due(store, now=NOON + timedelta(hours=h))[1] for h in range(24)}

    assert "watch" not in reasons
    assert reasons <= {"night window", "morning digest", "evening briefing"}


# --------------------------------------------------------------------------
# Arming
# --------------------------------------------------------------------------


def test_it_wakes_a_little_early(store, never_touch_windows):
    """Coming up from Modern Standby and getting an event loop scheduled is
    not instant. Early is invisible; late reads as broken."""
    _reminder(store, NOON + timedelta(hours=1))

    wake.arm(store, now=NOON)

    assert never_touch_windows == [NOON + timedelta(hours=1) - wake.LEAD]


def test_re_arming_an_unchanged_schedule_costs_nothing(store, never_touch_windows):
    """This runs every two minutes forever. Spawning a PowerShell each time
    to write the same value would be a background process doing nothing at
    a steady rate."""
    _reminder(store, NOON + timedelta(hours=1))

    wake.arm(store, now=NOON)
    second = wake.arm(store, now=NOON + timedelta(seconds=30))

    assert second["unchanged"] is True
    assert len(never_touch_windows) == 1


def test_a_nearer_reminder_moves_the_alarm(store, never_touch_windows):
    _reminder(store, NOON + timedelta(hours=5))
    wake.arm(store, now=NOON)

    _reminder(store, NOON + timedelta(hours=1))
    wake.arm(store, now=NOON)

    assert len(never_touch_windows) == 2
    assert never_touch_windows[-1] == NOON + timedelta(hours=1) - wake.LEAD


def test_a_machine_without_powershell_degrades_quietly(store, monkeypatch):
    """Nothing here is load-bearing for answering them. On a box with no
    scheduler this should do nothing rather than raise into the loop."""
    monkeypatch.setattr(wake, "_register", _REAL_REGISTER)
    monkeypatch.setattr(wake, "_powershell", lambda: None)
    monkeypatch.setattr(wake, "_armed", None)

    assert wake._register(NOON) is False
    assert wake.arm(store, now=NOON)["error"]


def test_the_wake_task_runs_the_windowless_python_beside_the_one_running_the_backend(
        monkeypatch):
    """It was the checkout's `.venv`, found from this file: an installed app
    has none, so its wake task would have run a program that does not
    exist, and Windows would not have woken the computer for it."""
    import subprocess

    ran: list[list[str]] = []
    monkeypatch.setattr(wake, "_register", _REAL_REGISTER)
    monkeypatch.setattr(wake, "_powershell", lambda: "powershell")
    monkeypatch.setattr(wake.subprocess, "run", lambda args, **kw: ran.append(args)
                        or subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr(wake.sys, "executable", r"C:\Programs\Steve\runtime\python.exe")

    assert wake._register(NOON)
    [[*_, script]] = ran
    assert r"-Execute 'C:\Programs\Steve\runtime\pythonw.exe' -Argument '-I -c pass'" in script


def test_a_profile_folder_with_a_quote_in_it_still_gets_this_installs_wake_task(monkeypatch):
    """PowerShell's single quotes take a quote only doubled: C:\\Users\\O'Brien
    ended the program's path early, and the task was never written. The
    task is this install's (core/identity.wake_task): one name for every
    install let a second take the first's."""
    import subprocess

    from core import identity
    from core.sandbox import profiles

    ran: list[list[str]] = []
    monkeypatch.setattr(wake, "_register", _REAL_REGISTER)
    monkeypatch.setattr(wake, "_powershell", lambda: "powershell")
    monkeypatch.setattr(wake.subprocess, "run", lambda args, **kw: ran.append(args)
                        or subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr(wake.sys, "executable", r"C:\Users\O'Brien\Steve\runtime\python.exe")

    assert wake._register(NOON)
    [[*_, script]] = ran
    assert r"-Execute 'C:\Users\O''Brien\Steve\runtime\pythonw.exe' -Argument" in script
    assert f"-TaskName '{identity.wake_task(profiles.install())}'" in script


def test_the_digest_hour_comes_from_the_caller(store, never_touch_windows):
    """One place decides when breakfast is. Two copies would diverge the
    first time it was tuned, and the symptom is a laptop waking at the old
    hour for a digest that arrives at the new one."""
    early = wake.next_due(store, now=datetime(2026, 9, 21, 4, 0), digest_hour=6)
    late = wake.next_due(store, now=datetime(2026, 9, 21, 4, 0), digest_hour=9)

    assert early[0].hour == 6
    assert late[0].hour == 9


async def test_the_wake_timer_is_armed_only_once_the_user_turns_it_on(monkeypatch):
    """A fresh install put a task in their Task Scheduler that wakes the
    computer, unasked. Off, the task is taken down once, whoever armed it."""
    from agent.worker import background
    from core import knobs

    done = []
    monkeypatch.setattr(wake, "arm", lambda conn, **kw: done.append("arm") or {})
    monkeypatch.setattr(wake, "disarm", lambda: done.append("disarm") or True)

    async def three_rounds(what, step, every_s):
        for _ in range(3):
            await step()

    monkeypatch.setattr(background, "_forever", three_rounds)

    await background.wake_loop()
    assert done == ["disarm"]

    done.clear()
    knobs.write({"WAKE_TIMER": "on"})
    await background.wake_loop()
    assert done == ["arm", "arm", "arm"]
