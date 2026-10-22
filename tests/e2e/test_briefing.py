"""The morning briefing reaches the phone and the day, once.

The briefing goes at the hour the user chose (the knob `MORNING`), outside
quiet hours, once a day. A scenario can't wait for eight o'clock, so it
moves the hour to the minute just gone, and the quiet hours away from now,
through the knobs the app sets them by; the briefing's loop, which looks
every five minutes, looks every second in a world (`BRISK`). On a fresh install
there is little to brief: what is checked is that the one message is
sent, shown on the day, and not sent again.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from agent.attention import briefing
from agent.worker import control
from core import call_policy
from tests.e2e.backend import Backend, morning_now, running, until
from tests.fakes.telegram import USER
from tests.fakes.world import world


@pytest.fixture(scope="module")
def steve(fenced):
    data = fenced / "data"
    (data / "telegram").mkdir(parents=True)
    (data / "telegram" / "state.json").write_text(
        json.dumps({"chat_id": USER, "offset": 0}), encoding="utf-8", newline="\n")
    with (world(fenced / "world", telegram=True) as made,
          running(made, data) as backend):
        assert until(lambda: bool(made.telegram.called("getUpdates")), within=60), backend.logs()
        yield backend


@pytest.mark.parametrize("hour", range(24))
def test_the_scenarios_make_the_morning_due_and_no_other_at_every_hour_of_the_day(hour):
    """The scenarios move the briefing's knobs to the clock they run at, and
    the knobs refuse a briefing inside quiet hours: one placing that
    passed by morning and failed every afternoon took six scenarios down
    with it, at whatever hour the suite happened to run."""
    for minute in (0, 1, 29, 31, 59):
        now = datetime(2026, 10, 19, hour, minute, 30)
        moved = morning_now(now)
        whole = control.defaults() | moved
        control._briefings_outside_quiet_hours(whole)
        quiet = [datetime.strptime(whole[k], "%H:%M").time() for k in ("QUIET_FROM", "QUIET_UNTIL")]
        assert not call_policy.is_quiet(now.time(), *quiet), (now, moved)
        for name, due in (("MORNING", True), ("EVENING", False)):
            at = datetime.combine(now.date(), datetime.strptime(whole[name], "%H:%M").time())
            last = at if at <= now else at - timedelta(days=1)
            assert (last <= now < last + briefing.LATE_BY) is due, (name, now, moved)


def briefings(steve: Backend) -> list[dict]:
    return [e for e in steve.get("/control/day").json()["entries"] if e["kind"] == "briefing"]


def test_the_morning_briefing_reaches_the_phone_and_the_day_once(steve):
    sent_before = len(steve.world.telegram.sent(USER))

    moved = steve.post("/control/knobs", {"changes": morning_now(datetime.now())})

    assert moved.json()["ok"], moved.text
    assert until(lambda: briefings(steve)), steve.logs()
    assert until(lambda: len(steve.world.telegram.sent(USER)) > sent_before), steve.logs()
    # Once: the loop looks every second, and finds it sent.
    assert not until(lambda: len(briefings(steve)) > 1, within=4)
    assert len(steve.world.telegram.sent(USER)) == sent_before + 1
