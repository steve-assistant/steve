"""The canary's count: whether Steve answers worse since a change.

In the gate because a change must not be able to shade the numbers it is
judged by. The model switch watches a new model with it
(agent/models_switch.py). Until 13 October 2026 the supervisor watched
every merged self-upgrade with it too, and put main back on a rise; Steve
no longer merges its own changes (agent/upgrade.py), and that watch and
its tests went with the merging. See core/canary.py.
"""

from __future__ import annotations

import pytest

from core import canary, db


def _counts(turns: int, bad: int) -> canary.Counts:
    return canary.Counts(turns=turns, bad=bad, by_kind={"turn_broke": bad} if bad else {})


# --------------------------------------------------------------------------
# The bar
# --------------------------------------------------------------------------


@pytest.mark.parametrize("before, after, worse", [
    ((40, 4), (20, 8), True),    # 10% -> 40%
    ((40, 4), (20, 4), False),   # 10% -> 20%: double, but not 15 points up
    ((40, 12), (20, 9), False),  # 30% -> 45%: not double
    ((40, 0), (20, 2), False),   # two is not a pattern
    ((40, 0), (3, 3), True),     # too few replies for a rate, but every one went wrong
    ((40, 0), (4, 3), False),    # too few, and not all
    ((2, 0), (20, 7), True),     # no baseline worth the name: 15% assumed; 35% is over it
    ((2, 0), (20, 4), False),    # ... and 20% is not
    ((40, 0), (5, 2), False),    # two is not a pattern, however few the replies
])
def test_the_canary_calls_a_regression_only_past_a_clear_bar(before, after, worse):
    said = canary.verdict(_counts(*before), _counts(*after))

    assert (said is not None) is worse, said


def test_a_new_kind_of_failure_is_seen_under_the_noise_of_an_old_one():
    """The real two days before 30 September: 51 replies, 11 tool errors
    from one tool the model misused. Three broken turns after a change,
    where there had been none, is a regression the whole rate hides; eight
    tool errors in twenty, the same old noise, is not."""
    before = canary.Counts(turns=51, bad=11, by_kind={"tool_error": 11})
    broke = canary.Counts(turns=20, bad=8, by_kind={"tool_error": 5, "turn_broke": 3})
    noise = canary.Counts(turns=20, bad=8, by_kind={"tool_error": 8})

    said = canary.verdict(before, broke)

    assert said is not None and "3 turn_broke in 20 replies" in said
    assert canary.verdict(before, noise) is None


def test_what_is_counted_is_replies_and_the_ledgers_bad_events_in_the_window(tmp_path):
    conn = db.open_store(tmp_path / "canary.db")
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
        for at in ("2026-10-01 10:00:00", "2026-10-01 11:00:00", "2026-09-30 09:00:00"):
            conn.execute("INSERT INTO messages (session_id, role, content, created_at)"
                         " VALUES (?, 'assistant', 'a reply', ?)", (session, at))
        conn.execute("INSERT INTO messages (session_id, role, content, created_at)"
                     " VALUES (?, 'user', 'a question', '2026-10-01 10:30:00')", (session,))
        for kind, at, summary in (
                ("turn_broke", "2026-10-01 10:05:00", "A turn broke: KeyError: 'start'"),
                ("feedback_down", "2026-10-01 10:06:00", "Maya gave a thumbs-down"),
                ("slow_turn", "2026-10-01 10:07:00", "A reply took 133s"),  # the provider's
                ("turn_broke", "2026-10-01 10:08:00",                       # the network's
                 "A turn broke: ModelAPIError: Connection error."),
                ("tool_error", "2026-10-01 10:09:00",                       # ... and again
                 "read_page reported: ConnectError: [Errno 11001] getaddrinfo failed"),
                ("turn_broke", "2026-09-30 09:05:00", "A turn broke: KeyError")):  # before
            issue = conn.execute("INSERT INTO issues (kind, fingerprint, summary) VALUES"
                                 " (?, ?, ?)", (kind, f"{kind}:{at}", summary)).lastrowid
            conn.execute("INSERT INTO issue_events (issue_id, at) VALUES (?, ?)", (issue, at))

    counted = canary.count(conn, "2026-10-01 00:00:00", "2026-10-02 00:00:00")
    conn.close()

    assert (counted.turns, counted.bad) == (2, 2)
    assert counted.by_kind == {"turn_broke": 1, "feedback_down": 1}
