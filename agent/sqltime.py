"""SQLite's clock, spelled once.

SQLite's `datetime('now')` is UTC text, `YYYY-MM-DD HH:MM:SS`, and Python's
`datetime.now()` is local. A comparison between the two is wrong for four
hours a day and passes every daytime test (docs/protocols.md §7). Six places
turned a Python moment into SQLite's text by hand -- three of them private
`_utc` copies -- so a fix to one would not have reached the others.

The format and the two conversions are `core/clock.py`'s since step 3, so
that core can count the user's day; they are handed on from here. "Since
midnight" was spelled here too, as SQLite's `localtime` -- the machine's zone,
not the one the code pins -- and is now `core/spend.windows`' day.

The form the app reads a moment in is here too (`iso`): four of the worker's
route modules each wrote it, and one kept the microseconds the others cut.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.clock import FORMAT, moment, stamp

__all__ = ["FORMAT", "iso", "iso_of", "moment", "stamp"]


def iso(when: datetime) -> str:
    """A moment as the app reads one: UTC ISO to the second, with a Z, which
    sorts as text."""
    return when.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_of(text: str | None) -> str | None:
    """SQLite's UTC text in that form; None for none, and for text that is
    not a moment."""
    if not text:
        return None
    try:
        return iso(moment(text))
    except ValueError:
        return None
