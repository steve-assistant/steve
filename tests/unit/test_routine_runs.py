"""Routines run when the user said, report only what changed, and stop when done.

What a routine may do outward, and who may make one, are pinned in
tests/gate/test_routines.py. These pin the rest: the schedules, exactly-once
runs, what reaches them and when, a watch ending itself, the day's budget,
the brakes, and the wake alarm knowing about them.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from agent import routines, runtime
from agent.worker import background, handlers, links
from core import db, spend

# A Saturday morning, pinned: quiet hours and weekdays must not depend on
# when the suite runs.
SAT_10 = datetime(2026, 10, 3, 10, 0)
SAT_23 = datetime(2026, 10, 3, 23, 0)


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    yield c
    c.close()


@pytest.fixture
def told(monkeypatch):
    out: list[str] = []

    async def notify(text, **_):
        out.append(text)
        return len(out)

    monkeypatch.setattr(links, "notify_user", notify)
    return out


def _at(**kw):
    return routines.Schedule.build(now=SAT_10, **kw)


# --- schedules ------------------------------------------------------------------


@pytest.mark.parametrize("kw,first", [
    (dict(at_time="19:00"), datetime(2026, 10, 3, 19, 0)),
    (dict(at_time="07:30"), datetime(2026, 10, 4, 7, 30)),          # already past today
    (dict(at_time="19:00", days=["sun"]), datetime(2026, 10, 4, 19, 0)),
    (dict(at_time="08:30", days=["mon", "tue", "wed", "thu", "fri"]),
     datetime(2026, 10, 5, 8, 30)),
    (dict(every_minutes=120, condition="tickets on sale"), datetime(2026, 10, 3, 12, 0)),
])
def test_a_schedule_runs_next_when_the_user_said(kw, first):
    assert _at(**kw).next_after(SAT_10) == first


@pytest.mark.parametrize("kw,why", [
    (dict(every_minutes=10, condition="x"), "every 30 minutes"),
    (dict(every_minutes=60), "needs an end"),
    (dict(at_time="25:00"), "HH:MM"),
    (dict(at_time="07:00", every_minutes=60, until="2026-10-10T00:00"), "not both"),
    (dict(at_time="07:00", condition="x"), "interval watch"),
    (dict(at_time="07:00", until="2026-10-01T00:00"), "past"),
    (dict(at_time="07:00", days=["someday"]), "days"),
    (dict(), "time of day"),
])
def test_a_schedule_that_will_not_do_says_why(kw, why):
    with pytest.raises(routines.BadRoutine, match=why):
        _at(**kw)


def test_a_schedule_ends_when_the_user_said():
    schedule = _at(every_minutes=60, until="2026-10-03T11:30")
    assert schedule.next_after(SAT_10) == datetime(2026, 10, 3, 11, 0)
    assert schedule.next_after(datetime(2026, 10, 3, 11, 0)) is None


def test_a_schedule_says_itself_plainly():
    assert _at(at_time="08:30", days=["mon", "tue", "wed", "thu", "fri"]).says() == \
        "weekdays at 08:30"
    assert _at(every_minutes=180, condition="tickets on sale").says().startswith(
        "every 3 hours until tickets on sale")


# --- the record --------------------------------------------------------------------


def test_one_with_outward_targets_waits_for_its_card(conn):
    plain = routines.create(conn, title="Recap", instruction="Recap my week.",
                            schedule=_at(at_time="19:00", days=["sun"]))
    outward = routines.create(conn, title="Mom", instruction="Email mom.",
                              schedule=_at(at_time="19:00", days=["fri"]),
                              envelope={"send_email": {"targets": ["mom@example.com"]}})
    assert plain.status == "active" and outward.status == "awaiting_approval"
    with pytest.raises(routines.BadRoutine, match="cannot be done"):
        routines.create(conn, title="x", instruction="y", schedule=_at(at_time="07:00"),
                        envelope={"purchase": {"targets": ["mom@example.com"]}})


def test_its_card_starts_it_only_if_the_targets_are_unchanged(conn):
    from core import monitor

    made = routines.create(conn, title="Mom", instruction="Email mom.",
                           schedule=_at(at_time="19:00"),
                           envelope={"send_email": {"targets": ["mom@example.com"]}})
    assert not routines.activate(conn, made.id, "0" * 64)
    assert routines.activate(conn, made.id, monitor.holder_sha(conn, "routine", made.id))
    assert not routines.activate(conn, made.id, monitor.holder_sha(conn, "routine", made.id))


def test_there_is_a_ceiling_on_how_many(conn, monkeypatch):
    monkeypatch.setattr(routines, "MAX_ACTIVE", 2)
    for i in range(2):
        routines.create(conn, title=f"r{i}", instruction="x", schedule=_at(at_time="07:00"))
    with pytest.raises(routines.BadRoutine, match="already 2"):
        routines.create(conn, title="r3", instruction="x", schedule=_at(at_time="07:00"))


def test_pause_resume_cancel_and_stop_all(conn):
    a = routines.create(conn, title="a", instruction="x", schedule=_at(at_time="07:00"))
    b = routines.create(conn, title="b", instruction="x", schedule=_at(at_time="08:00"))
    assert routines.pause(conn, a.id) and routines.get(conn, a.id).status == "paused"
    assert routines.pause(conn, a.id, resume=True, now=SAT_10)
    assert routines.pause_all(conn) == 2
    assert routines.cancel(conn, b.id) and routines.get(conn, b.id).status == "cancelled"
    assert [r.id for r in routines.listed(conn)] == [a.id]


# --- running ------------------------------------------------------------------------


def _due(conn, **kw) -> routines.Routine:
    made = routines.create(conn, title="Recap", instruction="Recap my week at "
                           "https://example.com/week.", schedule=_at(**kw), now=SAT_10)
    conn.execute("UPDATE routines SET next_run_at = ? WHERE id = ?",
                 ((SAT_10 - timedelta(minutes=1)).isoformat(timespec="seconds"), made.id))
    conn.commit()
    return routines.get(conn, made.id)


def test_a_due_routine_is_queued_once_however_many_look(conn, told):
    _due(conn, at_time="19:00")
    first = asyncio.run(background.routine_tick(SAT_10))
    again = asyncio.run(background.routine_tick(SAT_10))
    assert len(first) == 1 and again == []
    row = conn.execute("SELECT kind, prompt FROM jobs WHERE id = ?", (first[0],)).fetchone()
    assert row["kind"] == "routine"


class _Reply:
    def __init__(self, text):
        self.text, self.cost_usd = text, 0.001


@pytest.fixture
def run(monkeypatch):
    """`Conversation` replaced: what a run was built with, and what it says."""
    seen: dict = {}

    class Convo:
        def __init__(self, conn, **kw):
            seen["init"] = kw

        async def send(self, text, **kw):
            seen["send"] = kw
            return _Reply(seen["reply"])

        def end(self):
            pass

    monkeypatch.setattr(handlers, "Conversation", Convo)
    return seen


def _job(routine):
    class Job:
        prompt = str(routine.id)

    return Job()


def test_a_run_is_a_routine_run_with_its_tools_and_its_shown_urls(conn, told, run):
    routine = _due(conn, at_time="19:00")
    run["reply"] = "Three things due this week."
    out = json.loads(asyncio.run(handlers.handle_routine(_job(routine))))
    assert run["init"]["channel"] == "routine" and run["init"]["routine_id"] == routine.id
    assert run["send"]["allow"] == routines.toolset(routine)
    assert run["send"]["seen"] == {"https://example.com/week"}
    assert out["reported"] and told == ["Recap: Three things due this week."]


def test_nothing_new_and_the_same_again_say_nothing(conn, told, run):
    routine = _due(conn, at_time="19:00")
    run["reply"] = "NOTHING_NEW"
    asyncio.run(handlers.handle_routine(_job(routine)))
    run["reply"] = "Rain on Tuesday."
    asyncio.run(handlers.handle_routine(_job(routine)))
    asyncio.run(handlers.handle_routine(_job(routine)))
    assert told == ["Recap: Rain on Tuesday."]
    assert routines.get(conn, routine.id).runs == 3


def test_a_watch_ends_itself_when_its_condition_is_met(conn, told, run, monkeypatch):
    monkeypatch.setattr("core.call_policy.within_quiet_hours", lambda now=None: False)
    routine = _due(conn, every_minutes=60, condition="tickets on sale")
    run["reply"] = "CONDITION_MET: on sale now at the box office."
    asyncio.run(handlers.handle_routine(_job(routine)))
    assert routines.get(conn, routine.id).status == "ended"
    assert told and "on sale now" in told[0] and "Watch over" in told[0]


def test_a_report_goes_to_the_outbox_once_and_is_not_held(conn, told, run, monkeypatch):
    """A routine's report sent while Signal was down used to be gone; then it
    was held, and sent again by the routine loop until Signal took it. The
    outbox keeps trying it now (tests/unit/test_outbox.py), so a held copy
    would be a second message."""
    monkeypatch.setattr("core.call_policy.within_quiet_hours", lambda now=None: False)
    routine = _due(conn, at_time="19:00")
    run["reply"] = "Three things due this week."
    asyncio.run(handlers.handle_routine(_job(routine)))
    asyncio.run(background.routine_tick(SAT_10))

    assert told == ["Recap: Three things due this week."]
    assert routines.get(conn, routine.id).held is None


def test_a_watch_keeps_quiet_hours_and_is_told_after(conn, told, run, monkeypatch):
    quiet = {"now": True}
    monkeypatch.setattr("core.call_policy.within_quiet_hours",
                        lambda now=None: quiet["now"])
    routine = _due(conn, every_minutes=60, condition="tickets on sale")
    run["reply"] = "Presale announced for Friday."
    asyncio.run(handlers.handle_routine(_job(routine)))
    assert told == [] and routines.get(conn, routine.id).held
    quiet["now"] = False
    asyncio.run(background.routine_tick(SAT_10))
    assert told == ["Recap: Presale announced for Friday."]
    assert routines.get(conn, routine.id).held is None


def test_a_routine_at_a_time_the_user_chose_ignores_quiet_hours(conn, told, run, monkeypatch):
    monkeypatch.setattr("core.call_policy.within_quiet_hours", lambda now=None: True)
    routine = _due(conn, at_time="07:00")
    run["reply"] = "Your morning summary."
    asyncio.run(handlers.handle_routine(_job(routine)))
    assert told == ["Recap: Your morning summary."]


def test_an_urgent_watch_reaches_them_in_quiet_hours(conn):
    routine = _due(conn, every_minutes=60, condition="x")
    assert routines.keeps_quiet(routine, SAT_23)
    routine.urgent = True
    assert not routines.keeps_quiet(routine, SAT_23)


def test_the_days_budget_stops_runs_quietly_and_the_briefing_says_so_once(conn, told, run):
    """Held quietly, with a notice the next briefing turns into its line and
    marks told; the message it sent before came again after each restart."""
    from agent import caps

    spend.bill(conn, 1.0, model="m", unit=spend.Unit("background", "routines"))
    routine = _due(conn, at_time="07:00")
    run["reply"] = "should not run"
    with spend.spending("background", part="routines", conn=conn):
        for _ in range(2):
            out = json.loads(asyncio.run(handlers.handle_routine(_job(routine))))
            assert out["ran"] is False
    assert told == [] and "send" not in run
    notices = caps.for_the_briefing(conn)
    [line] = caps.lines(notices)
    assert line.startswith("Routines paused at ") and "$1.00 daily limit was reached" in line
    spend.told(conn, notices)
    assert caps.for_the_briefing(conn) == []


def test_a_paused_routine_does_not_run(conn, told, run):
    routine = _due(conn, at_time="07:00")
    routines.pause(conn, routine.id)
    out = json.loads(asyncio.run(handlers.handle_routine(_job(routine))))
    assert out["ran"] is False and "send" not in run


def test_only_routine_runs_count_against_their_budget(conn):
    """A busy day of talking, or of goals, must not hold the routines."""
    for category, part, cost in (("background", "routines", 0.2), ("chat", None, 3.0),
                                 ("background", "goals", 0.4)):
        with spend.spending(category, part=part, conn=conn):
            spend.bill(conn, cost, model="m")
    conn.commit()
    assert spend.spent(conn, spend.windows().day.since, part="routines") == pytest.approx(0.2)
    with spend.spending("background", part="routines", conn=conn):
        assert spend.may_spend(conn) is None


def test_the_wake_alarm_knows_the_next_run(conn):
    from agent import wake

    routines.create(conn, title="Recap", instruction="x", schedule=_at(at_time="19:00"),
                    now=SAT_10)
    when, why = wake.next_due(conn, now=SAT_10)
    assert (when, why) == (datetime(2026, 10, 3, 19, 0), "routine")


@pytest.mark.parametrize("text,match", [
    ("STOP ROUTINES", True), ("stop routines.", True), ("Stop all routines", True),
    ("stop routine", True), ("please stop routines", False), ("stop the routine for mom", False),
])
def test_stop_routines_is_a_phrase_the_user_can_send_from_memory(text, match):
    from agent.worker.answering import STOP_ROUTINES_RE

    assert bool(STOP_ROUTINES_RE.match(text)) is match


def test_a_reports_citation_markers_never_reach_their_phone():
    """The first live run, 4 October 2026, ended in one after its web search."""
    routine = routines.Routine(1, "Weather", "x", routines.Schedule("at", "07:00"))
    live = ("Pittsburgh is overcast and 62F; showers are likely this afternoon. "
            "\ue200cite\ue202turn0search0\ue201")
    assert routines.understood(routine, live) == (
        "Pittsburgh is overcast and 62F; showers are likely this afternoon.", False)


def test_a_held_report_survives_a_later_run_with_nothing_new(conn, told, run, monkeypatch):
    """A watch's 11pm news, held for the morning, was erased by its midnight
    run saying NOTHING_NEW -- found 4 October 2026."""
    quiet = {"now": True}
    monkeypatch.setattr("core.call_policy.within_quiet_hours", lambda now=None: quiet["now"])
    routine = _due(conn, every_minutes=60, condition="tickets on sale")
    run["reply"] = "Presale announced for Friday."
    asyncio.run(handlers.handle_routine(_job(routine)))
    run["reply"] = "NOTHING_NEW"
    asyncio.run(handlers.handle_routine(_job(routine)))
    assert routines.get(conn, routine.id).held == "Presale announced for Friday."
    quiet["now"] = False
    asyncio.run(background.routine_tick(SAT_10))
    assert told == ["Recap: Presale announced for Friday."]
