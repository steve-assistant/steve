"""Calendar-day countdown, deterministic because today is a parameter.

The built-in module `countdown` (modules/countdown/), tested here as plain
code; tests/gate/test_module_tools.py runs it in the sandbox."""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime

import pytest

from modules.countdown.countdown import calendar_days
from modules.countdown.countdown import days_until as _days_until


class _Ctx:
    """The one part of a module's context the tool reads: the user's zone."""

    zone = zoneinfo.ZoneInfo("Pacific/Kiritimati")


def days_until(target, today=None):
    return _days_until(_Ctx(), target, today)

# --------------------------------------------------------------------------
# The headline cases
# --------------------------------------------------------------------------


def test_the_year_end_countdown():
    """Sep 20 to Dec 31: ten September days, then 31, 30, 31."""
    assert calendar_days("2026-12-31", today=date(2026, 9, 20)) == 102


def test_tomorrow_is_one_day_away():
    assert calendar_days("2026-09-21", today="2026-09-20") == 1


def test_the_count_shrinks_by_exactly_one_per_day():
    assert calendar_days("2026-12-31", today=date(2026, 9, 21)) == 101


def test_the_first_of_the_next_month_crosses_a_month_boundary():
    assert calendar_days("2026-10-01", today=date(2026, 9, 20)) == 11


def test_a_date_object_is_as_good_as_a_string():
    assert calendar_days(date(2026, 12, 31), today=date(2026, 9, 20)) == 102


# --------------------------------------------------------------------------
# Today: a countdown that has arrived
# --------------------------------------------------------------------------


def test_today_is_zero_days_away():
    assert calendar_days("2026-09-20", today=date(2026, 9, 20)) == 0


def test_today_is_zero_in_either_input_form():
    assert calendar_days(date(2026, 9, 20), today="2026-09-20") == 0


def test_omitting_today_counts_from_today_where_the_user_is():
    """The one test allowed to read the clock. Today is the user's, in
    their zone, not the computer's: in Kiritimati (UTC+14) it is a day
    ahead of most computers for half of every day, and "how long until
    tomorrow" answered from the computer's date would be off by one."""
    theirs = datetime.now(_Ctx.zone).date()

    assert days_until(theirs.isoformat())["days"] == 0


# --------------------------------------------------------------------------
# The past: signed, not clamped
# --------------------------------------------------------------------------


def test_yesterday_is_minus_one():
    assert calendar_days("2026-09-19", today=date(2026, 9, 20)) == -1


def test_a_week_ago_is_minus_seven():
    assert calendar_days("2026-09-13", today=date(2026, 9, 20)) == -7


def test_zero_can_only_ever_mean_today():
    """Clamping the past to zero would make "0 days" ambiguous between "it is
    today" and "it already happened"; the sign is the documented answer."""
    assert calendar_days("2026-01-01", today=date(2026, 9, 20)) < 0
    assert calendar_days("2026-09-20", today=date(2026, 9, 20)) == 0


# --------------------------------------------------------------------------
# The calendar is datetime's problem
# --------------------------------------------------------------------------


def test_leap_day_sits_between_feb_28_and_march_1_in_a_leap_year():
    assert calendar_days("2028-03-01", today=date(2028, 2, 28)) == 2


def test_feb_28_to_march_1_is_one_day_in_a_common_year():
    assert calendar_days("2027-03-01", today=date(2027, 2, 28)) == 1


def test_a_year_straddling_a_leap_day_is_366_days():
    assert calendar_days("2029-02-28", today=date(2028, 2, 28)) == 366


# --------------------------------------------------------------------------
# Input forms: string, date, datetime
# --------------------------------------------------------------------------


def test_the_compact_iso_form_yyyymmdd_is_accepted():
    assert calendar_days("20261231", today="20260920") == 102


def test_a_datetime_is_reduced_to_its_date_part():
    """datetime types as a date, and midnight versus a second to midnight is
    not a question a calendar-days answer can hear: both must agree."""
    assert calendar_days(datetime(2026, 12, 31, 0, 0), today=date(2026, 9, 20)) == 102
    assert calendar_days(datetime(2026, 12, 31, 23, 59, 59), today=date(2026, 9, 20)) == 102


def test_today_may_be_a_datetime_or_a_string_too():
    assert calendar_days("2026-12-31", today=datetime(2026, 9, 20, 15, 0)) == 102
    assert calendar_days("2026-12-31", today="2026-09-20") == 102


def test_the_result_is_always_an_int():
    assert isinstance(calendar_days("2026-09-21", today="2026-09-20"), int)


# --------------------------------------------------------------------------
# Validation: junk is a caller bug, and stops here
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "not a date",
        "31-12-2026",
        "2026-1-5",  # ISO requires zero-padding
        "2026-13-01",  # month 13
        "2026-02-30",  # day out of range for month
        "",
        " 2026-12-31",  # whitespace is not part of any ISO form
        "2026-12-31 ",
        "2026-12-31T23:59",  # a time component makes it a datetime string
    ],
)
def test_malformed_target_strings_raise(bad):
    with pytest.raises(ValueError):
        calendar_days(bad, today=date(2026, 9, 20))


@pytest.mark.parametrize("bad", [None, 20261231, 3.5, True, ["2026-12-31"], object()])
def test_non_string_non_date_targets_raise(bad):
    with pytest.raises(ValueError):
        calendar_days(bad, today=date(2026, 9, 20))


@pytest.mark.parametrize("bad", ["yesterday", 7, 3.5, True, object()])
def test_a_bad_today_parameter_raises_even_when_the_target_is_fine(bad):
    with pytest.raises(ValueError):
        calendar_days("2026-12-31", today=bad)


# --------------------------------------------------------------------------
# The tool the model calls
# --------------------------------------------------------------------------


def test_the_tool_says_which_day_of_the_week_it_is():
    """The system prompt promised the weekday from the start; the tool only
    ever returned a count, so the model supplied the weekday itself."""
    answer = days_until("2026-10-31", today="2026-09-23")

    assert answer == {"days": 38, "date": "2026-10-31", "weekday": "Saturday"}


def test_the_tool_and_the_arithmetic_agree():
    assert days_until("2026-09-19", today="2026-09-20")["days"] == calendar_days(
        "2026-09-19", today="2026-09-20"
    ) == -1
