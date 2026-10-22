"""What needs the user comes as one list of notes, each answered once.

Cards, a reminder going off, a cap reached, a sign-in ended and Signal
unlinked were each in a different place in the app, or in none, and a
reminder scrolled away under updates (agent/worker/needs.py, its table in
db/migrations/0015_needs.sql). The clock is pinned to Chicago: CDT, five
hours behind UTC, all through October 2026.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.applications import Starlette

from agent import approvals, outbox, reminders, runtime, sqltime
from agent.attention import settings
from agent.worker import needs
from core import clock, db, health, modules, spend

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)  # 09:00 in Chicago


@pytest.fixture(autouse=True)
def chicago(monkeypatch):
    monkeypatch.setattr(clock, "local_zone", lambda: ZoneInfo("America/Chicago"))


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    monkeypatch.setattr(runtime, "signal_state", {"state": "ready"})
    yield c
    c.close()


def _card(conn, preview: str, *, tool: str = "send_email", token: str = "MAPLE",
          status: str = "pending", account: str | None = None,
          expires: str = "2026-10-15T21:00:00", made: str = "2026-10-15 13:58:00") -> int:
    return conn.execute(
        "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal, status,"
        " token, expires_at, account, created_at) VALUES (?, '{}', ?, ?, 'outward',"
        " 'irreversible', ?, ?, ?, ?, ?)",
        (tool, f"hash-{preview}", preview, status, token, expires, account, made)).lastrowid


def _going_off(conn, text: str, at: datetime, *, kind: str = "remind",
               every: int = 0) -> tuple[int, int]:
    """A reminder booked for `at`, its message gone out then, and its firing
    recorded, as the background loop does."""
    local = at.astimezone(clock.local_zone()).replace(tzinfo=None)
    booked = reminders.schedule(
        conn, kind=kind, text=text, when=local.isoformat(timespec="seconds"),
        every_minutes=every, until=(local + timedelta(hours=23)).isoformat() if every else "",
        now=local - timedelta(minutes=5))
    sent = outbox.add(conn, text, kind="reminder", reminder_id=booked.id, now=at)
    reminders.advance(conn, booked, now=local)
    return booked.id, sent


def _ids(found: list[dict]) -> list[str]:
    return [note["id"] for note in found]


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def test_a_card_waiting_is_a_note_with_its_word_and_the_whole_card(conn):
    """A yes the user owes, with the word that answers it from the phone,
    and everything the yes agrees to, as the phone has it. The note showed
    the card's second line alone: a send's card names its account first,
    so the app asked its yes without the subject, the Cc or the Bcc (19
    October 2026, step 10's mail scenario). The body is never on a card
    (`approvals.quoted_body`), but in the draft, where it is read."""
    card = _card(conn, "Send from your Mailbox account\nTo: dana@example.com\n"
                       "Bcc: sam@example.com\nSubject: Re: Saturday\n\n"
                       "The full email is in your Mailbox Drafts.")
    _card(conn, "Already answered", status="done", token="OAK")

    [note] = needs.needs(conn, NOW)

    assert note == {"kind": "yes", "id": f"card:{card}", "action_id": card,
                    "text": "Send from your Mailbox account",
                    "detail": "To: dana@example.com\nBcc: sam@example.com\n"
                              "Subject: Re: Saturday\nThe full email is in your Mailbox Drafts.",
                    "word": "MAPLE", "at": "2026-10-15T13:58:00Z",
                    "expires": "2026-10-16T02:00:00Z", "actions": ["yes", "no"],
                    "seen": approvals.get(conn, card).args_hash}


def _grouped(conn, previews: list[str], *, tool: str = "delete_draft", token: str = "MAPLE") -> int:
    """One card for several items, as `approvals.held` joins a turn's."""
    group = [{"args": {"draft_id": f"mail:gmail:d{n}"}, "preview": preview}
             for n, preview in enumerate(previews, 1)]
    card = _card(conn, approvals.listing(group), tool=tool, token=token)
    conn.execute("UPDATE actions SET args_json = ? WHERE id = ?",
                 (json.dumps({approvals.GROUPED: group}), card))
    return card


def test_a_card_of_several_items_lists_each_so_a_yes_can_leave_some_out(conn):
    """Its own text promised a yes to some of them in the app, and the note
    showed the first item alone, with a yes for all. Each item is whole:
    by its first line, two emails from one account read the same ("Send
    from your Mailbox account"), and which to untick was a guess. A card
    whose code was bound to every item is approved together, and says so."""
    _grouped(conn, ["Send from your Mailbox account\nTo: iris@example.com\nSubject: Plot fees",
                    "Send from your Mailbox account\nTo: theo@example.com\nSubject: Seed order"],
             tool="send_draft")
    _grouped(conn, ["Pay the plumber $180?", "Pay the painter $90?"], tool="purchase", token="OAK")

    yes, code = sorted(needs.needs(conn, NOW), key=lambda n: n["kind"], reverse=True)

    assert (yes["kind"], yes["text"], yes["detail"]) == (
        "yes", "2 on one card: untick any to leave out", None)
    assert yes["items"] == [
        "Send from your Mailbox account\nTo: iris@example.com\nSubject: Plot fees",
        "Send from your Mailbox account\nTo: theo@example.com\nSubject: Seed order"]
    assert (code["kind"], code["text"]) == ("code", "2 on one card, approved together")
    assert code["items"] == ["Pay the plumber $180?", "Pay the painter $90?"]


def test_a_card_that_spends_money_asks_for_more_than_a_yes(conn):
    """A high-risk card is its own kind of note: the app asks for the second
    factor before it sends the yes."""
    card = _card(conn, "Buy two tickets to Lisbon\n$412.00 to TAP", tool="purchase")
    assert [(n["kind"], n["id"]) for n in needs.needs(conn, NOW)] == [("code", f"card:{card}")]


def test_a_card_for_an_account_that_is_off_is_no_note(conn):
    """Switching an account off withdraws its cards; one asked in the moment
    between must not ask the user for a yes that would act on it."""
    _card(conn, "Archive 40 newsletters", account="acct-gone")
    assert needs.needs(conn, NOW) == []


def test_a_reminder_going_off_is_a_note_until_done_and_a_check_is_not(conn):
    """A reminder is due now, and scrolled away under the morning's updates.
    A check's answer is an update: nothing is due."""
    _, sent = _going_off(conn, "Call the landlord", NOW - timedelta(minutes=10))
    _going_off(conn, "Is the 5:10 train on time?", NOW - timedelta(minutes=5), kind="check")
    _going_off(conn, "Last week's", NOW - timedelta(days=8))

    [note] = needs.needs(conn, NOW)
    assert (note["kind"], note["id"], note["text"]) == ("now", f"reminder:{sent}",
                                                        "Call the landlord")
    assert note["actions"] == ["done", "later"]

    assert needs.done(conn, f"reminder:{sent}")
    assert needs.needs(conn, NOW) == []
    assert needs.needs(conn, NOW + timedelta(hours=11)) == []


def test_a_reminder_never_answered_is_still_a_note_after_a_long_day_away(conn):
    """A note stays until it is dealt with (docs/design/notes.md). One that
    went off at eight while the user was out all day was gone from the
    notes by evening, unanswered."""
    _, sent = _going_off(conn, "Renew the parking permit", NOW - timedelta(hours=13))

    assert [n["id"] for n in needs.needs(conn, NOW)] == [f"reminder:{sent}"]
    assert [n["id"] for n in needs.needs(conn, NOW + timedelta(days=2))] == [f"reminder:{sent}"]


def test_a_critical_notice_is_a_note_and_an_ordinary_one_is_not(conn):
    """A notice sent even at night has a cost if it waits; the rest are
    updates in the journal."""
    for key, critical in (("storm", 1), ("newsletter", 0)):
        obs = conn.execute(
            "INSERT INTO observations (source, key, title, critical) VALUES ('weather', ?, ?, ?)",
            (key, key, critical)).lastrowid
        sent = outbox.add(conn, f"About the {key}", kind="notice", now=NOW)
        conn.execute("INSERT INTO told (message, how, outbox_id, obs_id, line)"
                     " VALUES ('ping:x', 'ping', ?, ?, 'x')", (sent, obs))
    [note] = needs.needs(conn, NOW)
    assert note["text"] == "About the storm" and note["actions"] == ["done"]


def _cap_notice(conn, cap: str, kind: str = "stopped") -> None:
    period = spend.window_for(cap, spend.windows()).period
    conn.execute("INSERT INTO spend_notices (kind, cap, period, limit_usd, spent_usd, at)"
                 " VALUES (?, ?, ?, 30, 30.2, '2026-10-15 13:00:00')", (kind, cap, period))


def test_what_stops_steve_working_is_a_note_with_the_one_thing_that_fixes_it(conn, monkeypatch):
    """A cap reached, the balance used up, a sign-in ended and Signal
    unlinked each stop Steve until the user acts; each was in a different
    corner of the app."""
    _cap_notice(conn, "MONTH_USD")
    _cap_notice(conn, spend.BALANCE)
    _cap_notice(conn, "CHAT_DAY_USD", "warned")  # nearly is not stopped
    monkeypatch.setattr(modules, "accounts", lambda conn, **_: [
        SimpleNamespace(key="acct-1", name="Work mail")])
    health.record(conn, "acct-1", kind="sign_in", now=NOW)
    health.record(conn, "acct-removed", kind="sign_in", now=NOW)  # no account now
    monkeypatch.setattr(runtime, "signal_state", {"state": "unlinked", "lost": True})

    found = {n["id"]: n for n in needs.needs(conn, NOW)}

    month = spend.window_for("MONTH_USD", spend.windows()).period
    assert found[f"cap:MONTH_USD:{month}"]["actions"] == ["raise_cap", "not_now"]
    assert "limit was reached" in found[f"cap:MONTH_USD:{month}"]["text"]
    [balance] = [n for i, n in found.items() if i.startswith(f"cap:{spend.BALANCE}:")]
    assert balance["actions"] == ["top_up", "not_now"]
    assert found["account:acct-1"]["actions"] == ["sign_in", "not_now"]
    assert found["account:acct-1"]["text"].startswith("Work mail")
    assert found["signal"]["text"] == needs.SIGNAL_LOST
    assert {n["kind"] for n in found.values()} == {"stopped"}
    assert len(found) == 4


def test_signal_is_a_note_only_when_a_link_that_worked_was_lost(conn, monkeypatch):
    """"Unlinked" is also Signal installed and never linked, or removed on
    purpose: only a link Signal still holds here, and can't use, has lost
    the user anything (`links.start_signal`). Read from setup's answer, a
    link made later from Channels and then lost was no note at all."""
    for state in ({"state": "unlinked"}, {"state": "unlinked", "lost": False},
                  {"state": "ready"}, {"state": "unavailable"}):
        monkeypatch.setattr(runtime, "signal_state", state)
        assert needs.needs(conn, NOW) == []
    monkeypatch.setattr(runtime, "signal_state", {"state": "unlinked", "lost": True})
    assert _ids(needs.needs(conn, NOW)) == ["signal"]


def test_a_lost_signal_is_no_note_while_telegram_reaches_the_phone(conn, monkeypatch):
    """Signal chosen in setup, then dropped for Telegram: every message
    reaches the phone, and a note saying none does came back every hour."""
    monkeypatch.setattr(runtime, "signal_state", {"state": "unlinked", "lost": True})
    monkeypatch.setattr(runtime, "signal", None)
    monkeypatch.setattr(runtime, "telegram",
                        SimpleNamespace(channel=SimpleNamespace(chat_id=4242)))
    assert needs.needs(conn, NOW) == []


def test_stopped_comes_first_then_what_is_going_off_then_cards_soonest_to_expire(
        conn, monkeypatch):
    """The design's order (notes.md, Order). Nothing else works until a
    stopped note is seen to; a reminder is due this minute, a card has
    hours; and of the cards, the one about to expire is the one to answer
    first, however recently it was asked."""
    _, sent = _going_off(conn, "Call the landlord", NOW - timedelta(minutes=10))
    later = _card(conn, "Send the deck to Iris", made="2026-10-15 13:58:00")
    soon = _card(conn, "Pay the plumber", token="CEDAR", made="2026-10-15 14:00:00",
                 expires="2026-10-15T18:00:00")
    older = _card(conn, "Reply to Odile", token="BIRCH", made="2026-10-15 13:50:00")
    monkeypatch.setattr(runtime, "signal_state", {"state": "failed", "lost": True})
    assert _ids(needs.needs(conn, NOW)) == [
        "signal", f"reminder:{sent}", f"card:{soon}", f"card:{older}", f"card:{later}"]


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


def test_not_now_puts_a_stopped_note_away_for_an_hour_and_then_it_comes_back(conn, monkeypatch):
    """Putting it off fixes nothing: what stopped is still stopped an hour
    on, and the note says so again."""
    monkeypatch.setattr(runtime, "signal_state", {"state": "unlinked", "lost": True})
    assert needs.not_now(conn, "signal", now=NOW)
    assert needs.needs(conn, NOW + timedelta(minutes=59)) == []
    assert _ids(needs.needs(conn, NOW + timedelta(minutes=61))) == ["signal"]


def test_each_answer_takes_only_the_notes_it_is_for(conn):
    """"Done" is a reminder's or a notice's, "Not now" a stopped note's: a
    card is answered yes or no, never put away."""
    assert not needs.done(conn, "card:3") and not needs.done(conn, "signal")
    assert not needs.not_now(conn, "reminder:4") and not needs.not_now(conn, "card:3")
    assert not needs.done(conn, "reminder:4; DROP TABLE put_off")
    assert conn.execute("SELECT count(*) FROM put_off").fetchone()[0] == 0


@pytest.mark.parametrize(("when", "at"), [
    ("15m", "2026-10-15T14:15:00Z"),
    ("1h", "2026-10-15T15:00:00Z"),
    ("evening", "2026-10-16T02:00:00Z"),   # 21:00 in Chicago
    ("morning", "2026-10-16T13:00:00Z"),   # 08:00 tomorrow
])
def test_later_books_the_reminder_again_and_answers_its_note(conn, when, at):
    """"Later" moves what is due, in the user's wall time, to when they
    chose; the note goes, and the reminder comes back as a new one."""
    _, sent = _going_off(conn, "Call the landlord", NOW - timedelta(minutes=10))

    assert needs.later(conn, f"reminder:{sent}", when, now=NOW) == at

    assert needs.needs(conn, NOW) == []
    [again] = reminders.active(conn)
    assert (again.text, again.kind) == ("Call the landlord", "remind")
    assert sqltime.stamp(datetime.fromisoformat(again.next_fire_at).replace(
        tzinfo=clock.local_zone())) == sqltime.stamp(datetime.fromisoformat(at))


def test_later_follows_the_briefings_the_user_moved(conn, monkeypatch):
    """The evening is the evening briefing's time, which the user may set:
    a fixed nine o'clock would come after a briefing at seven."""
    monkeypatch.setattr(settings, "EVENING", settings.time(19, 30))
    late = datetime(2026, 10, 16, 1, 0, tzinfo=UTC)  # 20:00 in Chicago, past it
    assert needs.later_at("evening", NOW) == datetime(2026, 10, 15, 19, 30,
                                                      tzinfo=ZoneInfo("America/Chicago"))
    assert needs.later_at("evening", late) == datetime(2026, 10, 16, 19, 30,
                                                       tzinfo=ZoneInfo("America/Chicago"))


def test_later_leaves_a_repeating_reminders_series_where_it_was(conn):
    """Put off once, an hourly reminder still goes off at its next hour:
    moving the series would move every one after."""
    booked, sent = _going_off(conn, "Water the plants", NOW - timedelta(minutes=10), every=60)
    series = reminders.get(conn, booked).next_fire_at

    assert needs.later(conn, f"reminder:{sent}", "1h", now=NOW)

    assert reminders.get(conn, booked).next_fire_at == series
    assert sorted(r.text for r in reminders.active(conn)) == [
        "Water the plants", "Water the plants"]


def test_later_takes_only_a_reminders_note(conn):
    """A notice has nothing to book again."""
    sent = outbox.add(conn, "Storm warning", kind="notice", now=NOW)
    assert needs.later(conn, f"notice:{sent}", "1h", now=NOW) is None
    assert needs.later(conn, "reminder:999", "1h", now=NOW) is None
    _, reminded = _going_off(conn, "Call the landlord", NOW - timedelta(minutes=10))
    assert needs.later(conn, f"notice:{reminded}", "1h", now=NOW) is None
    assert reminders.active(conn) == []


async def test_a_reminder_the_worker_fires_is_a_note_that_later_books_again(conn):
    """The message must say which reminder went off: without it, "Later"
    has nothing to book."""
    from agent.worker import background

    local = datetime.now(clock.local_zone()).replace(tzinfo=None)
    booked = reminders.schedule(conn, kind="remind", text="Mop", now=local,
                                when=(local + timedelta(minutes=1)).isoformat(timespec="seconds"))
    await background._fire_reminder(booked)

    [note] = needs.needs(conn)
    assert note["text"] == "Mop" and needs.later(conn, note["id"], "1h")
    assert [r.text for r in reminders.active(conn)] == ["Mop"]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _call(method: str, path: str, **kw) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=Starlette(routes=needs.ROUTES))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.request(method, path, **kw)

    return asyncio.run(go())


def test_the_routes_list_the_notes_and_refuse_an_answer_they_cannot_take(conn):
    """The app's four calls, and a wrong one refused with its reason."""
    _, sent = _going_off(conn, "Call the landlord", datetime.now(UTC) - timedelta(minutes=10))
    conn.commit()

    listed = _call("GET", "/control/needs").json()
    assert listed["ok"] and _ids(listed["needs"]) == [f"reminder:{sent}"]

    assert _call("POST", "/control/needs/later",
                 json={"id": f"reminder:{sent}", "when": "someday"}).status_code == 400
    later = _call("POST", "/control/needs/later", json={"id": f"reminder:{sent}", "when": "1h"})
    assert later.status_code == 200 and later.json()["at"].endswith("Z")
    assert _call("GET", "/control/needs").json()["needs"] == []

    _, sent = _going_off(conn, "Take the bins out", datetime.now(UTC) - timedelta(minutes=5))
    conn.commit()
    assert _call("POST", "/control/needs/done", json={"id": f"reminder:{sent}"}).json() == {
        "ok": True}
    assert needs.needs(conn, datetime.now(UTC) + timedelta(hours=11)) == []  # for good
    assert _call("POST", "/control/needs/done", json={"id": "signal"}).status_code == 400
    assert _call("POST", "/control/needs/not-now", json={"id": "signal"}).json() == {"ok": True}
    assert _call("POST", "/control/needs/not-now", json={}).status_code == 400
