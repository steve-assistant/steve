"""Asleep and awake: the heartbeat, and catching up when the computer wakes.

Nothing of Steve's runs while the computer sleeps. Before step 4 nothing
noticed afterwards either: the loops paused on the event loop's clock, so a
reminder due at six could wait out a loop's period after the lid opened, the
night's suites could start beside the user's morning, and a reply that had
waited all night said nothing of why. Every test here pins the clock: asleep
is the clock jumping with no loop run in between, and the wake is the jump
being noticed.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import datetime, timedelta

import pytest

from agent import awake, night, runtime, sqltime, wake
from agent.worker import background
from core import db


def T(text: str) -> datetime:
    return datetime.fromisoformat(text)


class _Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An install from before every night these tests name: a database made
    today would owe none of them (`night.take_up`)."""
    conn = db.open_store(tmp_path / "awake.db")
    with conn:
        conn.execute("UPDATE schema_version SET applied_at = '2026-01-01 00:00:00'")
    monkeypatch.setattr(runtime, "conn", conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def held(monkeypatch):
    """A process of its own for each test, on mains power, and no real power
    request: a test that took one would keep the computer running it awake.
    Returns the requests held, by why."""
    held: list[str] = []

    class Kept:
        def __init__(self, why: str):
            self.why = why

        def __enter__(self) -> bool:
            held.append(self.why)
            return True

        def __exit__(self, *exc) -> bool:
            held.remove(self.why)
            return False

    monkeypatch.setattr(wake, "kept_awake", Kept)
    monkeypatch.setattr(awake, "_here", awake._Here())
    monkeypatch.setattr(awake, "on_mains", lambda: True)
    return held


async def _rounds(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _until(done, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not done():
        assert asyncio.get_running_loop().time() < deadline, "it never happened"
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------
# The heartbeat
# --------------------------------------------------------------------------


def test_beats_within_three_minutes_of_each_other_are_one_stretch_awake(store):
    """A busy event loop or a worker swap holds a beat back by up to a
    minute or so, and must not read as the computer sleeping."""
    assert awake.beat(store, T("2026-10-13 21:00")) is None, "the first beat ever"
    assert awake.beat(store, T("2026-10-13 21:01")) is None
    assert awake.beat(store, T("2026-10-13 21:04")) is None

    rows = [tuple(r) for r in store.execute("SELECT since, last_beat, gap FROM awake")]
    assert rows == [(sqltime.stamp(T("2026-10-13 21:00")),
                     sqltime.stamp(T("2026-10-13 21:04")), None)]
    assert awake.status(store) == {"since": sqltime.stamp(T("2026-10-13 21:00")),
                                   "slept": None}


def test_a_beat_long_after_the_last_one_records_the_computer_asleep_in_between(store):
    """The app says when Steve slept, and the reply that waited on it can
    say so."""
    awake.beat(store, T("2026-10-13 22:10"))
    slept = awake.beat(store, T("2026-10-14 07:45"))

    assert slept == awake.Slept(T("2026-10-13 22:10"), T("2026-10-14 07:45"), "asleep")
    assert awake.status(store) == {
        "since": sqltime.stamp(T("2026-10-14 07:45")),
        "slept": {"from": sqltime.stamp(T("2026-10-13 22:10")),
                  "until": sqltime.stamp(T("2026-10-14 07:45")), "how": "asleep"}}


def test_a_gap_found_by_a_process_first_beat_is_the_backend_not_running(store, monkeypatch):
    """A process frozen with the machine beats again when it wakes; one that
    was not running at all beats for the first time. Saying "the computer was
    asleep" to someone who had quit Steve would be a small lie, in the one
    line meant to explain."""
    awake.beat(store, T("2026-10-13 22:10"))
    monkeypatch.setattr(awake, "_here", awake._Here())  # quit, and started in the morning

    assert awake.beat(store, T("2026-10-14 07:45")).how == "stopped"
    assert awake.status(store)["slept"]["how"] == "stopped"


def test_a_week_of_stretches_is_kept_and_no_more(store, monkeypatch):
    """One row each time the computer wakes, for as long as Steve is
    installed, would grow without end."""
    for day in range(1, 11):
        monkeypatch.setattr(awake, "_here", awake._Here())
        awake.beat(store, T(f"2026-10-{day:02d} 09:00"))

    first = store.execute("SELECT MIN(since) FROM awake").fetchone()[0]
    assert awake._local(first) == T("2026-10-03 09:00")


# --------------------------------------------------------------------------
# Waking: every pause ends
# --------------------------------------------------------------------------


async def test_a_wake_ends_every_nap_under_way_and_wakes_the_outbox(monkeypatch):
    """Each loop pauses for its own period, up to a day for the night's;
    after a night asleep, each looks at once at what came due. A nap begun
    after the wake is a new pause, and runs its time."""
    outbox = asyncio.Event()
    monkeypatch.setattr(runtime, "outbox_wake", outbox)
    naps = [asyncio.create_task(runtime.nap(3600)) for _ in range(2)]
    await _rounds(3)

    runtime.woke()

    assert await asyncio.wait_for(asyncio.gather(*naps), 1) == [True, True]
    assert outbox.is_set()
    assert await runtime.nap(0.01) is False
    assert not runtime._napping, "a finished nap is not kept"


def test_no_background_loop_pauses_on_the_event_loops_clock_alone():
    """A pause on `asyncio.sleep` cannot be ended by the wake: a loop that
    took one would wait out the rest of its period after a night asleep, as
    every loop did before step 4."""
    tree = ast.parse(inspect.getsource(background))
    sleeps = [node.lineno for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "sleep"
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "asyncio"]
    assert sleeps == []


async def test_a_reminder_due_while_asleep_and_the_morning_briefing_go_out_as_soon_as_it_wakes(
        store, monkeypatch):
    """Step 4's exit test for sleep. Both loops took their round before the
    night and began an hour's pause; the clock jumps to 08:30 with neither
    running, as it does for a computer asleep. Nothing goes until the
    heartbeat notices, and then both go at once, not when their pauses end."""
    from agent import reminders
    from agent.attention import briefing
    from agent.worker import links

    clock = _Clock(T("2026-10-13 22:30"))
    real_due, real_step = reminders.due, briefing.step
    monkeypatch.setattr(reminders, "due", lambda conn, now=None: real_due(conn, clock()))
    monkeypatch.setattr(briefing, "step", lambda conn: real_step(conn, now=clock()))
    monkeypatch.setattr(briefing, "seconds_until_next", lambda now: 3600.0)
    monkeypatch.setattr(reminders, "TICK_S", 3600)
    monkeypatch.setattr(background, "BRIEFING_CHECK_S", 3600)
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setattr(runtime, "outbox_wake", None)

    async def send(conn, kind, *, notify=None, now=None):
        """The briefing as it goes, without the model that writes it."""
        await links.notify_user(f"Your {kind} briefing.", kind="briefing")
        briefing._mark_sent(conn, kind, briefing.set_for(kind, now).date())
        return {"sent": True}

    monkeypatch.setattr(briefing, "send", send)
    reminders.schedule(store, kind="remind", text="Put the bins out",
                       when="2026-10-14T06:00:00", now=clock())

    def told() -> list[tuple[str, str]]:
        return sorted(tuple(r) for r in store.execute("SELECT kind, text FROM outbox"))

    awake.tick(store, keep_awake=False, now=clock())
    loops = [asyncio.create_task(background.reminder_loop()),
             asyncio.create_task(background.briefing_loop())]
    try:
        await _rounds()
        assert told() == [], "nothing was due at bedtime"

        clock.at = T("2026-10-14 08:30")
        await _rounds()
        assert told() == [], "asleep: no loop ran"

        awake.tick(store, keep_awake=False, now=clock())
        await _until(lambda: len(told()) == 2)
        assert told() == [("briefing", "Your morning briefing."),
                          ("reminder", "Put the bins out")]
    finally:
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)


# --------------------------------------------------------------------------
# The night's window
# --------------------------------------------------------------------------


def _window(on: str) -> tuple[datetime, timedelta]:
    """When the night's window opens on the night ending on day `on`, and
    how long after that a night may still be taken up."""
    opens, open_for = night._window()
    return datetime.combine(T(on).date(), opens), open_for


def _napping(monkeypatch, clock: _Clock, wakes: list[datetime]) -> None:
    """`runtime.nap` on the pinned clock: each nap ends at the next of
    `wakes`, the computer waking, or else when its time is up."""
    async def nap(seconds: float) -> bool:
        if wakes:
            clock.at = wakes.pop(0)
            return True
        clock.at += timedelta(seconds=seconds)
        return False

    monkeypatch.setattr(runtime, "nap", nap)


async def test_the_night_starts_when_its_window_opens(store, monkeypatch):
    opens, _ = _window("2026-10-14")
    clock = _Clock(opens - timedelta(hours=2))
    _napping(monkeypatch, clock, [])

    await background.until_the_night(now=clock)

    assert clock() == opens


async def test_a_night_slept_through_starts_on_waking_while_it_may_still_be_taken_up(
        store, monkeypatch):
    """Asleep at the opening, awake an hour or two later: the night is still
    worth having, and it is recorded as taken up, so a worker that keeps
    dying does not start it again each time."""
    opens, open_for = _window("2026-10-14")
    clock = _Clock(opens - timedelta(hours=2))
    _napping(monkeypatch, clock, [opens + open_for / 2])

    await background.until_the_night(now=clock)

    assert clock() == opens + open_for / 2
    assert store.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind = 'night_taken_up'").fetchone()[0] == 1


async def test_a_night_slept_through_until_morning_waits_for_the_next_night(store, monkeypatch):
    """A computer that slept through the opening woke to a wait that had
    ended, and the night's coding attempts and suites then ran beside the
    user's morning."""
    opens, open_for = _window("2026-10-14")
    clock = _Clock(opens - timedelta(hours=2))
    _napping(monkeypatch, clock, [opens + open_for + timedelta(hours=1)])

    await background.until_the_night(now=clock)

    assert clock() == opens + timedelta(days=1)


# --------------------------------------------------------------------------
# Holding the computer up
# --------------------------------------------------------------------------


def test_a_wake_holds_the_computer_up_for_five_minutes_then_lets_go(store, held):
    """Windows puts a machine its wake timer woke back to sleep after about
    two idle minutes, which can be before what came due has gone."""
    awake.tick(store, keep_awake=False, now=T("2026-10-13 22:10"))
    assert held == [], "the first beat ever is no wake"

    for minute in range(45, 50):
        awake.tick(store, keep_awake=False, now=T(f"2026-10-14 07:{minute}"))
        assert held == ["catching up after a wake"]
    awake.tick(store, keep_awake=False, now=T("2026-10-14 07:50"))
    assert held == []


def test_keep_awake_holds_the_computer_up_on_mains_power_and_only_then(store, held, monkeypatch):
    """Off by default, and on battery it lets go: keeping a laptop on its
    battery awake all night would empty it."""
    mains = [True]
    monkeypatch.setattr(awake, "on_mains", lambda: mains[0])

    awake.tick(store, keep_awake=True, now=T("2026-10-13 10:00"))
    assert held == ["kept awake while plugged in"]
    mains[0] = False
    awake.tick(store, keep_awake=True, now=T("2026-10-13 10:01"))
    assert held == []
    mains[0] = True
    awake.tick(store, keep_awake=False, now=T("2026-10-13 10:02"))
    assert held == [], "the switch off"


async def test_the_heartbeat_lets_go_of_the_computer_when_its_duty_ends(store, held):
    """A worker handing its duties over must not leave the computer held
    awake by a loop that no longer runs."""
    from core import knobs

    knobs.write({"KEEP_AWAKE": "on"})
    beating = asyncio.create_task(background.heartbeat())
    await _until(lambda: held == ["kept awake while plugged in"])

    beating.cancel()
    await asyncio.gather(beating, return_exceptions=True)

    assert held == []


# --------------------------------------------------------------------------
# "Sorry for the wait"
# --------------------------------------------------------------------------


def _slept(store) -> None:
    awake.beat(store, T("2026-10-13 22:10"))
    awake.beat(store, T("2026-10-14 07:45"))


def test_a_reply_to_a_message_that_waited_on_sleep_says_so_once_per_wake(store):
    _slept(store)

    assert awake.waited_line(store, T("2026-10-13 23:00"), now=T("2026-10-14 07:46")) == \
        "Sorry for the wait: the computer was asleep until 07:45."
    assert awake.waited_line(store, T("2026-10-14 01:00"), now=T("2026-10-14 07:47")) is None, \
        "a second message that waited: once is enough"


def test_a_message_that_did_not_wait_on_sleep_gets_no_line(store):
    """An apology for a wait that did not happen, or that something else
    caused, would be noise, or wrong."""
    _slept(store)

    assert awake.waited_line(store, T("2026-10-14 07:44"), now=T("2026-10-14 07:46")) is None, \
        "read two minutes after it was sent"
    assert awake.waited_line(store, T("2026-10-14 07:50"), now=T("2026-10-14 08:05")) is None, \
        "sent after the wake"
    assert awake.waited_line(store, T("2026-10-13 23:00"), now=T("2026-10-14 08:30")) is None, \
        "read long after the wake: something else held it"
    assert awake.waited_line(store, T("2026-10-13 23:00"), now=T("2026-10-14 07:46")), \
        "and the line is still there for one that did wait"


def test_a_reply_that_waited_on_steve_not_running_says_that_and_not_sleep(store, monkeypatch):
    awake.beat(store, T("2026-10-13 22:10"))
    monkeypatch.setattr(awake, "_here", awake._Here())
    awake.beat(store, T("2026-10-14 07:45"))

    assert awake.waited_line(store, T("2026-10-13 23:00"), now=T("2026-10-14 07:46")) == \
        "Sorry for the wait: I wasn't running until 07:45."


def test_before_any_wake_there_is_no_line(store):
    assert awake.waited_line(store, T("2026-10-13 23:00"), now=T("2026-10-14 07:46")) is None
    awake.beat(store, T("2026-10-14 07:45"))
    assert awake.waited_line(store, T("2026-10-13 23:00"), now=T("2026-10-14 07:46")) is None, \
        "the first beat ever ends no gap"
