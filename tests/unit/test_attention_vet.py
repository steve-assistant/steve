"""The checks in code between the judge and delivery (`agent/attention/vet.py`).

Each test replays an incident from 24 September to 10 October 2026 as rows
in a temporary database: the judge's verdict is what it really said, and
the test pins what code now makes of it. The incidents are the reason the
checks exist; a test that does not replay one is guarding nothing the user saw.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from agent.attention import briefing, deliver, owed, replay, store, told, triage, vet
from agent.attention.sources import mail, weather
from agent.attention.store import Observation
from agent.connectors import Connected
from agent.home import Place
from core import db
from tests.fakes import google as fake_google
from tests.fakes import outlook as fake_outlook

DAY = dt.datetime(2026, 10, 8, 15, 0)
NIGHT = dt.datetime(2026, 10, 8, 23, 3)  # when the Ridgeview warning woke them
ROPES = "40 Ferris Dr, Pittsburgh, PA 15213, United States"
RIDGEVIEW = Place("Ridgeview, Alabama", 31.085, -88.011, "America/Chicago", "US")
HOME = Place("Pittsburgh, Pennsylvania", 40.44062, -79.99589, "America/New_York", "US")
# Each account's mail source, as `sources/mail.py` names it.
GMAIL, OUTLOOK = (f"mail:{key}" for key in (fake_google.KEY, fake_outlook.KEY))
# An account the user added, as the IMAP engine would serve their own domain's mail.
ADDED = "mail:5e1f0a2b3c4d"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    async def no_lead(*_a, **_k):
        return ""

    monkeypatch.setattr(briefing, "lead", no_lead)
    c = db.open_store(tmp_path / "vet.db")
    yield c
    c.close()


def _judged(conn, source, key, *, title=None, detail="", ref=None, say="briefing",
            critical=False, reason="", why="", when=None, vet_it=True) -> Observation:
    """One observation, judged as the judge judged it, then checked."""
    [obs] = store.record_new(conn, [Observation(source, key, title or key, detail=detail,
                                                when=when, ref=ref or {})])
    triage.apply(conn, [obs], [triage.Verdict(key=key, say=say, critical=critical,
                                              headline=title or key, reason=reason, why=why)])
    if vet_it:
        vet.after_judging(conn, [obs.id], now=DAY)
    [obs] = store.get(conn, [obs.id])
    return obs


def _new(conn, source, key, *, title=None, detail="", ref=None) -> Observation:
    """One observation recorded and not yet judged."""
    [obs] = store.record_new(conn, [Observation(source, key, title or key, detail=detail,
                                                ref=ref or {})])
    return next(o for o in store.untriaged(conn, 50) if o.id == obs.id)


def _said(conn, text, *, hours_ago=1, channel="signal"):
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES (?)", (channel,)).lastrowid
        conn.execute("INSERT INTO messages (session_id, role, content, created_at)"
                     " VALUES (?, 'user', ?, datetime('now', ?))",
                     (session, text, f"-{hours_ago} hours"))


def _judged_hours_ago(conn, obs_id, hours):
    with conn:
        conn.execute("UPDATE observations SET triaged_at = datetime('now', ?),"
                     " first_seen = datetime('now', ?) WHERE id = ?",
                     (f"-{hours} hours", f"-{hours} hours", obs_id))


def _vet_row(conn, obs_id):
    return conn.execute("SELECT * FROM verdicts WHERE obs_id = ?", (obs_id,)).fetchone()


class _Notify:
    def __init__(self):
        self.sent: list[str] = []

    async def __call__(self, text):
        self.sent.append(text)
        return True


# ---------------------------------------------------------------------------
# Who sent it
# ---------------------------------------------------------------------------


def test_a_platforms_personal_invitation_from_its_ceo_is_automated_not_a_person():
    """Trey Research's "A Personal Invitation from the Trey Research CEO" came from
    studies@treyresearch.qualtrics.com under Marcus Bell's name, and the
    address test let it through as a person (obs 4385)."""
    sender = vet.sender_class("studies@treyresearch.qualtrics.com", "Marcus Bell")

    assert sender.kind == "automated" and "qualtrics.com" in sender.why


@pytest.mark.parametrize(("kwargs", "kind"), [
    ({"headers": {"list_unsubscribe": "<mailto:leave@x.example.org>"}}, "automated"),
    ({"headers": {"precedence": "bulk"}}, "automated"),
    ({"headers": {"list_id": "<contoso-ai.contoso.example>"}}, "group"),
    ({"headers": {"sorted_as": "automated", "filed_under": "Updates"}}, "automated"),
    ({"headers": {"sorted_as": "group", "filed_under": "Forums"}}, "group"),
    ({"headers": {"sorted_as": "person"}}, "person"),
    ({"recipients": 40}, "group"),
    ({"snippet": "Hi Everyone, We're excited to see you all at the kickoff"}, "group"),
    ({}, "person"),
])
def test_what_a_mailing_system_adds_says_nobody_wrote_it_to_them(kwargs, kind):
    """The structure a list or a platform leaves on a message, strongest
    first; Lena's "Hi Everyone" kickoff to the class (obs 271) became a
    reply the user owed on the greeting alone."""
    assert vet.sender_class("lena@contoso.example", "Lena", **kwargs).kind == kind


def test_fabrikams_thank_you_for_registering_is_dropped_however_the_judge_held_it(conn):
    """Fabrikam's "Thank you for registering" confirmations went into the 7
    October morning briefing (obs 3467/3468): a receipt for something the user did
    themselves has nothing in it to tell them."""
    obs = _judged(conn, ADDED, "mail:fabrikam", ref={
        "from": "Fabrikam <globalrecruiting.noreply@fabrikam.example>",
        "subject": "Fabrikam: Confirmed for 2026 Denver Design Summit"},
        title="Fabrikam: Confirmed for 2026 Denver Design Summit -- from Fabrikam",
        detail="Home, unread. Hi Maya, Thank you for registering for 2026 Denver Design"
               " Summit: Meet the Leadership Team - Virtual.")

    assert obs.say == "never"
    assert "receipt" in _vet_row(conn, obs.id)["vet_why"]


def test_a_payment_reminder_is_not_a_receipt_whatever_the_judges_headline_says(conn):
    """The electric company's reminder (obs 2351) got the headline "... a
    payment confirmation says a payment is processing"; read with the
    judge's words it was a receipt and would have been dropped."""
    obs = _judged(conn, ADDED, "mail:power", say="briefing", reason="money", ref={
        "from": "noreply@billing.gridpower.example", "subject": "Payment Reminder"},
        title="Payment Reminder -- from noreply@billing.gridpower.example",
        detail="Home, already read. Payment Reminder Hello, your Gridpower payment for the"
               " billing date of 09/09/26 is due on 10/07/26.")
    with conn:
        conn.execute("UPDATE observations SET headline = ? WHERE id = ?",
                     ("Gridpower bill due October 7; a payment confirmation says it is processing",
                      obs.id))
    [obs] = store.get(conn, [obs.id])

    assert vet.sender_rule(obs, vet.sender_of(conn, obs), "money") is None


def test_automated_mail_said_now_becomes_fyi_unless_it_changes_their_plans(conn):
    """The maintainer, 10 October: automated mail is never "important" unless it
    changes their plans. 41 of the 68 mail items the user was told came from
    automated senders."""
    notice = _judged(conn, GMAIL, "mail:pkg", say="now", critical=True, reason="deadline",
                     ref={"from": "Parcel Desk <no-reply@parceldesk.example>",
                          "subject": "You have a package!"})
    moved = _judged(conn, GMAIL, "mail:moved", say="now", reason="changes-plans",
                    ref={"from": "CampusGroups <noreply@campusgroups.com>",
                         "subject": "Event moved to 4pm"})

    assert (notice.say, notice.critical, notice.reason) == ("briefing", False, "fyi")
    assert (moved.say, moved.reason) == ("now", "changes-plans")


# ---------------------------------------------------------------------------
# Which message it is
# ---------------------------------------------------------------------------


def test_a_thread_about_a_message_judged_never_as_mail_is_settled_before_the_judge(conn):
    """Trey Research's invitation was judged `never` as mail on 8 October (obs
    3762) and raised again as "Marcus Bell wrote 2 days ago and you haven't
    replied" on the 9th (obs 4385), which went into the evening briefing
    with "Want me to draft a reply?"."""
    mail_row = _judged(conn, OUTLOOK, "mail:trey", say="never", ref={
        "message_id": "AAMk-trey", "from": "Marcus Bell <studies@treyresearch.qualtrics.com>",
        "subject": "A Personal Invitation from the Trey Research CEO"})
    thread = _new(conn, "threads", "thread:trey", ref={
        "message_id": "AAMk-trey", "waiting": "owed by the user",
        "with": "Marcus Bell <studies@treyresearch.qualtrics.com>",
        "subject": "A Personal Invitation from the Trey Research CEO"})

    left = vet.before_judging(conn, [thread])

    [thread] = store.get(conn, [thread.id])
    assert left == [] and thread.say == "never"
    assert f"observation {mail_row.id}" in _vet_row(conn, thread.id)["vet_why"]


def test_one_message_in_two_mailboxes_is_told_once(conn):
    """"RE: Thank You!" reached the own-domain mailbox and Contoso and was told twice in one
    evening briefing (obs 3159/3160)."""
    ref = {"from": "Okafor, Tobi <tobi@contoso.example>", "subject": "RE: Thank You!"}
    words = "Hi Maya, it was great to meet you at the Fabrikam event last week and thank you"
    first = _judged(conn, ADDED, "mail:dh-1", ref=dict(ref, message_id="dh-1"),
                    detail=f"Home, unread. {words}")
    copy = _new(conn, OUTLOOK, "mail:on_outlook-1", ref=dict(ref, message_id="outlook-1"),
                detail=f"Outlook, unread. {words}")

    assert vet.before_judging(conn, [copy]) == []
    assert f"observation {first.id}" in _vet_row(conn, copy.id)["vet_why"]


def test_the_same_alert_ten_days_later_is_a_new_message(conn):
    """LinkedIn's "please verify your new device" opens with the same words
    every time; the fingerprint that joins two copies of one message must
    not join a new alert to one from ten days before (obs 1584 and 3724)."""
    ref = {"from": "LinkedIn <security-noreply@linkedin.com>",
           "subject": "Maya, please verify your new device"}
    words = "Maya, we noticed a sign-in from a new device. Enter this code to verify"
    old = _judged(conn, OUTLOOK, "mail:li-1", ref=dict(ref, message_id="li-1"),
                  detail=f"Outlook, unread. {words}")
    _judged_hours_ago(conn, old.id, 240)
    new = _new(conn, OUTLOOK, "mail:li-2", ref=dict(ref, message_id="li-2"),
               detail=f"Outlook, unread. {words}")

    assert vet.before_judging(conn, [new]) == [new]


# ---------------------------------------------------------------------------
# Where it is
# ---------------------------------------------------------------------------


def _ridgeview_warning(conn, **ref):
    return _judged(
        conn, "weather", "weather:alert:urn:hurricane", say="now", critical=True,
        reason="safety",
        title="Hurricane Warning in effect in Ridgeview, where the user is going (Thu)",
        detail="severity Extreme, urgency Expected; from 2026-10-08T22:00:00-05:00 until"
               " 2026-10-10T12:00:00-05:00",
        ref={"official": "National Weather Service", "severity": "Extreme",
             "urgency": "Expected", "ends": "2030-01-01T00:00:00-05:00", **ref})


def test_a_hurricane_warning_for_an_unverified_trip_never_reaches_them(conn):
    """At 23:03 on 8 October a hurricane warning for Ridgeview, Alabama
    woke them "where she is going" -- a ropes course in Pittsburgh (obs 4126).
    The judge's `critical` was the whole of the night rule."""
    obs = _ridgeview_warning(conn, **{"for": "trip", "trip": "Ridgeview, Alabama",
                                         "trip_verified": False,
                                         "trip_why": "one event, not travel"})
    notify = _Notify()

    import asyncio
    asyncio.run(deliver.deliver(conn, notify=notify, now=NIGHT))

    assert notify.sent == []
    assert (obs.say, obs.critical) == ("never", False)
    assert "not a verified trip" in _vet_row(conn, obs.id)["vet_why"]


def test_a_warning_written_before_places_were_kept_is_checked_by_its_title(conn):
    """The rows from 8 October carry only `official` in their ref; the
    title says where, and nowhere the user is."""
    obs = _ridgeview_warning(conn)

    assert obs.say == "never"


def test_a_three_hour_event_whose_address_says_pennsylvania_is_not_a_trip_to_alabama():
    """The ropes course's own address ended "PA 15213"; the geocoder put
    "United States" in Ridgeview, Alabama."""
    ropes = {"summary": "Contoso ropes course", "location": ROPES,
             "start": "2026-10-09T13:00:00-04:00", "end": "2026-10-09T16:00:00-04:00"}
    dinner = {"summary": "Dinner", "location": "Row 34, Boston, MA",
              "start": "2026-10-03T19:00:00-04:00", "end": "2026-10-03T21:00:00-04:00"}
    boston = Place("Boston, Massachusetts", 42.36, -71.06, "America/New_York")

    from agent.attention import trips

    assert trips.verify(ropes, RIDGEVIEW, "US")[0] is False
    assert trips.verify(dinner, boston, "US") == (
        True, "the address's own state (MA) is Massachusetts")
    assert trips.verify({"summary": "Flight to Chicago", "start": "2026-10-03"},
                        boston, "US")[0] is True


def test_an_unverified_trip_gets_no_weather_and_no_alerts(conn, monkeypatch):
    """Its kept forecast is what the briefing and the night read as where
    the user is; the 9 October briefing carried Ridgeview's."""
    from agent.attention import trips

    asked: list = []
    monkeypatch.setattr(weather, "forecast", lambda place, days=3: asked.append(place) or [])
    monkeypatch.setattr(weather, "alerts", lambda place: asked.append(place) or [])
    guessed = trips.Trip(RIDGEVIEW, DAY.date(), DAY.date(), ["ropes"], "US",
                         verified=False, why="one event")

    assert weather.trip_observations(conn, [guessed], DAY.date(), "us") == []
    assert asked == []


def test_an_alerts_own_area_and_whom_it_is_for_are_recorded():
    alert = {"id": "urn:x", "event": "Coastal Flood Warning", "severity": "Severe",
             "urgency": "Expected", "areaDesc": "Southern Pittsburgh",
             "ends": "2026-09-27T18:00:00-04:00",
             "geocode": {"SAME": ["009009"], "UGC": ["CTZ010"]}}

    [obs] = weather.alert_observations([alert], HOME)

    assert obs.ref == {"official": "National Weather Service", "severity": "Severe",
                       "urgency": "Expected", "same": ["009009"], "ugc": ["CTZ010"],
                       "area": "Southern Pittsburgh", "ends": "2026-09-27T18:00:00-04:00",
                       "for": "home", "home": HOME.name}


# ---------------------------------------------------------------------------
# The night
# ---------------------------------------------------------------------------


def _night(conn) -> list[str]:
    import asyncio

    notify = _Notify()
    asyncio.run(deliver.deliver(conn, notify=notify, now=NIGHT))
    return notify.sent


def test_at_night_what_is_critical_after_the_checks_goes_and_nothing_else(conn):
    """The maintainer, 10 October: at night "only truly important stuff", a little
    looser than emergencies code can verify. The judge's critical, once the
    checks have passed it: a flood warning for home goes; the hurricane
    warning for a town the user was never going to (8 October, 23:03) does not."""
    _judged(conn, "weather", "weather:alert:urn:flood", say="now", critical=True,
            reason="safety", title=f"Coastal Flood Warning in effect at home ({HOME.name})",
            ref={"official": "National Weather Service", "for": "home",
                 "ends": "2030-01-01T00:00:00-04:00"})
    _judged(conn, "weather", "weather:alert:urn:hurricane", say="now", critical=True,
            reason="safety",
            title=f"Hurricane Warning in effect in {RIDGEVIEW.name}, where the user is going",
            ref={"trip": RIDGEVIEW.name, "trip_verified": False, "trip_why": "one event",
                 "ends": "2030-01-01T00:00:00-04:00"})

    [text] = _night(conn)

    assert "Coastal Flood Warning" in text and "Ridgeview" not in text


def test_an_automated_alert_is_never_critical_so_never_wakes_them(conn):
    """Security alerts and the like are for the briefing: the user sees them
    themselves (the maintainer, 10 October)."""
    obs = _judged(conn, GMAIL, "mail:signin", say="now", critical=True, reason="safety",
                  title="New sign-in to your account",
                  ref={"from": "Google <no-reply@accounts.google.com>"})

    assert _night(conn) == []
    [obs] = store.get(conn, [obs.id])
    assert not obs.critical and obs.say == "briefing", "held for the morning, not lost"


# ---------------------------------------------------------------------------
# Whether it is still true
# ---------------------------------------------------------------------------


def test_their_word_that_the_user_is_in_pittsburgh_drops_the_held_ridgeview_items(conn):
    """At 00:24 on 9 October the user wrote "I'm in Pittsburgh"; the 08:00
    briefing still carried three Ridgeview items (4125, 4128, 4143)."""
    rain = _judged(conn, "weather", "weather:trip:ridgeview-alabama:2026-10-09:rain",
                   title="Rain likely in Ridgeview on Fri Oct 9, while the user is there (Fri)",
                   ref={"trip": "Ridgeview, Alabama", "trip_verified": True,
                        "trip_why": "test"}, when="2030-10-09", vet_it=False)
    _judged_hours_ago(conn, rain.id, 8)
    _said(conn, "I'm in Pittsburgh", hours_ago=7)
    [rain] = store.get(conn, [rain.id])

    kept, dropped = vet.recheck(conn, [rain], dt.datetime.now())

    assert kept == [] and "Pittsburgh" in dropped[0][1]
    assert store.get(conn, [rain.id])[0].say == "never"


def test_solve_is_done_drops_tailspin_solve_is_due_today(conn):
    """"Solve is done" at 01:00 on 9 October; the morning briefing still
    said "Tailspin Solve is due today"."""
    due = _judged(conn, "commitments", "commitment:5:due", title="Tailspin Solve is due today",
                  vet_it=False)
    _judged_hours_ago(conn, due.id, 8)
    _said(conn, "Solve is done", hours_ago=7)
    [due] = store.get(conn, [due.id])

    kept, _ = vet.recheck(conn, [due], dt.datetime.now())

    assert kept == []


def test_a_correction_about_something_else_leaves_a_story_that_shares_one_name(conn):
    """"you used the wrong email addresses ... the tailspin format" (29
    September) is about the agent's drafts, not a news story naming
    Tailspin; one shared name is not enough for a bare "that's wrong"."""
    story = _judged(conn, "news", "news:mck", vet_it=False,
                    title="AI May Force 11 Million Workers Into New Jobs, Tailspin Says")
    _judged_hours_ago(conn, story.id, 3)
    _said(conn, "you also used the wrong email addresses for everyone, the tailspin format is"
                " first.last", hours_ago=2)
    [story] = store.get(conn, [story.id])

    kept, _ = vet.recheck(conn, [story], dt.datetime.now())

    assert [o.id for o in kept] == [story.id]


def test_a_commitment_the_user_closed_is_not_said(conn):
    """Commitment 5 was done; nothing looked before saying it was due. Closed
    in the obligations ledger, where commitments live since 10 October."""
    from tools import obligations

    with conn:
        conn.execute("INSERT INTO obligations (id, kind, text, quote, source, status)"
                     " VALUES (5, 'task', 'Complete Tailspin Solve', 'q', 'conversation',"
                     " 'open')")
        conn.execute("INSERT INTO obligations (id, kind, text, quote, source, status)"
                     " VALUES (6, 'task', 'Send the Northwind note', 'q', 'conversation', 'open')")
    obligations.close(conn, 5, by="her words")
    obs = _judged(conn, "commitments", "commitment:5:due", ref={"commitment_id": 5})

    assert owed.closed_refs(conn) == {"commitment:5"}
    assert obs.say == "never" and "commitment 5" in _vet_row(conn, obs.id)["vet_why"]


def test_an_assignment_whose_due_time_has_passed_is_not_said(conn):
    """DES 202's case prep, due Mon 28 Sep 2:35pm, was told after it."""
    obs = _judged(conn, "deadlines", "deadlines:402", when="2026-09-28T14:35:00-04:00",
                  vet_it=False)

    kept, dropped = vet.recheck(conn, [obs], dt.datetime(2026, 9, 28, 20, 0))

    assert kept == [] and "passed" in dropped[0][1]


# ---------------------------------------------------------------------------
# The judge's reason, kept; the briefing as a view; what the user was told
# ---------------------------------------------------------------------------


def test_the_judges_reason_and_sentence_are_kept_and_a_misspelt_code_is_not(conn):
    """`store.judged` threw the verdict's why away, so "why did you tell
    me that?" had no answer."""
    good = _judged(conn, GMAIL, "mail:a", reason="Reply_Owed", why="Rina asked a question",
                   vet_it=False)
    bad = _judged(conn, GMAIL, "mail:b", reason="urgent", vet_it=False)

    assert (good.reason, good.why) == ("reply-owed", "Rina asked a question")
    assert bad.reason is None


async def test_the_briefing_puts_what_needs_user_first_with_its_reason_then_five_fyi(
        conn, monkeypatch):
    from agent import calendars, features

    monkeypatch.setattr(features, "has", lambda _conn, name: name == "calendar")
    monkeypatch.setattr(calendars, "agenda", lambda *a, **k: {"events": []})
    _judged(conn, GMAIL, "mail:due", title="Problem Set 2 due Friday", reason="deadline")
    for n in range(7):
        _judged(conn, "calendar", f"calendar:club{n}", title=f"Club meetup {n}", reason="fyi")
    notify = _Notify()

    await briefing.send(conn, "morning", notify=notify, now=dt.datetime.now())

    text = notify.sent[0]
    assert "Problem Set 2 due Friday (a deadline)" in text
    assert text.index("Needs you:") < text.index("FYI:") < text.index("Today:")
    fyi = text[text.index("FYI:"):text.index("Today:")]
    assert fyi.count("Club meetup") == briefing.MAX_FYI


async def test_every_line_sent_is_recorded_and_explains_itself(conn):
    """On 9 October the user asked "why do you think I'm in Ridgeview?" about a
    ping nothing could look up."""
    obs = _judged(conn, GMAIL, "mail:bill", say="now", reason="money",
                  title="Con Ed bill due today", why="A bill with a stated due date today")
    notify = _Notify()

    await deliver.deliver(conn, notify=notify, now=DAY)

    [row] = conn.execute("SELECT * FROM told").fetchall()
    assert (row["obs_id"], row["how"], row["reason"]) == (obs.id, "ping", "money")
    [why] = told.explain(conn, "con ed")
    assert f"observation {obs.id}" in why and "A bill with a stated due date today" in why
    assert told.explain(conn, obs.id) == [why]


# ---------------------------------------------------------------------------
# What the sources record for the checks
# ---------------------------------------------------------------------------


def _account(name: str, key: str) -> Connected:
    return Connected(key=key, name=name, klass="mail", module="tests", provider=None)


def test_a_gmail_message_keeps_what_the_sender_check_reads():
    message = {"id": "g1", "from": "Contoso AI Club <ai@contoso.example>", "subject": "Meetup",
               "to": "a@contoso.example, b@contoso.example", "cc": "c@contoso.example",
               "labels": ["INBOX", "CATEGORY_FORUMS"], "rfc_id": "<abc@contoso.example>",
               "list_id": "<ai.contoso.example>", "precedence": "list", "snippet": "Hi all",
               "sorted_as": "group", "filed_under": "Forums"}

    ref = mail._observe(message, _account("gmail", fake_google.KEY)).ref

    assert ref["headers"] == {"list_id": "<ai.contoso.example>", "precedence": "list",
                              "sorted_as": "group", "filed_under": "Forums"}
    assert "labels" not in ref, "a provider's own labels are its module's to read"
    assert (ref["recipients"], ref["rfc_id"]) == (3, "<abc@contoso.example>")


def test_outlook_mail_claims_no_recipient_count_it_did_not_read():
    """Contoso's envelopes leave `to` empty for received mail: 0 would be a lie."""
    ref = mail._observe({"id": "y1", "from": "x@contoso.example", "to": ""},
                        _account("outlook", fake_outlook.KEY)).ref

    assert "recipients" not in ref and "headers" not in ref


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


def test_the_replay_reports_and_never_writes_to_the_database_it_reads(tmp_path, capsys):
    path = tmp_path / "copy.db"
    c = db.open_store(path)
    try:
        _ridgeview_warning(c, **{"for": "trip", "trip": "Ridgeview, Alabama"})
        c.execute("DELETE FROM verdicts")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()
    before = path.read_bytes()

    assert replay.main(["--dry-run", "--db", str(path)]) == 0

    assert path.read_bytes() == before
    out = capsys.readouterr().out
    assert "Replayed 1 judged observations" in out and "Night pings" in out


# ---------------------------------------------------------------------------
# Whether it is already done
# ---------------------------------------------------------------------------


def _lunch(conn, *, began_hours_ago=1.5):
    """Lunch with Sam, just ended, not yet judged."""
    began = (dt.datetime.now() - dt.timedelta(hours=began_hours_ago)).astimezone()
    return _new(conn, "after", "after:e1:today", title="Just ended: Lunch with Sam",
                ref={"start": began.isoformat(timespec="seconds"),
                     "end": (began + dt.timedelta(hours=1)).isoformat(timespec="seconds"),
                     "people": ["Sam Ortiz <Sam.Ortiz@northwind.example>"]})


def _wrote(conn, tool, to, *, ago="-10 minutes"):
    with conn:
        conn.execute(
            "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal, status,"
            " created_at) VALUES (?, ?, ?, 'a note', 'private', 'reversible', 'done',"
            " datetime('now', ?))", (tool, json.dumps({"to": to}), f"{tool}{to}{ago}", ago))


@pytest.mark.parametrize("tool", ["draft_email", "send_email"])
def test_a_meeting_followed_up_through_steve_is_not_asked_about(conn, tool):
    """The judge was told in words not to offer a note "after you wrote one",
    and on 1 October 2026 offered one nine minutes after the agent drafted
    it. The draft was in the database."""
    obs = _lunch(conn)
    _wrote(conn, tool, "sam.ortiz@northwind.example")

    assert vet.before_judging(conn, [obs]) == []
    assert "already followed up" in _vet_row(conn, obs.id)["vet_why"]


def test_a_meeting_the_user_followed_up_from_their_own_mail_is_not_asked_about(conn):
    from tools import people

    obs = _lunch(conn)
    person = people.upsert(conn, "sam.ortiz@northwind.example", name="Sam Ortiz")
    people.record_interaction(conn, person, kind="email", direction="sent", source_id="m1",
                              occurred_at=dt.datetime.now().isoformat(timespec="seconds"))

    assert vet.before_judging(conn, [obs]) == []


def test_writing_to_someone_else_or_before_the_meeting_is_no_follow_up(conn):
    obs = _lunch(conn)
    _wrote(conn, "draft_email", "dana@contoso.example")
    _wrote(conn, "draft_email", "sam.ortiz@northwind.example", ago="-3 hours")

    assert [o.id for o in vet.before_judging(conn, [obs])] == [obs.id]


def test_a_question_waiting_to_go_is_dropped_once_the_user_writes_to_them(conn):
    """Judged before the note was written, it was still sent after."""
    obs = _lunch(conn)
    triage.apply(conn, [obs], [triage.Verdict(key=obs.key, say="now",
                                              headline="How did lunch with Sam go?")])
    [obs] = store.get(conn, [obs.id])
    _wrote(conn, "draft_email", "Sam Ortiz <sam.ortiz@northwind.example>", ago="-1 minutes")

    kept, dropped = vet.recheck(conn, [obs], dt.datetime.now())

    assert kept == [] and "draft email to sam.ortiz@northwind.example" in dropped[0][1]


def test_a_trip_alert_records_whether_the_trip_was_verified(monkeypatch):
    from agent.attention import trips

    trip = trips.Trip(RIDGEVIEW, DAY.date(), DAY.date(), ["ropes"], "US",
                      verified=False, why="one event")
    [obs] = weather.alert_observations([{"id": "x", "event": "Hurricane Warning"}],
                                       RIDGEVIEW, trip=trip)

    assert json.loads(json.dumps(obs.ref))["trip_verified"] is False
    assert not vet.location(obs).ok
