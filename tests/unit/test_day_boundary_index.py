"""Today's spend is read through an index, with the same day boundary.

`datetime(created_at, 'localtime') >= date('now', 'localtime')` turns every
row into local time before comparing, so no index can serve it: a scan of
every model call ever made, on every attention tick, status poll, routine
and goal check (5 October 2026). Local midnight turned into UTC once --
`created_at >= datetime(date('now', 'localtime'), 'utc')` -- is the same
boundary as a constant, which the index on `created_at` can serve.

The day boundary is where this codebase has been wrong before (protocols
§7: it rolled over at 8 p.m. three separate times), so the two forms are
compared at the hours that broke it and across both of 2026's clock
changes, rather than trusted.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from core import db

OLD = "datetime(?, 'localtime') >= date(?, 'localtime')"
NEW = "? >= datetime(date(?, 'localtime'), 'utc')"


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


@pytest.mark.parametrize("day", ["2026-03-08", "2026-11-01", "2026-10-05", "2026-12-31"])
def test_local_midnight_in_utc_is_the_same_day_boundary(day):
    """`now` at every half hour of the day, rows every ten minutes for two
    days around it: both forms pick the same rows."""
    conn = sqlite3.connect(":memory:")
    start = datetime.fromisoformat(day).astimezone()  # local midnight, this machine's zone
    for half_hours in range(48):
        now = _utc(start + timedelta(minutes=30 * half_hours))
        for tens in range(-144, 144):
            row = _utc(start + timedelta(minutes=10 * tens))
            old = conn.execute(f"SELECT {OLD}", (row, now)).fetchone()[0]
            new = conn.execute(f"SELECT {NEW}", (row, now)).fetchone()[0]
            assert old == new, (now, row)


def test_todays_spend_is_read_through_the_index(tmp_path):
    from core import spend

    conn = db.open_store(tmp_path / "t.db")
    since = spend.windows().day.since
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    spend.spent(conn, since, part="triage")
    conn.set_trace_callback(None)
    [asked] = [q for q in seen if "usage_events" in q]
    plans = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + asked)]

    assert all("SCAN u" not in p and "SCAN usage_events" not in p for p in plans), plans
    assert any("idx_usage_created" in p for p in plans), plans
    conn.close()
