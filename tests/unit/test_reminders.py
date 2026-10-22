"""Reminders, and checks that repeat.

The interesting half is the repeating one. Running a question every ten
minutes and reporting every answer is how you teach someone to ignore you:
six messages saying 14-7 bury the one saying 21-7. So most of these are about
when to stay quiet, and about a schedule that does not drift or run away.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent import reminders as rem
from agent.reminders import BadTime

NOW = datetime(2026, 9, 20, 16, 0, 0)


def _at(**delta) -> str:
    return (NOW + timedelta(**delta)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# One-shot
# --------------------------------------------------------------------------


def test_a_reminder_is_booked_for_the_time_given(store):
    booked = rem.schedule(store, kind="remind", text="grab groceries",
                          when=_at(hours=1), now=NOW)
    assert booked.next_fire_at == _at(hours=1)
    assert booked.recurring is False
    assert booked.max_fires == 1


def test_a_one_shot_is_finished_after_firing(store):
    booked = rem.schedule(store, kind="remind", text="x", when=_at(minutes=5), now=NOW)
    assert rem.advance(store, booked, now=NOW + timedelta(minutes=5)) is True
    assert rem.get(store, booked.id).status == "done"


def test_it_is_not_due_before_its_time(store):
    rem.schedule(store, kind="remind", text="x", when=_at(hours=1), now=NOW)
    assert rem.due(store, now=NOW) == []
    assert len(rem.due(store, now=NOW + timedelta(hours=1))) == 1


# --------------------------------------------------------------------------
# Times a model might produce
# --------------------------------------------------------------------------


def test_a_time_in_the_past_is_refused(store):
    with pytest.raises(BadTime, match="past"):
        rem.schedule(store, kind="remind", text="x", when=_at(hours=-2), now=NOW)


def test_a_few_seconds_of_slack_is_allowed(store):
    """A model computing "in 5 minutes" from an injected clock is often a
    little behind by the time the call lands. Refusing that is pedantic."""
    rem.schedule(store, kind="remind", text="x", when=_at(seconds=-30), now=NOW)


def test_words_are_not_accepted_as_times(store):
    """The model resolves "tomorrow" itself, because it knows what day it is
    and this does not."""
    with pytest.raises(BadTime, match="ISO 8601"):
        rem.schedule(store, kind="remind", text="x", when="tomorrow at 5", now=NOW)


def test_an_absurdly_distant_time_is_refused(store):
    with pytest.raises(BadTime, match="days away"):
        rem.schedule(store, kind="remind", text="x", when=_at(days=400), now=NOW)


def test_an_empty_reminder_is_refused(store):
    with pytest.raises(BadTime):
        rem.schedule(store, kind="remind", text="   ", when=_at(hours=1), now=NOW)


def test_a_timezone_aware_time_is_accepted(store):
    """Some models emit an offset, some do not. Both mean the same moment."""
    aware = (NOW + timedelta(hours=1)).astimezone().isoformat(timespec="seconds")
    assert rem.schedule(store, kind="remind", text="x", when=aware, now=NOW)


# --------------------------------------------------------------------------
# Repeating
# --------------------------------------------------------------------------


def test_a_repeating_check_needs_an_end(store):
    """Otherwise "every 10 minutes" runs until somebody notices the bill."""
    with pytest.raises(BadTime, match="end time"):
        rem.schedule(store, kind="check", text="score?", when=_at(minutes=1),
                     every_minutes=10, now=NOW)


def test_the_number_of_checks_follows_from_the_span(store):
    booked = rem.schedule(store, kind="check", text="score?", when=_at(minutes=0),
                          every_minutes=10, until=_at(minutes=60), now=NOW)
    assert booked.max_fires == 7  # 0,10,20,30,40,50,60


def test_a_very_short_interval_is_raised_to_the_floor(store):
    """Each check is a model call; "every 10 seconds" is not a thing anybody
    means literally."""
    booked = rem.schedule(store, kind="check", text="x", when=_at(minutes=0),
                          every_minutes=0.1, until=_at(minutes=30), now=NOW)
    assert booked.every_seconds >= rem.MIN_INTERVAL_S


def test_a_span_longer_than_a_day_is_refused(store):
    with pytest.raises(BadTime, match="at most"):
        rem.schedule(store, kind="check", text="x", when=_at(minutes=0),
                     every_minutes=30, until=_at(days=3), now=NOW)


def test_an_end_before_the_start_is_refused(store):
    with pytest.raises(BadTime, match="before"):
        rem.schedule(store, kind="check", text="x", when=_at(hours=2),
                     every_minutes=10, until=_at(hours=1), now=NOW)


def test_the_series_does_not_drift(store):
    """The next time comes from the schedule, not from when it actually
    fired -- otherwise a check that ran ten seconds late moves the whole
    series ten seconds later, and again, and again."""
    booked = rem.schedule(store, kind="check", text="x", when=_at(minutes=0),
                          every_minutes=10, until=_at(minutes=60), now=NOW)

    rem.advance(store, booked, result="a", now=NOW + timedelta(seconds=25))
    assert rem.get(store, booked.id).next_fire_at == _at(minutes=10)


def test_falling_far_behind_skips_rather_than_backlogs(store):
    """A laptop that slept through four checks should do the next one, not
    four in a row at once."""
    booked = rem.schedule(store, kind="check", text="x", when=_at(minutes=0),
                          every_minutes=10, until=_at(minutes=60), now=NOW)

    rem.advance(store, booked, result="a", now=NOW + timedelta(minutes=35))
    assert rem.get(store, booked.id).next_fire_at == _at(minutes=40)


def test_it_stops_at_the_end_time(store):
    booked = rem.schedule(store, kind="check", text="x", when=_at(minutes=0),
                          every_minutes=30, until=_at(minutes=60), now=NOW)
    for _ in range(booked.max_fires):
        booked = rem.get(store, booked.id)
        finished = rem.advance(store, booked, result="a")
    assert finished is True
    assert rem.get(store, booked.id).status == "done"


# --------------------------------------------------------------------------
# When to actually say something
# --------------------------------------------------------------------------


def _check(fires: int = 0, last: str | None = None) -> rem.Reminder:
    return rem.Reminder(
        id=1, kind="check", text="score?", next_fire_at=_at(), every_seconds=600,
        until_at=_at(hours=1), fires=fires, max_fires=6, last_result=last,
        status="active",
    )


def test_the_first_answer_is_always_reported():
    """Otherwise the first ten minutes are silent and it looks broken."""
    assert rem.should_report(_check(fires=0), "14-7", finished=False)


def test_an_unchanged_answer_is_not_reported():
    """The whole point. Six messages saying 14-7 bury the one saying 21-7."""
    assert not rem.should_report(_check(fires=2, last="14-7"), "14-7", finished=False)


def test_a_changed_answer_is_reported():
    assert rem.should_report(_check(fires=2, last="14-7"), "21-7", finished=False)


def test_the_last_answer_is_always_reported():
    """So the series closes rather than trailing off in silence."""
    assert rem.should_report(_check(fires=5, last="21-7"), "21-7", finished=True)


@pytest.mark.parametrize(
    ("before", "after"),
    [("14-7", "14-7 "), ("14-7", "14-7"), ("Score: 14-7", "score:  14-7")],
)
def test_trivial_rewording_is_not_a_change(before, after):
    """A model asked the same question twice rarely answers byte-identically;
    treating whitespace and case as news would defeat the whole filter."""
    assert not rem.should_report(_check(fires=1, last=before), after, finished=False)


# --------------------------------------------------------------------------
# Managing them
# --------------------------------------------------------------------------


def test_active_lists_what_is_pending(store):
    rem.schedule(store, kind="remind", text="a", when=_at(hours=1), now=NOW)
    rem.schedule(store, kind="remind", text="b", when=_at(hours=2), now=NOW)
    assert [r.text for r in rem.active(store)] == ["a", "b"]


def test_cancelling_stops_it_firing(store):
    booked = rem.schedule(store, kind="remind", text="a", when=_at(minutes=1), now=NOW)
    assert rem.cancel(store, booked.id) is True
    assert rem.due(store, now=NOW + timedelta(hours=1)) == []


def test_cancelling_twice_is_not_an_error(store):
    booked = rem.schedule(store, kind="remind", text="a", when=_at(hours=1), now=NOW)
    rem.cancel(store, booked.id)
    assert rem.cancel(store, booked.id) is False


def test_a_description_reads_like_a_sentence(store):
    """It goes straight back to the user when the user asks what is scheduled."""
    one = rem.schedule(store, kind="remind", text="grab groceries",
                       when=_at(hours=1), now=NOW)
    many = rem.schedule(store, kind="check", text="score?", when=_at(minutes=0),
                        every_minutes=10, until=_at(minutes=60), now=NOW)

    assert "grab groceries" in one.describe()
    assert "every 10 min" in many.describe()


# --------------------------------------------------------------------------
# Getting rid of them
# --------------------------------------------------------------------------


def test_several_are_cancelled_in_one_go(store):
    """"Cancel everything" used to cost a tool call per reminder against a
    budget of twenty, so a busy week would fail halfway and leave the rest
    running -- the worst way for a cancel to go wrong."""
    ids = [
        rem.schedule(store, kind="remind", text=f"thing {i}",
                     when=_at(hours=i + 1), now=NOW).id
        for i in range(5)
    ]
    assert rem.cancel_many(store, ids) == 5
    assert rem.active(store) == []


def test_cancel_all_needs_no_ids(store):
    for i in range(3):
        rem.schedule(store, kind="remind", text=f"t{i}", when=_at(hours=i + 1), now=NOW)
    assert rem.cancel_all(store) == 3
    assert rem.active(store) == []


def test_cancelling_a_mix_of_live_and_dead_ids_counts_only_the_live(store):
    """So the agent can say "cancelled two" rather than "cancelled four" after
    the model passes ids it read a minute ago."""
    live = rem.schedule(store, kind="remind", text="a", when=_at(hours=1), now=NOW).id
    gone = rem.schedule(store, kind="remind", text="b", when=_at(hours=2), now=NOW).id
    rem.cancel(store, gone)

    assert rem.cancel_many(store, [live, gone, 9999]) == 1


def test_cancelling_nothing_is_not_an_error(store):
    assert rem.cancel_many(store, []) == 0
    assert rem.cancel_all(store) == 0


def test_a_cancelled_reminder_never_fires(store):
    booked = rem.schedule(store, kind="check", text="score?", when=_at(minutes=1),
                          every_minutes=10, until=_at(minutes=60), now=NOW)
    rem.cancel(store, booked.id)
    assert rem.due(store, now=NOW + timedelta(hours=2)) == []


# --------------------------------------------------------------------------
# Sleeping through one
# --------------------------------------------------------------------------


def test_a_reminder_missed_while_asleep_still_fires(store):
    """The laptop is the only thing that fires these, so one booked for 5pm
    on a machine shut at 4 has to survive until it wakes. Late is a fair
    outcome; silently lost is not."""
    rem.schedule(store, kind="remind", text="grab groceries",
                 when=_at(hours=1), now=NOW)

    woke = NOW + timedelta(hours=3)
    assert [r.text for r in rem.due(store, now=woke)] == ["grab groceries"]


# --------------------------------------------------------------------------
# What just went off
# --------------------------------------------------------------------------


def _finished(store, reminder, status: str, hours_ago: float) -> None:
    with store:
        store.execute(
            "UPDATE reminders SET status = ?, finished_at = datetime('now', ?) WHERE id = ?",
            (status, f"-{hours_ago * 60:.0f} minutes", reminder.id),
        )


def test_what_just_went_off_or_was_cancelled_is_listed_with_when(store):
    """27 September 2026: the mop and laundry reminders went off, the user answered
    "both complete", and the list the model checked held only what was still
    scheduled -- the week's gym and run reminders. It took "both" to mean
    those and cancelled all six."""
    mop = rem.schedule(store, kind="remind", text="Mop", when=_at(hours=1), now=NOW)
    sheets = rem.schedule(store, kind="remind", text="put your sheets in the wash",
                          when=_at(hours=2), now=NOW)
    yesterday = rem.schedule(store, kind="remind", text="yesterday's", when=_at(hours=3), now=NOW)
    gym = rem.schedule(store, kind="remind", text="Gym at PWG", when=_at(days=1), now=NOW)
    _finished(store, mop, "done", 0.4)
    _finished(store, sheets, "cancelled", 1)
    _finished(store, yesterday, "done", 13)

    listed = rem.recent(store)

    assert [r["text"] for r in listed] == ["Mop", "put your sheets in the wash"]
    assert gym.id not in [r["id"] for r in listed]
    assert listed[0]["status"] == "done" and "went_off" in listed[0]
    assert listed[1]["status"] == "cancelled" and "cancelled" in listed[1]
    # Local time, as every other time the model is given: SQLite's UTC text
    # would put it four hours out.
    went_off = datetime.fromisoformat(listed[0]["went_off"])
    assert abs(went_off - (datetime.now() - timedelta(minutes=24))) < timedelta(minutes=2)


async def test_the_list_the_model_reads_has_what_just_went_off_after_what_is_scheduled(store):
    from types import SimpleNamespace

    from agent import loop
    from agent.models import Role

    gym = rem.schedule(store, kind="remind", text="Gym at PWG", when=_at(days=1), now=NOW)
    mop = rem.schedule(store, kind="remind", text="Mop", when=_at(hours=1), now=NOW)
    _finished(store, mop, "done", 0.5)
    tools = loop.build_agent(Role.CONVERSE)._function_toolset.tools
    ctx = SimpleNamespace(deps=SimpleNamespace(tools_used=[], conn=store))

    listed = await tools["list_reminders"].function(ctx)

    assert [(r["id"], r["status"]) for r in listed] == [(gym.id, "active"), (mop.id, "done")]
