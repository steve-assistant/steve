"""The morning briefing and the evening look-ahead.

Two messages a day the user counts on. What matters is that they are exact
where exactness matters (a card's word, a meeting's time), that each goes
out once, that what they carry is marked told only when it arrived, and
that yesterday's news does not come back tomorrow.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from agent.attention import briefing, settings, store, triage
from agent.attention.store import Observation
from core import call_policy, db, spend

MORNING = dt.datetime(2026, 9, 24, 8, 1)
EVENING = dt.datetime(2026, 9, 24, 21, 1)

# Every test here stubs the model's lead line; this is the real one, for
# the test that checks a briefing survives it failing.
REAL_LEAD = briefing.lead


@pytest.fixture
def conn(tmp_path, monkeypatch):
    async def no_lead(*_a, **_k):
        return ""

    monkeypatch.setattr(briefing, "lead", no_lead)
    c = db.open_store(tmp_path / "briefing.db")
    yield c
    c.close()


@pytest.fixture
def calendar(conn, monkeypatch):
    """Their calendar, connected as the app connects one, and its events
    as `calendars.agenda` returns them."""
    from agent import calendars
    from tests.fakes import google as fake_google

    fake_google.signed_in(conn, classes=["calendar"])
    events: list[dict] = []
    monkeypatch.setattr(
        calendars, "agenda", lambda conn, days, limit: {"events": list(events)}
    )
    return events


def _held(conn, key, say="briefing", *, source="gmail", headline=None, outcome=None, **ref):
    [obs] = store.record_new(conn, [Observation(source=source, key=key, title=key, ref=ref)])
    triage.apply(conn, [obs], [triage.Verdict(key=key, say=say, headline=headline or key)])
    if outcome:
        with conn:
            conn.execute(
                "UPDATE observations SET outcome = ?, prepared_at = datetime('now') WHERE id = ?",
                (outcome, obs.id),
            )
    return obs.id


class _Notify:
    """Stands in for `notify_user`: keeps the text and returns its outbox
    row, or cannot write it, like a locked database."""

    def __init__(self, written=True):
        self.written = written
        self.sent: list[str] = []

    async def __call__(self, text):
        if not self.written:
            raise RuntimeError("database is locked")
        self.sent.append(text)
        return len(self.sent)


def _told(conn, obs_id):
    row = conn.execute(
        "SELECT delivered_in FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()
    return row["delivered_in"]


# --------------------------------------------------------------------------
# What it says
# --------------------------------------------------------------------------


async def test_the_day_is_rendered_in_time_order_with_places(conn, calendar):
    calendar += [
        {"id": "a", "summary": "DES 204", "start": "2026-09-24T09:00:00-04:00",
         "location": "Larkspur 2400"},
        {"id": "b", "summary": "Lunch with Sam", "start": "2026-09-24T11:30:00-04:00"},
        {"id": "c", "summary": "Tomorrow's thing", "start": "2026-09-25T10:00:00-04:00"},
    ]
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=MORNING)

    text = notify.sent[0]
    assert "9am  DES 204 (Larkspur 2400)" in text
    assert "11:30am  Lunch with Sam" in text
    assert "Tomorrow's thing" not in text


async def test_a_user_with_no_calendar_is_not_told_their_day_is_empty(conn):
    """With no calendar connected the briefing knew nothing of the day and
    said "nothing on the calendar", which reads as a free day. Without a
    calendar the day is left out."""
    _held(conn, "gmail:1", headline="Rina wrote about the lease")
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=MORNING)

    [text] = notify.sent
    assert "Rina wrote about the lease" in text
    assert "Today:" not in text and "nothing on the calendar" not in text


async def test_an_account_steve_could_not_check_is_named_so_quiet_is_not_read_as_nothing(conn):
    """A briefing that said nothing of a signed-out mailbox read as a quiet
    morning; on 26 September the user learned of a lapsed sign-in from a
    calendar request instead. The next briefing names it, in its health's
    sentence."""
    from core import health, modules

    modules.register(conn)
    key = modules.add_account(conn, module_id="imap", name="Work", classes=["mail"],
                              addresses=["maya@work.example"])
    health.record(conn, key, kind="sign_in")
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=MORNING)

    [text] = notify.sent
    assert ("Could not check:\n  · Work needs you: sign in again, in Steve's app, "
            "under Settings: Accounts.") in text


async def test_a_multiday_camping_trip_keeps_tomorrow_from_reading_as_free(conn, calendar):
    """An all-day trip already underway still occupies tomorrow's look-ahead."""
    calendar.append({"id": "camping", "summary": "Camping trip", "start": "2026-09-24",
                     "end": "2026-09-27", "all_day": True})
    notify = _Notify()

    await briefing.send(conn, "evening", notify=notify, now=EVENING)

    assert "Camping trip" in notify.sent[0]
    assert "nothing on the calendar" not in notify.sent[0]


def test_event_date_ranges_occupy_their_interior_dates_not_the_exclusive_end():
    """Calendar date ranges end exclusively, or a finished trip invents an extra day."""
    events = [
        {"id": "camping", "summary": "Camping trip", "start": "2026-09-24",
         "end": "2026-09-27", "all_day": True},
        {"id": "overnight", "summary": "Overnight train", "start": "2026-09-24T23:00:00-04:00",
         "end": "2026-09-25T02:00:00-04:00"},
        {"id": "late", "summary": "Late shift", "start": "2026-09-24T18:00:00-04:00",
         "end": "2026-09-25T00:00:00-04:00"},
    ]

    assert briefing._agenda(events, dt.date(2026, 9, 25), {})
    assert briefing._agenda(events[:1], dt.date(2026, 9, 27), {}) == []
    # Still under way after midnight, it is on the next day at its start time;
    # one that ends at midnight is not.
    assert briefing._agenda(events[1:], dt.date(2026, 9, 25), {}) == ["   11pm  Overnight train"]


async def test_things_are_sorted_by_whether_they_need_them(conn, calendar):
    _held(conn, "mail:bill", "now", headline="Con Ed bill due today")
    _held(conn, "mail:club", headline="Contoso AI Club meetup Thursday 6pm")
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=MORNING)

    text = notify.sent[0]
    assert text.index("Needs you:") < text.index("Con Ed") < text.index("FYI:")
    assert text.index("FYI:") < text.index("Contoso AI Club") < text.index("Today:")


async def test_what_was_done_about_it_is_said_under_it(conn, calendar):
    _held(conn, "mail:rina", "now", headline="Rina asks if you are in DES 203",
          outcome="Drafted a reply confirming it (Outlook drafts)")
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=MORNING)

    assert "Rina asks if you are in DES 203\n      Drafted a reply" in notify.sent[0]


async def test_no_more_than_three_headlines_and_the_rest_are_dropped(conn, calendar):
    """Yesterday's news carried into tomorrow's briefing is not news."""
    ids = [_held(conn, f"news:{n}", source="news", headline=f"story {n}") for n in range(5)]
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=MORNING)

    assert notify.sent[0].count("story") == 3
    assert [_told(conn, i) for i in ids] == ["morning"] * 3 + ["dropped"] * 2


async def test_an_early_meetings_brief_is_folded_into_the_morning(conn, calendar):
    """A brief due at 8:40 for a 9:25 lecture is read over breakfast, not
    buzzed forty minutes later -- and its prep goes under the meeting."""
    # Years ahead, so the brief is in the future by any real clock and only
    # the briefing's own `now` can bring it in.
    start = dt.datetime(2030, 1, 10, 9, 25).astimezone()
    calendar.append({"id": "ev9", "summary": "Accounting", "start": start.isoformat()})
    [obs] = store.record_new(conn, [Observation(
        source="horizon", key="horizon:ev9:soon", title="Accounting",
        when=start.isoformat(), ref={"event_id": "ev9"},
    )])
    triage.apply(conn, [obs], [triage.Verdict(key=obs.key, say="before", headline="Accounting")])
    with conn:
        conn.execute(
            "UPDATE observations SET outcome = 'Problem set 2 is due at the start',"
            " prepared_at = datetime('now') WHERE id = ?", (obs.id,),
        )
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=MORNING.replace(
        year=start.year, month=start.month, day=start.day))

    assert "Accounting\n         prep: Problem set 2 is due at the start" in notify.sent[0]
    assert _told(conn, obs.id) == "morning"


async def test_what_the_night_learned_is_said_in_the_morning_even_from_an_older_report(
        conn, calendar):
    """The night's report is read back from its job, and one written by an
    older Steve, with a section this one no longer has (the tools it gave
    itself, before step 6), still reads rather than costing the morning
    its night."""
    report = {
        "started_at": "03:00", "learned": ["her advisor is Mira"], "granted": ["units"],
        "improvement_paused": "paused by Maya", "minutes": 12.0,
    }
    with conn:
        conn.execute(
            "INSERT INTO jobs (kind, prompt, status, result) VALUES ('night', 'x', 'done', ?)",
            (json.dumps(report),),
        )
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=dt.datetime.now().replace(hour=8))

    text = notify.sent[0]
    assert "Overnight:\n  · Learned 1 thing: her advisor is Mira." in text
    assert "units" not in text and "REVOKE" not in text


def test_the_night_comes_after_what_is_worth_knowing_and_the_day_and_its_weather_come_last():
    """A briefing is read at its top and its bottom: the maintainer, 19
    October 2026, asked for the day and its weather at the end, and the
    night's report in the middle, where it was last."""
    facts = briefing.Facts(
        kind="morning", about=dt.date(2026, 10, 20), agenda=["  9am  Standup"],
        weather=["62°F, rain after 3pm"], waiting=["  · the draft to Dana"],
        unchecked=["Work mail is signed out."], owed=["Reply to Priya"],
        due=["Rent, Friday"], night=["Learned 1 thing: her advisor is Mira"],
        goals=["  · Find a dentist: 2 of 4 steps"], news_summary="- A bridge reopened",
        spending=["Background work paused at 3:10pm."])

    text = briefing.render(facts, lead="A wet afternoon.")
    heads = [part.split("\n")[0].split(":")[0] for part in text.split("\n\n")[2:]]

    assert heads == ["Waiting on you", "Could not check", "Needs you", "Overnight",
                     "Due soon", "Goals", "Spending", "In the news", "Today", "Weather"]


async def test_the_nights_report_is_a_line_each_with_the_number_that_drops_a_fact_it_kept(
        conn, calendar):
    """Overnight was one line of clauses joined by semicolons, each fact
    noted about the user followed by "(FORGET MEMORY 12)" and the whole by
    "(FORGET MEMORY <number> drops one)". A line each now, the facts'
    numbers before them, and the way to drop one said once. A report from
    an earlier night carries its numbers the same way, so reads the same."""
    report = {
        "started_at": "03:00", "minutes": 12.0,
        "remembered": ["She is training for a half marathon (FORGET MEMORY 12)",
                       "Her sister Ana lives in Lisbon (FORGET MEMORY 13)"],
        "learned": ["the gym closes at 9", "Ana's birthday is in May", "rent went up"],
        "researched": ["running shoes", "Lisbon flights", "marathon plans"],
        "built": ["a tide table"],
        "improvement_paused": "self-upgrade is paused: off by default",
    }
    with conn:
        conn.execute(
            "INSERT INTO jobs (kind, prompt, status, result) VALUES ('night', 'x', 'done', ?)",
            (json.dumps(report),),
        )
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=dt.datetime.now().replace(hour=8))

    [overnight] = [part for part in notify.sent[0].split("\n\n") if part.startswith("Overnight")]
    assert overnight.split("\n") == [
        "Overnight:",
        "  · Noted about you: (12) She is training for a half marathon; (13) Her sister Ana"
        " lives in Lisbon. To drop one, send FORGET MEMORY and its number.",
        "  · Learned 3 things: the gym closes at 9; Ana's birthday is in May; rent went up.",
        "  · Read about running shoes, Lisbon flights and marathon plans.",
        "  · Built: a tide table.",
        "  · Made no changes to itself: self-upgrade is paused.",
    ]


async def test_a_briefing_goes_without_its_lead_when_the_model_fails(conn, calendar, monkeypatch):
    import agent.llm

    async def down(*_a, **_k):
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(briefing, "lead", REAL_LEAD)
    monkeypatch.setattr(agent.llm, "run_agent", down)
    notify = _Notify()

    result = await briefing.send(conn, "morning", notify=notify, now=MORNING)

    assert result["sent"] and notify.sent[0].startswith("Good morning.")


# --------------------------------------------------------------------------
# When, and once
# --------------------------------------------------------------------------


def test_the_morning_is_due_from_eight_until_noon_once(conn):
    assert briefing.due(conn, MORNING) == "morning"
    assert briefing.due(conn, MORNING.replace(hour=7, minute=59)) is None
    assert briefing.due(conn, MORNING.replace(hour=12, minute=5)) is None

    briefing._mark_sent(conn, "morning", MORNING.date())

    assert briefing.due(conn, MORNING.replace(hour=9)) is None


def test_the_evening_is_due_at_nine(conn):
    assert briefing.due(conn, EVENING) == "evening"
    assert briefing.due(conn, EVENING.replace(hour=20)) is None


def test_no_briefing_goes_in_quiet_hours(conn, monkeypatch):
    """The evening's window ran to midnight, so a laptop that woke at 23:00
    sent the look-ahead an hour into the user's quiet hours."""
    assert briefing.due(conn, EVENING.replace(hour=23)) is None
    monkeypatch.setattr(call_policy, "QUIET_FROM", dt.time(23, 30))
    assert briefing.due(conn, EVENING.replace(hour=23)) == "evening"


async def test_an_evening_look_ahead_after_midnight_is_about_the_day_after_its_evening(
        conn, calendar, monkeypatch):
    """Set for 23:00 and sent at 00:30, it is still the evening of the 24th,
    about the 25th: by the clock it went at, it was about the 26th, and was
    recorded as the 25th's, so the 25th's evening never went."""
    monkeypatch.setattr(settings, "EVENING", dt.time(23, 0))
    monkeypatch.setattr(call_policy, "QUIET_FROM", dt.time(1, 0))
    gathered = []
    real = briefing.gather
    monkeypatch.setattr(briefing, "gather", lambda conn, kind, now, about=None: gathered.append(
        about) or real(conn, kind, now, about))
    late = dt.datetime(2026, 9, 25, 0, 30)

    assert briefing.due(conn, late) == "evening"
    await briefing.send(conn, "evening", notify=_Notify(), now=late)

    assert gathered == [dt.date(2026, 9, 25)]
    assert briefing.sent_on(conn, "evening") == "2026-09-24"


async def test_a_briefing_the_outbox_could_not_take_marks_nothing(conn, calendar):
    """Marked sent before it was written, a briefing that never went would
    not be tried again, and what it carried would be lost with it."""
    obs_id = _held(conn, "mail:club")

    with pytest.raises(RuntimeError):
        await briefing.send(conn, "morning", notify=_Notify(written=False), now=MORNING)

    assert _told(conn, obs_id) is None
    assert briefing.due(conn, MORNING) == "morning"  # it tries again


async def test_a_briefing_goes_to_the_outbox_only_until_it_is_too_late_to_read(
        conn, calendar, monkeypatch):
    """A morning briefing that reaches the phone at lunch is worse than none:
    the outbox stops trying it when `due` would stop writing it."""
    from agent.worker import links

    _held(conn, "mail:club")
    asked: list[dict] = []

    async def notify_user(text, **kw):
        asked.append(kw)
        return 1

    monkeypatch.setattr(links, "notify_user", notify_user)
    await briefing.send(conn, "morning", now=MORNING)

    [kw] = asked
    assert kw == {"kind": "briefing", "until": briefing.set_for("morning", MORNING)
                  + briefing.LATE_BY}


async def test_a_sent_briefing_marks_what_it_carried(conn, calendar):
    obs_id = _held(conn, "mail:club")

    await briefing.send(conn, "evening", notify=_Notify(), now=EVENING)

    assert _told(conn, obs_id) == "evening"
    assert briefing.due(conn, EVENING) is None


async def test_a_cap_background_work_reached_is_a_line_in_the_next_briefing_once(
        conn, calendar):
    """Stopped quietly where it was, it is said here, in code: the lead's
    writer may be what the cap stopped. Told only once it is in the outbox."""
    from agent import caps

    spend.bill(conn, 2.50, model="m", unit=spend.Unit("background"))
    with spend.spending("background", conn=conn):
        assert spend.may_spend(conn)

    with pytest.raises(RuntimeError):
        await briefing.send(conn, "morning", notify=_Notify(written=False), now=MORNING)
    assert len(caps.for_the_briefing(conn)) == 2  # the warning and the stop, untold
    notify = _Notify()
    await briefing.send(conn, "morning", notify=notify, now=MORNING)

    [text] = notify.sent
    [line] = [ln for ln in text.split("\n\n") if ln.startswith("Spending:")]
    assert line.startswith("Spending:\n  · Background work paused at ")
    assert line.endswith("its $2.50 daily limit was reached.")
    assert caps.for_the_briefing(conn) == []


async def test_an_empty_tomorrow_sends_nothing_in_the_evening(conn, calendar):
    """A look-ahead at an empty day is a buzz for nothing."""
    notify = _Notify()

    result = await briefing.send(conn, "evening", notify=notify, now=EVENING)

    assert notify.sent == [] and not result["sent"]
    assert briefing.due(conn, EVENING) is None  # decided, not retried every 5 minutes


def test_the_next_briefing_is_the_nearer_of_the_two():
    assert briefing.seconds_until_next(dt.datetime(2026, 9, 24, 20, 0)) == 3600
    assert briefing.seconds_until_next(dt.datetime(2026, 9, 24, 22, 0)) == 10 * 3600


async def test_the_morning_says_what_went_wrong_yesterday(conn, calendar):
    """The failure ledger's line: a bad day is seen the next morning, not
    found later by reading logs."""
    from agent import issues

    for _ in range(3):
        issues.record(conn, "slow_turn", "converse", "A reply took 303s")
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=dt.datetime.now().replace(hour=8))

    assert "  · Noticed about itself: 3 slow replies." in notify.sent[0]


async def test_a_briefing_asks_about_what_has_gone_quiet_on_their_list(conn, calendar):
    """The maintainer, 10 October 2026: reach out more, and ask when it needs them.
    A task days past its date with no sign it was done is asked about in
    the next briefing, with a card the user answers in a word."""
    from agent import approvals
    from tools import obligations

    stale = obligations.add(conn, kind="task", text="Send Zoe the doctor's note",
                            source="conversation", due_date="2026-01-05")
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=MORNING)

    [card] = approvals.pending(conn)
    text = notify.sent[0]
    needs = text.split("Needs you:")[1]
    assert "Send Zoe the doctor's note" in needs and f"YES {card.token}" in needs
    assert obligations.get(conn, stale).status == "asked"
