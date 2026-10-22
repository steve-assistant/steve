"""Calendar-day countdown to a date.

A countdown question -- "how long until the 31st?" -- is arithmetic the
assistant should never guess at, and the one place where exactness matters
most is also where off-by-one errors are easiest: months differ in length
and leap years move February. This module delegates every calendar question
to datetime.date, which owns the rules, and keeps only the subtraction for
itself.

The result is a signed count of calendar days: positive while the target is
ahead, exactly 0 when the target *is* today, and negative for a date already
past (-1 yesterday, -7 a week ago). Clamping the past to zero would make
"0 days" mean both "it is today" and "it already happened", which are
different answers to a person who asked; the sign says which.

Inputs are accepted in the two forms people actually have on hand: an ISO
8601 date string (YYYY-MM-DD at minimum; the compact YYYYMMDD form parses
too) or a datetime.date. A datetime.datetime is accepted and reduced to its
date part -- it types as a date, and midnight versus a second to midnight is
not a question a calendar-days answer can hear. Everything else is a caller
bug and raises ValueError, in the house style: a silently wrong answer is
worse than a loud refusal.

"Today" is the user's, in their time zone (`ctx.zone`): the sandbox's
clock is the computer's, and near midnight the two disagree on the date.
It can be given instead, which makes the answer deterministic for tests
and replays.
"""

from __future__ import annotations

from datetime import date, datetime


def _as_date(value: object, what: str) -> date:
    """Normalise one accepted input form into a datetime.date.

    datetime.datetime is a subclass of datetime.date, so it is checked first:
    handed straight to date arithmetic it would mostly work, but the mismatch
    surfaces elsewhere as "can't compare datetime to date", and the date part
    is all this module can meaningfully use.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{what} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc
    raise ValueError(f"{what} must be an ISO date string or a date, got {value!r}")


def calendar_days(target: str | date, today: str | date) -> int:
    """Return the number of calendar days from ``today`` until ``target``.

    Signed: positive while ``target`` is ahead, exactly 0 on the day itself,
    and negative once it has passed, so "0" can only ever mean "today".

    Both take an ISO date string (YYYY-MM-DD at minimum) or a datetime.date;
    a datetime.datetime is reduced to its date part. Anything else raises
    ValueError.
    """
    return (_as_date(target, "target") - _as_date(today, "today")).days


def days_until(ctx, target: str, today: str | None = None) -> dict:
    """The tool: how many days until ``target``, from ``today`` or the
    user's today, and which day of the week it is. Returns ``days``
    (signed, as `calendar_days`), the ``date`` itself, and its ``weekday``."""
    target_date = _as_date(target, "target")
    start = datetime.now(ctx.zone).date() if today is None else today
    return {
        "days": calendar_days(target_date, start),
        "date": target_date.isoformat(),
        "weekday": target_date.strftime("%A"),
    }
