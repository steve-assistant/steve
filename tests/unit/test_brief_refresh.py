"""A pre-meeting brief is current when it goes out, and names nobody it guessed.

The failure this pins, from 5-7 October 2026. The calendar showed two
"Fireside-Chat Moderator Prep Call with Alex" events, 7 and 19 October,
with nobody listed. The doer prepared both on the 5th (sessions 252-253) and
said "Alex appears to be Alexandra Wren at Contoso" -- the ledger's only
"alex", a stranger to the call. The 7 October brief went out and the user
answered "That's who Alex is, not Alexandra Wren". By then the event listed
the real Alex (aarden@alumni.example, obs 3722) and nothing had looked again;
the 19 October brief (obs 3157) carried the same sentence, prepared fourteen
days before its moment, and was still queued.

Two things bring a brief back to the doer: its moment is near and it was
written more than a day before it (`store.refresh_briefs`), or a look at the
same event lists different people (`store.record_new`). All times here are
relative to the SQLite clock the queries use, so no day boundary or quiet
hour is involved.
"""

from __future__ import annotations

import datetime as dt

import pytest

from agent import sqltime
from agent.attention import prepare, store
from agent.attention.store import Observation
from core import spend

OLD_TEXT = ("Alex appears to be Alexandra Wren at Contoso; your last exchange was about a "
            "Contoso club.")
NEW_TEXT = "The call is with Alex Arden (aarden@alumni.example).\nBring the question list."
EVENT = "obcd50k0mcatfota6noj5qepuo"


def _ago(**span) -> str:
    return sqltime.stamp(dt.datetime.now() - dt.timedelta(**span))


def _in(**span) -> str:
    return sqltime.stamp(dt.datetime.now() + dt.timedelta(**span))


def _brief(conn, *, due: str, prepared: str, text: str | None = OLD_TEXT, guests=(),
           event: str = EVENT, key: str | None = None, source: str = "calendar") -> int:
    """A brief the doer has already written, as the pipeline leaves it."""
    [obs] = store.record_new(conn, [Observation(
        source=source, key=key or f"{source}:{event}:2026-10-05T18:33:35.479Z",
        title="Fireside-Chat Moderator Prep Call with Alex",
        ref={"event_id": event, "people": list(guests)},
    )])
    store.judged(conn, obs.id, say="before", critical=False,
                 headline="Prep Call with Alex, Monday 1pm.",
                 task="Prepare a brief for the call with Alex", deliver_at=due)
    store.start_task(conn, obs.id)
    store.finish_task(conn, obs.id, text)
    with conn:
        # Judged and prepared together, as long ago as the brief is old: a
        # row judged "just now" would hide whether a brief sent back is
        # held for the doer, which is what `_PREPARED` reads `triaged_at` for.
        conn.execute("UPDATE observations SET prepared_at = ?, triaged_at = ? WHERE id = ?",
                     (prepared, prepared, obs.id))
    return obs.id


def _row(conn, obs_id: int):
    return conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()


class _Doer:
    """Stands in for `Conversation`: replies from a queue, keeps what it was asked."""

    replies: list = []
    asked: list = []

    def __init__(self, conn, *, channel):
        self.conn = conn

    async def send(self, text, *, allow=None):
        _Doer.asked.append(text)
        return _Doer.replies.pop(0)

    def end(self):
        pass


class _Reply:
    def __init__(self, text, tools=("who_is", "search_email"), cost=0.001):
        self.text, self.tools_used, self.cost_usd = text, list(tools), cost


@pytest.fixture
def doer(monkeypatch):
    import agent.loop

    _Doer.replies, _Doer.asked = [], []
    monkeypatch.setattr(agent.loop, "Conversation", _Doer)
    # Browsing workers off: what the doer's turn is given with them on is
    # the gate's (tests/gate/test_browsing_workers.py).
    monkeypatch.setattr("core.browser.background", lambda: False)
    return _Doer


# --------------------------------------------------------------------------
# Written long ago, written again when the day comes
# --------------------------------------------------------------------------


async def test_a_brief_written_two_weeks_early_is_written_again_the_day_before(conn, doer):
    """Obs 3157 was prepared on 5 October for a call on 19 October."""
    brief = _brief(conn, due=_in(hours=20), prepared=_ago(days=14))
    doer.replies = [_Reply(NEW_TEXT)]

    await prepare.prepare(conn)

    assert len(doer.asked) == 1
    assert _row(conn, brief)["outcome"] == NEW_TEXT


async def test_a_brief_written_the_day_before_is_not_written_again(conn, doer):
    brief = _brief(conn, due=_in(hours=5), prepared=_ago(hours=10))

    await prepare.prepare(conn)

    assert doer.asked == []
    assert _row(conn, brief)["outcome"] == OLD_TEXT


async def test_a_brief_waits_for_its_day_before_it_is_written_again(conn, doer):
    """Not at once: the reason to look again is that the meeting is near."""
    brief = _brief(conn, due=_in(days=3), prepared=_ago(days=14))

    await prepare.prepare(conn)
    assert doer.asked == [] and _row(conn, brief)["outcome"] == OLD_TEXT

    with conn:
        conn.execute("UPDATE observations SET deliver_at = ? WHERE id = ?",
                     (_in(hours=20), brief))
    doer.replies = [_Reply(NEW_TEXT)]
    await prepare.prepare(conn)

    assert len(doer.asked) == 1 and _row(conn, brief)["outcome"] == NEW_TEXT


async def test_a_brief_is_written_again_once_and_not_on_every_tick(conn, doer):
    """The loop runs every minute; written again, the brief is no longer stale."""
    _brief(conn, due=_in(hours=20), prepared=_ago(days=14))
    doer.replies = [_Reply(NEW_TEXT), _Reply(NEW_TEXT)]

    await prepare.prepare(conn)
    await prepare.prepare(conn)

    assert len(doer.asked) == 1


async def test_a_brief_already_sent_is_left_as_it_went(conn, doer):
    """Written again, it would change nothing the user has: they were sent
    the old words."""
    brief = _brief(conn, due=_in(hours=12), prepared=_ago(days=14))
    store.delivered(conn, [brief], "before")

    await prepare.prepare(conn)

    assert doer.asked == [] and _row(conn, brief)["outcome"] == OLD_TEXT


async def test_a_brief_that_said_nothing_is_not_written_again_for_being_old(conn, doer):
    """It is staleness of what was said that this is for; with no text there
    is none, and the doer's turn is paid for out of a day's budget."""
    _brief(conn, due=_in(hours=20), prepared=_ago(days=14), text=None)

    await prepare.prepare(conn)

    assert doer.asked == []


async def test_something_that_is_not_a_meeting_brief_is_not_written_again(conn, doer):
    """A weather note or a reminder task is not made twice for being old."""
    _brief(conn, due=_in(hours=20), prepared=_ago(days=14), source="weather",
           key="weather:rain:e1")

    await prepare.prepare(conn)

    assert doer.asked == []


async def test_a_spent_budget_sends_no_brief_back(conn, doer):
    """A brief sent back that nothing will write would only lose its place."""
    brief = _brief(conn, due=_in(hours=20), prepared=_ago(days=14))
    spend.bill(conn, 0.25, model="m", unit=spend.Unit("background", "work"))

    await prepare.prepare(conn)

    assert doer.asked == []
    assert _row(conn, brief)["prepared_at"] is not None


async def test_a_brief_written_again_that_finds_nothing_goes_out_without_the_old_text(
    conn, doer
):
    """The old text was written weeks ago and may be wrong; the headline,
    which says only what the event is, is the better brief."""
    brief = _brief(conn, due=_in(hours=20), prepared=_ago(days=14))
    doer.replies = [_Reply("NOTHING TO DO")]

    await prepare.prepare(conn)

    assert _row(conn, brief)["outcome"] is None


# --------------------------------------------------------------------------
# Written without the guests, written again when a look lists them
# --------------------------------------------------------------------------


def _look(*guests: str, event: str = EVENT, key: str = f"horizon:{EVENT}:ahead") -> Observation:
    return Observation(source="horizon", key=key, title="Prep Call with Alex",
                       ref={"event_id": event, "people": list(guests)})


async def test_the_brief_for_the_19th_is_rewritten_once_a_look_lists_the_real_alex(conn, doer):
    """The incident: obs 3157 was written with nobody listed, and the event
    came to list aarden@alumni.example."""
    brief = _brief(conn, due=_in(days=10), prepared=_ago(days=3))

    store.record_new(conn, [_look("Alex Arden <aarden@alumni.example>")])

    held = _row(conn, brief)
    assert held["outcome"] is None and held["prepared_at"] is None
    assert "aarden@alumni.example" in held["ref"]
    doer.replies = [_Reply(NEW_TEXT)]
    await prepare.prepare(conn)
    assert "Alex Arden <aarden@alumni.example>: not in the user's records" in doer.asked[0]
    assert _row(conn, brief)["outcome"] == NEW_TEXT


def test_a_brief_sent_back_for_new_guests_waits_for_the_doer_before_it_can_go(conn):
    """The morning briefing may not take it while the doer has it: it would
    carry no text."""
    brief = _brief(conn, due=_in(hours=12), prepared=_ago(hours=40))
    assert [o.id for o in store.for_briefing(conn, briefs_due_by=_in(hours=13))] == [brief]

    store.record_new(conn, [_look("Alex Arden <aarden@alumni.example>")])

    assert store.for_briefing(conn, briefs_due_by=_in(hours=13)) == []
    assert [o.id for o in store.tasks_waiting(conn, 5)] == [brief]


def test_a_look_that_lists_the_same_people_leaves_the_brief_alone(conn):
    """A name filled in later is not a new guest."""
    brief = _brief(conn, due=_in(days=10), prepared=_ago(days=3),
                   guests=["aarden@alumni.example <aarden@alumni.example>"])

    store.record_new(conn, [_look("Alex Arden <aarden@alumni.example>")])

    assert _row(conn, brief)["outcome"] == OLD_TEXT


def test_a_look_that_lists_nobody_leaves_the_brief_alone(conn):
    brief = _brief(conn, due=_in(days=10), prepared=_ago(days=3),
                   guests=["Alex Arden <aarden@alumni.example>"])

    store.record_new(conn, [_look()])

    assert _row(conn, brief)["outcome"] == OLD_TEXT


def test_a_look_at_another_event_leaves_the_brief_alone(conn):
    brief = _brief(conn, due=_in(days=10), prepared=_ago(days=3))

    store.record_new(conn, [
        _look("Sam <sam@northwind.example>", event="another", key="horizon:another:soon"),
    ])

    assert _row(conn, brief)["outcome"] == OLD_TEXT


def test_a_brief_already_sent_or_for_a_meeting_that_has_begun_is_left_alone(conn):
    sent = _brief(conn, due=_in(hours=12), prepared=_ago(hours=40), key="calendar:a")
    store.delivered(conn, [sent], "before")
    begun = _brief(conn, due=_ago(minutes=5), prepared=_ago(hours=40), key="calendar:b")

    store.record_new(conn, [_look("Alex Arden <aarden@alumni.example>")])

    assert _row(conn, sent)["outcome"] == OLD_TEXT
    assert _row(conn, begun)["outcome"] == OLD_TEXT


def test_looking_still_records_what_is_new_when_sending_a_brief_back_fails(conn, monkeypatch):
    """The rows are written before the brief is touched; a failure there
    must not lose them or stop the sources after this one."""

    def broken(_conn, _fresh):
        raise RuntimeError("bad ref")

    monkeypatch.setattr(store, "_restale_briefs", broken)

    fresh = store.record_new(conn, [_look("Alex Arden <aarden@alumni.example>")])

    assert [o.key for o in fresh] == [f"horizon:{EVENT}:ahead"]


# --------------------------------------------------------------------------
# What the doer is told
# --------------------------------------------------------------------------


async def test_the_doer_is_told_to_name_nobody_it_only_guessed_from_a_first_name(conn, doer):
    _brief(conn, due=_in(hours=20), prepared=_ago(days=14))
    doer.replies = [_Reply(NEW_TEXT)]

    await prepare.prepare(conn)

    asked = " ".join(doer.asked[0].split())  # the prompt wraps its lines
    assert 'a who_is match marked "partial"' in asked
    assert "leave that person out" in asked
