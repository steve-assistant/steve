"""A routine or goal whose card ends without a yes ends too.

One that may act without asking waits for the user's yes on its card
(`approve_routine`, `approve_goal`). A no left it waiting all the same,
with no card left to say yes to: listed as waiting, named so in the
morning's briefing, and a routine held one of its twenty places for good.
So did a card that expired, and a yes undone during the hold
(db/migrations/0020_unapproved.sql). Each ending is driven here through
approvals' own functions, as the worker reaches them.
"""

from __future__ import annotations

import contextlib

import pytest

from agent import approvals, goals, routines
from core import db, monitor
from core.consent import classify

IRIS = "iris@example.com"
ENVELOPE = {"send_email": {"targets": [IRIS]}}


@pytest.fixture
def conn(tmp_path):
    with contextlib.closing(db.open_store(tmp_path / "agent.db")) as store:
        yield store


def _card(conn, tool: str, args: dict) -> approvals.Action:
    _, card = approvals.queue(conn, tool=tool, args=args, preview=f"{tool} {args}",
                              consent=classify(tool, args))
    assert card is not None
    return card


def _routine(conn, title: str = "Friday recap") -> tuple[routines.Routine, approvals.Action]:
    made = routines.create(conn, title=title, instruction="Email Iris a recap.",
                           schedule=routines.Schedule.build(at_time="19:00", days=["fri"]),
                           envelope=ENVELOPE)
    assert made.status == "awaiting_approval"
    return made, _card(conn, "approve_routine", {
        "routine_id": made.id, "envelope_sha": monitor.holder_sha(conn, "routine", made.id),
        "title": made.title})


def _status(conn, routine: routines.Routine) -> str:
    return routines.get(conn, routine.id).status


def test_a_no_to_a_routines_card_cancels_the_routine(conn):
    """The user said no: nothing is left waiting for a yes they gave."""
    routine, card = _routine(conn)

    assert approvals.decide(conn, card.id, approve=False, by="tray")["ok"]

    assert _status(conn, routine) == "cancelled"
    assert routine.id not in [r.id for r in routines.listed(conn)]


def test_a_routines_card_that_expires_unanswered_cancels_the_routine(conn):
    """A card is asked for twelve hours (`approvals.TTL`); after that there
    is nothing to say yes to, so nothing may wait for it."""
    routine, card = _routine(conn)
    with conn:
        conn.execute("UPDATE actions SET expires_at = '2026-10-01T00:00:00' WHERE id = ?",
                     (card.id,))

    assert approvals.expire_stale(conn) == 1

    assert _status(conn, routine) == "cancelled"


def test_a_yes_undone_during_the_hold_cancels_the_routine(conn):
    routine, card = _routine(conn)
    assert (yes := approvals.decide(conn, card.id, approve=True, by="tray"))["ok"], yes
    assert approvals.claim(conn, card.id) is not None

    assert approvals.cancel_during_hold(conn, card.id)

    assert _status(conn, routine) == "cancelled"


def test_a_yes_leaves_the_routine_waiting_for_its_card_to_run_and_a_no_ends_only_its_own(conn):
    """The routine starts when its card runs (`routines.activate`), not at
    the yes; a no to another routine's card is not a no to this one."""
    routine, card = _routine(conn)
    other, refused = _routine(conn, "Garden log")

    assert (yes := approvals.decide(conn, card.id, approve=True, by="tray"))["ok"], yes
    assert approvals.decide(conn, refused.id, approve=False, by="tray")["ok"]

    assert _status(conn, routine) == "awaiting_approval"
    assert _status(conn, other) == "cancelled"


def test_a_no_to_a_goals_card_drops_the_goal_and_says_why(conn):
    goal = goals.create(conn, title="Dinner with Iris", ask="Find an evening for dinner.",
                        done_when="a date is agreed", steps=["ask Iris"], envelope=ENVELOPE)
    assert goal.status == "awaiting_approval"
    card = _card(conn, "approve_goal", {"goal_id": goal.id,
                                        "envelope_sha": monitor.holder_sha(conn, "goal",
                                                                           goal.id),
                                        "title": goal.title})

    assert approvals.decide(conn, card.id, approve=False, by="tray")["ok"]

    dropped = goals.get(conn, goal.id)
    assert dropped.status == "dropped" and dropped.outcome == "its card was not approved"
    assert dropped.id not in [g.id for g in goals.listed(conn)]
