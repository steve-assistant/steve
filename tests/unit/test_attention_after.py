"""After a meeting: ask how it went, and offer the thank-you note.

The failure worth fearing here is the nag -- "how did Basics of Economics
go?" three times a week -- because a notifier that nags is muted. So these
pin which events count as over and worth asking about (one-off, timed,
short, finished), and that each is asked about once. That nothing is drafted
before the user answers is the frozen gate test's
(`tests/gate/test_attention_bounds.py`).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from agent import calendars
from agent.attention import deliver, sources
from agent.attention.sources import calendar as cal_source
from agent.attention.store import Observation
from tests.gate.decisions import install

NY = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 9, 28, 17, 30, tzinfo=NY)


def _event(start_h, end_h, **kw):
    day = "2026-09-28"
    return {"id": kw.pop("id", "e1"),
            "summary": kw.pop("summary", "Tailspin & Company Coffee Chat"),
            "start": f"{day}T{start_h}-04:00", "end": f"{day}T{end_h}-04:00", **kw}


@pytest.mark.parametrize(("event", "kept"), [
    (_event("16:00:00", "16:30:00"), True),
    (_event("16:00:00", "16:30:00", recurring=True), True),  # the user's setting decides
    (_event("16:00:00", "18:00:00"), False),  # still going
    (_event("09:00:00", "16:00:00"), False),  # a seven-hour block is not a meeting
    (_event("16:00:00", "16:30:00", status="cancelled"), False),
    ({"id": "b", "summary": "Birthday", "start": "2026-09-28", "end": "2026-09-29",
      "all_day": True}, False),
    ({"id": "n", "summary": "No zone", "start": "2026-09-28T16:00:00",
      "end": "2026-09-28T16:30:00"}, False),
    (_event("16:00:00", "16:00:00"), False),  # a deadline, not a meeting
])
def test_only_a_finished_meeting_of_meeting_length_is_asked_about(event, kept):
    """A recurring meeting is kept here: it was dropped, below the user's
    setting, so "after every meeting" never asked about a standing one.
    Whether it is worth asking is the judged mode's to say."""
    found = calendars.finished([event], NOW, longest=dt.timedelta(hours=4))

    assert bool(found) is kept


def test_a_meeting_that_ended_is_asked_about_once(conn, monkeypatch):
    events = [_event("16:00:00", "16:30:00", attendees=[
        {"email": "maya@contoso.example", "self": True},
        {"email": "nina@tailspin.example", "name": "Nina D"},
    ])]
    monkeypatch.setattr(calendars, "ended", lambda *a, **k: {"events": [dict(e) for e in events]})
    source = sources.Source("after", cal_source.after)

    [first] = sources.collect(conn, source)
    from agent.attention import store

    store.record_new(conn, [first])
    again = sources.collect(conn, source)

    assert first.key == "after:e1:2026-09-28"
    assert first.title == "Just ended: Tailspin & Company Coffee Chat"
    assert first.detail.startswith("lasted 30 min")
    assert first.ref["people"] == ["Nina D <nina@tailspin.example>"]
    assert again == []


def test_a_meeting_the_user_copied_onto_their_calendar_does_not_read_as_their_own_routine():
    """Every event on their calendar has them as organiser. Shown that, the
    judge filed their Northwind coffee chat with the routine events the user makes
    themselves and never asked about it."""
    mine = _event("16:00:00", "16:30:00", organizer="steve.test.user@gmail.com",
                  organizer_self=True)
    theirs = _event("16:00:00", "16:30:00", organizer="nina@tailspin.example")

    assert "organiser" not in cal_source._where(mine)
    assert "organiser nina@tailspin.example" in cal_source._where(theirs)


def test_the_question_names_who_was_there_so_their_answer_has_a_recipient():
    obs = Observation(source="after", key="k", title="Just ended: Coffee chat", say="now",
                      headline="How did the Tailspin coffee chat go? Want a thank-you note?",
                      ref={"people": ["Nina D <nina@tailspin.example>"]})

    text = deliver.line_for(obs, NOW.replace(tzinfo=None))

    assert text == ("How did the Tailspin coffee chat go? Want a thank-you note?\n"
                    "  with Nina D <nina@tailspin.example>")


def test_the_source_is_checked_every_quarter_hour():
    names = {s.name: s for s in sources.all_sources()}

    assert names["after"].every_s == 15 * 60


# --------------------------------------------------------------------------
# The user's choice (`settings.FOLLOW_UP_AFTER`): never, every meeting, or
# those a decision model judges worth it
# --------------------------------------------------------------------------


def _one_ended(monkeypatch, **kw):
    event = _event("16:00:00", "16:30:00", summary=kw.pop("summary", "Lunch with Sam"),
                   description="Bring the lease", attendees=[
                       {"email": "maya@example.com", "self": True},
                       {"email": "sam@northwind.example", "name": "Sam Ortiz"}], **kw)
    monkeypatch.setattr(calendars, "ended", lambda *a, **k: {"events": [dict(event)]})


def test_with_follow_ups_off_no_meeting_is_looked_at_and_the_judge_hears_of_none(
        conn, monkeypatch):
    """Off means off: no calendar read every quarter hour for nothing, and
    no paragraph telling the judge to ask how meetings went."""
    from agent import features
    from agent.attention import settings, triage

    monkeypatch.setattr(settings, "FOLLOW_UP_AFTER", "off")
    monkeypatch.setattr(features, "enabled", lambda c: features.everything())
    monkeypatch.setattr(calendars, "ended",
                        lambda *a, **k: pytest.fail("no meeting is looked at"))

    watching = {s.name for s in sources.watching(conn)}

    assert "after" not in watching and "calendar" in watching
    assert cal_source.after(conn) == []
    assert "**After**" not in triage.judge_prompt(watching)


def test_with_every_meeting_chosen_each_is_asked_about_without_a_decision(conn, monkeypatch):
    from agent.attention import settings

    monkeypatch.setattr(settings, "FOLLOW_UP_AFTER", "every")
    jev = install(monkeypatch)
    _one_ended(monkeypatch, recurring=False)

    [obs] = cal_source.after(conn)

    assert obs.title == "Just ended: Lunch with Sam"
    assert "follow_up" not in obs.ref and jev.asked == []


def test_judged_settles_a_recurring_meeting_without_asking_the_model(conn, monkeypatch):
    """The same meeting each week is not worth a question each week, and
    saying so needs no model call."""
    from agent.attention import settings

    monkeypatch.setattr(settings, "FOLLOW_UP_AFTER", "judged")
    jev = install(monkeypatch)
    _one_ended(monkeypatch, recurring=True)

    [obs] = cal_source.after(conn)

    assert (obs.ref["follow_up"], obs.ref["follow_up_why"]) == (
        "not worth asking", "a recurring meeting")
    assert jev.asked == []


@pytest.mark.parametrize(("p", "said"), [(0.8, "worth asking"), (0.2, "not worth asking")])
def test_judged_asks_jev_and_records_its_answer_and_its_bill(conn, monkeypatch, p, said):
    """Asked on the attention loop's connection, so the ask is billed where
    every other of its calls is."""
    from agent import facts
    from agent.attention import settings

    monkeypatch.setattr(settings, "FOLLOW_UP_AFTER", "judged")
    jev = install(monkeypatch, follow_up=p)
    facts.add(conn, facts.USER, "occupation", "product designer", source="confirmed")
    _one_ended(monkeypatch)

    [obs] = cal_source.after(conn)

    assert obs.ref["follow_up"] == said
    assert f"{p:.2f}" in obs.ref["follow_up_why"]
    [payload] = jev.asked
    assert payload["questions"] == {"follow_up": {
        "type": "noul", "instructions": cal_source.FOLLOW_UP_QUESTION}}
    assert [r[0] for r in conn.execute("SELECT model FROM usage_events")] == [
        "typesafe/jev-1.13"]


def test_the_decision_is_shown_the_meetings_shape_and_nothing_of_whats_in_it(monkeypatch):
    """What it judges is the meeting's shape and what the user does, as
    they put it -- not a list of the meeting kinds of one life, and not the
    description or who was there, which are the user's and the guests'."""
    jev = install(monkeypatch, follow_up=0.9)
    event = _event("16:00:00", "16:30:00", summary="Lunch with Sam",
                   description="Bring the lease", attendees=[
                       {"email": "maya@example.com", "self": True},
                       {"email": "sam@northwind.example", "name": "Sam Ortiz"}])

    assert cal_source.worth_asking(event, "product designer") == (
        True, "a decision model gave it 0.90")

    [state] = jev.inputs
    assert "product designer" in state and "Lunch with Sam" in state
    assert "lasted 30 min" in state and "Others invited: 1" in state
    assert "lease" not in state and "northwind" not in state and "Ortiz" not in state


@pytest.mark.parametrize("why", ["no key", "Jev down", "no answer"])
def test_a_decision_that_cannot_be_had_is_no_follow_up(monkeypatch, why):
    """A question nobody wanted costs more than one not asked."""
    from core import secrets

    if why == "no key":
        monkeypatch.setattr(secrets, "get", lambda name: None)
        jev = install(monkeypatch, follow_up=0.9)
    elif why == "Jev down":
        jev = install(monkeypatch, down=RuntimeError("HTTP 503"))
    else:
        jev = install(monkeypatch)

    worth, said = cal_source.worth_asking(_event("16:00:00", "16:30:00"))

    assert worth is False and said == {
        "no key": "not asked: no OpenRouter key", "Jev down": "not asked: RuntimeError: HTTP 503",
        "no answer": "not asked: follow_up: not answered"}[why]
    assert len(jev.asked) == (0 if why == "no key" else 1)


def test_one_judged_not_worth_asking_is_settled_before_the_judge_and_never_again(
        conn, monkeypatch):
    """Recorded with why, so the decision can be read later, and never shown
    to the judge, which would be asked to second-guess it; its key is kept,
    so the next quarter hour does not ask the decision model again."""
    from agent.attention import settings, store, vet

    monkeypatch.setattr(settings, "FOLLOW_UP_AFTER", "judged")
    jev = install(monkeypatch, follow_up=0.1)
    _one_ended(monkeypatch)

    store.record_new(conn, cal_source.after(conn))
    left = vet.before_judging(conn, store.untriaged(conn, 10))

    assert left == []
    assert conn.execute("SELECT say FROM observations").fetchone()[0] == "never"
    assert cal_source.after(conn) == [] and len(jev.asked) == 1


def test_judged_without_a_decision_model_looks_at_nothing_and_the_judge_hears_of_none(
        conn, monkeypatch):
    """The default needs a decision model. Without one it read the calendar
    every quarter hour, asked about nothing, and left the judge a paragraph
    about questions that never came."""
    from agent import features
    from agent.attention import settings, triage
    from core import secrets

    monkeypatch.setattr(settings, "FOLLOW_UP_AFTER", "judged")
    monkeypatch.setattr(features, "enabled", lambda c: features.everything())
    monkeypatch.setattr(secrets, "get", lambda name, default=None: None)

    watching = {s.name for s in sources.watching(conn)}

    assert "after" not in watching and "calendar" in watching
    assert "**After**" not in triage.judge_prompt(watching)

    monkeypatch.setattr(secrets, "get", lambda name, default=None: "sk-or-test")
    assert "after" in {s.name for s in sources.watching(conn)}
