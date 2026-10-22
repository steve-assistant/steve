"""The journal's backend gives the day page one day of everything, in order.

A reply on Signal, a card answered from the phone and a briefing each lived
in a different view, or in none, and none could say what happened on a day
(agent/worker/journal.py, its tables in db/migrations/0014_journal.sql).
The clock is pinned to Chicago here: CDT, five hours behind UTC, all
through October 2026.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.applications import Starlette

from agent import approvals, calendars, jobs, notes, outbox, runtime, sqltime
from agent.attention.briefing import key_for
from agent.history import UNPROMPTED
from agent.worker import answering, journal
from core import clock, db, spend

DAY = date(2026, 10, 15)  # its page: 09:00 UTC on the 15th to 09:00 UTC on the 16th


@pytest.fixture(autouse=True)
def chicago(monkeypatch):
    monkeypatch.setattr(clock, "local_zone", lambda: ZoneInfo("America/Chicago"))


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    monkeypatch.setattr(journal, "_next_event", {"at": None, "event": None})
    yield c
    c.close()


def _session(conn, channel: str = "tray") -> int:
    return conn.execute("INSERT INTO sessions (channel) VALUES (?)", (channel,)).lastrowid


def _message(conn, session: int, role: str, content: str, at: str, model=None) -> int:
    return conn.execute(
        "INSERT INTO messages (session_id, role, content, model, created_at)"
        " VALUES (?, ?, ?, ?, ?)", (session, role, content, model, at)).lastrowid


def _outbox(conn, kind: str, text: str, at: str, **more) -> int:
    return conn.execute(
        "INSERT INTO outbox (kind, text, created_at, until, remote, sent_at)"
        " VALUES (?, ?, ?, '2026-10-20 00:00:00', ?, ?)",
        (kind, text, at, more.get("remote"), more.get("sent_at"))).lastrowid


def _action(conn, preview: str, at: str, status: str = "pending", **more) -> int:
    return conn.execute(
        "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal,"
        " status, channel, decided_by, decided_at, executed_at, withdrawn_reason, created_at)"
        " VALUES ('send_email', '{}', ?, ?, 'outward', 'irreversible', ?, ?, ?, ?, ?, ?, ?)",
        (f"hash-{preview}-{at}-{status}", preview, status, more.get("channel"),
         more.get("by"), more.get("decided_at"), more.get("executed_at"),
         more.get("reason"), at)).lastrowid


def _job(conn, prompt: str, at: str, status: str = "done", kind: str = "research") -> int:
    return conn.execute(
        "INSERT INTO jobs (kind, prompt, status, created_at, finished_at) VALUES (?, ?, ?, ?, ?)",
        (kind, prompt, status, at, at if status == "done" else None)).lastrowid


def _keys(found: list[dict]) -> list[str]:
    return [entry["key"] for entry in found]


# ---------------------------------------------------------------------------
# The day
# ---------------------------------------------------------------------------


def test_a_talk_at_one_in_the_morning_stays_on_the_day_it_belongs_to():
    """The page turns at 4 a.m. in the user's zone: at midnight, a talk that
    ran late would have been cut in two across pages."""
    assert journal.page_of(datetime(2026, 10, 16, 6, 0, tzinfo=UTC)) == DAY  # 01:00 local
    assert journal.page_of(datetime(2026, 10, 16, 9, 30, tzinfo=UTC)) == date(2026, 10, 16)
    assert journal.page_of(datetime(2026, 10, 15, 8, 30, tzinfo=UTC)) == date(2026, 10, 14)
    assert journal.bounds(DAY) == ("2026-10-15 09:00:00", "2026-10-16 09:00:00")


def test_a_page_holds_the_messages_from_its_four_a_m_to_the_next(conn):
    """What the user said at 1 a.m. is on yesterday's page, and what they
    said at 4:30 on today's."""
    s = _session(conn)
    late = _message(conn, s, "user", "Still up, sorry", "2026-10-16 06:00:00")
    early = _message(conn, s, "user", "Before four", "2026-10-15 08:30:00")
    morning = _message(conn, s, "user", "Good morning", "2026-10-16 09:30:00")
    assert _keys(journal.entries(conn, DAY)) == [f"said:{late}"]
    assert _keys(journal.entries(conn, date(2026, 10, 14))) == [f"said:{early}"]
    assert _keys(journal.entries(conn, date(2026, 10, 16))) == [f"said:{morning}"]


def test_the_page_shows_the_users_conversations_and_none_of_steves_own(conn):
    """A night's research, a routine's run or a background task opens with
    words Steve wrote for itself, and the page drew them as the user's:
    "Research this and write down what you learn: ..." under their name.
    What came of those is on the page from the outbox and the tasks."""
    theirs = [("tray", "said"), ("signal", "said"), ("telegram", "said"), ("voice", "spoken")]
    shown = []
    for i, (channel, kind) in enumerate(theirs):
        said = _message(conn, _session(conn, channel), "user", f"on {channel}",
                        f"2026-10-15 13:0{i}:00")
        shown.append(f"{kind}:{said}")
    for channel in ("night", "routine", "job", "mailbox", "initiative", "goal", "reminder"):
        s = _session(conn, channel)
        _message(conn, s, "user", "Research this and write down what you learn: tides",
                 "2026-10-15 14:00:00")
        _message(conn, s, "assistant", "Tides follow the moon.", "2026-10-15 14:00:30")
    assert _keys(journal.entries(conn, DAY)) == shown


def test_every_source_comes_on_one_page_in_one_order(conn):
    """The conversation, the outbox, actions, tasks, listening notes and the
    computer's sleep, each where it happened: the views they replace each
    held one of them."""
    s = _session(conn, "signal")
    conn.execute("INSERT INTO awake (since, last_beat, gap_from, gap) VALUES"
                 " ('2026-10-15 12:10:00', '2026-10-15 20:00:00', '2026-10-15 04:50:00',"
                 " 'asleep')")
    briefing = _outbox(conn, "briefing", "Morning. Two meetings today.", "2026-10-15 12:30:00",
                       remote="signal", sent_at="2026-10-15 12:30:05")
    conn.execute("INSERT INTO told (message, how, outbox_id, line) VALUES"
                 " ('briefing:morning:2026-10-15', 'morning', ?, 'Two meetings today.')",
                 (briefing,))
    said = _message(conn, s, "user", "Send Iris the deck", "2026-10-15 13:00:00")
    reply = _message(conn, s, "assistant", "I'll ask you first.", "2026-10-15 13:00:20")
    action = _action(conn, "Send the deck to Iris", "2026-10-15 13:00:10")
    card = _outbox(conn, "card", "Send the deck to Iris? yes/no", "2026-10-15 13:00:11")
    task = _job(conn, "Find the cheapest flight to Lisbon", "2026-10-15 14:00:00")
    note = conn.execute("INSERT INTO notes (kind, title, started_at, ended_at) VALUES"
                        " ('lecture', 'Pricing strategy', '2026-10-15 15:00:00',"
                        " '2026-10-15 16:15:00')").lastrowid
    reminder = _outbox(conn, "reminder", "Call the landlord", "2026-10-15 17:00:00")

    found = journal.entries(conn, DAY)

    assert _keys(found) == [
        "asleep:1", f"outbox:{briefing}", f"said:{said}", f"action:{action}",
        f"reply:{reply}", f"task:{task}", f"listening:{note}", f"outbox:{reminder}"]
    assert f"outbox:{card}" not in _keys(found)  # the action's entry carries it
    asleep, morning = found[0], found[1]
    assert (asleep["from"], asleep["to"]) == ("2026-10-15T04:50:00Z", "2026-10-15T12:10:00Z")
    assert morning["kind"] == "briefing" and morning["which"] == "morning"
    assert morning["channel"] == "signal" and morning["sent"] == "2026-10-15T12:30:05Z"
    assert found[-1]["kind"] == "update" and found[-1]["of"] == "reminder"
    assert found[5]["title"] == "Find the cheapest flight to Lisbon"
    assert found[6]["ended"] == "2026-10-15T16:15:00Z"


def _night(conn, report: dict | str, status: str = "done") -> int:
    return conn.execute(
        "INSERT INTO jobs (kind, prompt, why, status, result, created_at, finished_at)"
        " VALUES ('night', '2026-10-16 03:00', 'nightly free time', ?, ?,"
        " '2026-10-16 08:04:00', '2026-10-16 08:04:00')",
        (status, report if isinstance(report, str) else json.dumps(report))).lastrowid


def test_the_nights_entry_carries_a_two_sentence_summary_and_the_parts_it_opens_to(conn):
    """The night showed on the day as "The night's work" and "done", and
    nothing of what it did. Its entry folds as a spoken exchange does: a
    summary of two sentences, read from the night's report so it is there
    as soon as the night is, and the report's parts to open to. A night
    cut short says why in its summary, not only inside the fold."""
    quiet_hours = {"started_at": "2026-10-16 03:00", "minutes": 3.6, "cost_usd": 0.0054,
                   "researched": ["Tide tables", "Ferry timetables", "Lighthouse history"],
                   "learned": ["maya@example.com: 0 sent, 9 received"],
                   "rehearsed": ["Desk rehearsal did not finish: it exited 1."],
                   "improvement_paused": "self-upgrade is paused: off by default"}
    night = _night(conn, quiet_hours)

    entry = next(e for e in journal.entries(conn, DAY) if e["key"] == f"task:{night}")
    assert entry["summary"] == ("Overnight I read about 3 topics, learned from the day and "
                                "rehearsed the desk. It took 4 minutes and $0.01.")
    assert entry["minutes"] == 4
    assert entry["parts"] == [
        {"title": "The desk, rehearsed", "items": ["Desk rehearsal did not finish: it exited 1."]},
        {"title": "Learned", "items": ["maya@example.com: 0 sent, 9 received"]},
        {"title": "Read about",
         "items": ["Tide tables", "Ferry timetables", "Lighthouse history"]},
        {"title": "No modules built", "items": ["self-upgrade is paused: off by default"]}]

    cut_short = _night(conn, {"started_at": "2026-10-16 03:00", "minutes": 1.0,
                              "stopped_because": "the night's cap was reached",
                              "not_done": ["research"]})
    entry = next(e for e in journal.entries(conn, DAY) if e["key"] == f"task:{cut_short}")
    assert entry["summary"] == ("A quiet night, with nothing worth doing. I stopped early: "
                                "the night's cap was reached.")
    assert entry["parts"] == [{"title": "Stopped early", "items": [
        "the night's cap was reached", "Not done: research"]}]

    unread = _night(conn, "not a report")
    failed = _night(conn, quiet_hours, status="failed")
    for key in (unread, failed):
        entry = next(e for e in journal.entries(conn, DAY) if e["key"] == f"task:{key}")
        assert "summary" not in entry and entry["title"] == "The night's work"


def test_a_reply_in_the_same_second_comes_after_the_words_it_answers(conn):
    """Moments are whole seconds, and a quick reply shares the second of the
    message it answers. Sorted by key, "reply:2" came before "said:1", so
    the answer sat above the question. Within a second the conversation
    goes first, in the order it was said, then what came of it."""
    s = _session(conn)
    said = _message(conn, s, "user", "Who wrote Middlemarch?", "2026-10-15 13:00:00")
    reply = _message(conn, s, "assistant", "George Eliot.", "2026-10-15 13:00:00")
    again = _message(conn, s, "user", "Thanks", "2026-10-15 13:00:00")
    task = _job(conn, "Find a copy of Middlemarch", "2026-10-15 13:00:00")

    assert _keys(journal.entries(conn, DAY)) == [
        f"said:{said}", f"reply:{reply}", f"said:{again}", f"task:{task}"]


def test_a_listening_note_on_the_day_carries_its_text_and_its_summarys_link(conn):
    """The day is the only place the app shows a listening note, since the
    Notes view went: without its text there, what was dictated could be
    neither read nor checked before deleting it."""
    note = conn.execute(
        "INSERT INTO notes (kind, title, body, doc_url, status, started_at, ended_at) VALUES"
        " ('dictation', 'Thank-you note', 'Dear Ines, thank you for the books.',"
        " 'https://docs.example.com/d/1', 'done', '2026-10-15 15:00:00',"
        " '2026-10-15 15:02:00')").lastrowid

    [entry] = journal.entries(conn, DAY)

    assert entry["key"] == f"listening:{note}"
    assert entry["text"] == "Dear Ines, thank you for the books."
    assert entry["doc_url"] == "https://docs.example.com/d/1"


def test_a_message_from_the_outbox_shows_once_and_not_again_from_the_conversation(conn):
    """A told message joins the conversation so a reply can find what it
    answers. Read from both, every briefing showed twice."""
    s = _session(conn, "signal")
    briefing = _outbox(conn, "briefing", "Evening. All quiet.", "2026-10-15 23:00:00")
    _message(conn, s, "assistant", "Evening. All quiet.", "2026-10-15 23:00:01",
             model=UNPROMPTED)
    assert _keys(journal.entries(conn, DAY)) == [f"outbox:{briefing}"]


def test_an_actions_decision_in_local_time_is_put_on_the_one_clock(conn):
    """An action's decision is written in local wall time, everything else in
    UTC: sorted as text, a card decided at 9:10 sat five hours from where it
    happened."""
    _action(conn, "Send the deck to Iris\nAttached: deck.pdf", "2026-10-15 14:00:00",
            status="done", channel="signal", by="signal", decided_at="2026-10-15T09:10:00",
            executed_at="2026-10-15 14:12:00")
    [entry] = journal.entries(conn, DAY)
    assert entry["decided"] == "2026-10-15T14:10:00Z"
    assert entry["changed"] == "2026-10-15T14:12:00Z"
    assert entry["what"] == "Send the deck to Iris"
    assert entry["line"] == "You said yes on Signal · done 09:12"
    assert entry["asked"]


def test_an_irreversible_action_in_its_hold_says_until_when_it_can_be_undone(conn):
    """The page counts Undo down from this. Only an irreversible action is
    held, and only while it is held can Undo stop it: offered later, Undo
    would say "too late" about mail already gone."""
    _action(conn, "Send the deck to Iris", "2026-10-15 14:00:00", status="executing",
            by="tray", decided_at="2026-10-15T09:10:00")
    _action(conn, "Send the notes to Iris", "2026-10-15 14:01:00", status="done",
            by="tray", decided_at="2026-10-15T09:11:00")
    reversible = _action(conn, "Add Dentist to Thursday", "2026-10-15 14:02:00",
                         status="executing", by="tray", decided_at="2026-10-15T09:12:00")
    conn.execute("UPDATE actions SET reversal = 'reversible' WHERE id = ?", (reversible,))

    held, done, undoable = journal.entries(conn, DAY)

    assert held["undo_until"] == "2026-10-15T14:10:30Z"
    assert done["undo_until"] is None
    assert undoable["undo_until"] is None


@pytest.mark.parametrize(("status", "more", "line"), [
    ("pending", {}, "Asked you about: Send the deck to Iris"),
    ("rejected", {"by": "tray"}, "You said no"),
    ("rejected", {"by": "voice"}, "You said no aloud"),
    ("expired", {}, "Expired · nothing was done"),
    ("withdrawn", {"reason": "the account was removed"},
     "Withdrawn · the account was removed"),
    ("failed", {"by": "tray"}, "You said yes · it failed"),
    ("executing", {"by": "signal"}, "You said yes on Signal · under way"),
    ("done", {"by": "auto"}, "Send the deck to Iris"),
    ("failed", {"by": "auto"}, "Couldn't: Send the deck to Iris"),
])
def test_an_actions_line_says_what_came_of_it(conn, status, more, line):
    """The page's line for a card is its outcome in words: a status code
    means nothing to the user."""
    _action(conn, "Send the deck to Iris", "2026-10-15 14:00:00", status=status, **more)
    [entry] = journal.entries(conn, DAY)
    assert entry["line"] == line
    assert entry["asked"] == (more.get("by") != "auto")


def test_voice_turns_close_together_are_one_spoken_exchange(conn):
    """A spoken conversation is one entry, not a line per breath; five
    minutes' quiet starts the next."""
    s = _session(conn, "voice")
    first = _message(conn, s, "user", "What's on tomorrow?", "2026-10-15 14:00:00")
    _message(conn, s, "assistant", "Two meetings.", "2026-10-15 14:00:30")
    _message(conn, s, "user", "Move the second.", "2026-10-15 14:01:40")
    later = _message(conn, s, "user", "And the weather?", "2026-10-15 14:10:00")

    talk, again = journal.entries(conn, DAY)

    assert (talk["key"], again["key"]) == (f"spoken:{first}", f"spoken:{later}")
    assert [line["text"] for line in talk["lines"]] == [
        "What's on tomorrow?", "Two meetings.", "Move the second."]
    assert talk["first_words"] == "What's on tomorrow?" and talk["minutes"] == 2
    assert talk["changed"] == "2026-10-15T14:01:40Z" and talk["summary"] is None
    assert again["minutes"] == 1


class _Summer:
    """The summary's model: what it was asked, and the answers it gives."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []

    async def __call__(self, system: str, prompt: str) -> str:
        self.asked.append((system, prompt))
        return self.answers.pop(0)


def _sum_up(at: str) -> int:
    return asyncio.run(journal.sum_up_spoken(runtime.conn, datetime.fromisoformat(at)))


def test_a_spoken_exchange_is_summed_up_once_it_has_ended_and_the_page_shows_it(
        conn, monkeypatch):
    """Folded under its first words, often only "Hey", an exchange said
    nothing of what was talked about. Summed up once it ends: not while it
    may still grow, and not twice."""
    summer = _Summer('"Moved the second meeting tomorrow"\nand more', "Asked about dinner")
    monkeypatch.setattr(journal, "_write", summer)
    s = _session(conn, "voice")
    first = _message(conn, s, "user", "Hey, what's on tomorrow?", "2026-10-15 14:00:00")
    _message(conn, s, "assistant", "Two meetings.", "2026-10-15 14:00:30")
    _message(conn, s, "user", "Move the second.", "2026-10-15 14:01:40")
    going = _message(conn, s, "user", "What's for dinner?", "2026-10-15 14:20:00")
    _message(conn, s, "user", "Something warm.", "2026-10-15 14:21:00")

    assert _sum_up("2026-10-15T14:24:00+00:00") == 1
    assert summer.asked == [(journal.SUM_UP, "Person: Hey, what's on tomorrow?\n"
                             "Steve: Two meetings.\nPerson: Move the second.")]
    assert _sum_up("2026-10-15T14:25:00+00:00") == 0
    assert _sum_up("2026-10-15T14:26:00+00:00") == 1

    talk, dinner = journal.entries(conn, DAY)
    assert (talk["key"], dinner["key"]) == (f"spoken:{first}", f"spoken:{going}")
    assert talk["summary"] == "Moved the second meeting tomorrow"
    assert dinner["summary"] == "Asked about dinner"
    assert talk["first_words"] == "Hey, what's on tomorrow?"
    # Made long after the exchange ended, the summary reaches an open page.
    polled = journal.entries(conn, DAY, since="2026-10-15T14:30:00Z")
    assert _keys(polled) == [f"spoken:{first}", f"spoken:{going}"]


def test_one_question_aloud_needs_no_summary_and_one_the_model_left_blank_is_not_asked_again(
        conn, monkeypatch):
    """An exchange in which the user said one thing is summed up by its first
    words. And a blank answer is kept, or the sweep would ask about the same
    exchange every minute."""
    summer = _Summer("   ")
    monkeypatch.setattr(journal, "_write", summer)
    s = _session(conn, "voice")
    _message(conn, s, "user", "What time is it?", "2026-10-15 14:00:00")
    _message(conn, s, "assistant", "Nine.", "2026-10-15 14:00:10")
    _message(conn, s, "user", "Remind me at ten.", "2026-10-15 14:10:00")
    _message(conn, s, "user", "Ten sharp.", "2026-10-15 14:10:30")

    assert _sum_up("2026-10-15T15:00:00+00:00") == 1
    assert _sum_up("2026-10-15T16:00:00+00:00") == 0

    assert len(summer.asked) == 1 and "Remind me at ten." in summer.asked[0][1]
    assert [e["summary"] for e in journal.entries(conn, DAY)] == [None, None]


def test_no_spoken_exchange_is_summed_up_past_the_cap_on_background_work(conn, monkeypatch):
    """A summary is background work, and stops where its cap says."""
    summer = _Summer("Asked about dinner")
    monkeypatch.setattr(journal, "_write", summer)
    # The day's background work spent, to the most its cap can be set to.
    spend.bill(conn, 0.5, model="m", unit=spend.Unit("background", "work"))
    s = _session(conn, "voice")
    _message(conn, s, "user", "What's for dinner?", "2026-10-15 14:20:00")
    _message(conn, s, "user", "Something warm.", "2026-10-15 14:21:00")

    assert _sum_up("2026-10-15T15:00:00+00:00") == 0
    assert summer.asked == []
    assert conn.execute("SELECT COUNT(*) FROM spoken_summaries").fetchone()[0] == 0


def test_a_poll_since_its_last_answer_gets_only_what_changed(conn):
    """The page asks every few seconds: it gets an action decided since, and
    a row stamped just before its last answer, which committed after it."""
    s = _session(conn)
    _message(conn, s, "user", "Old news", "2026-10-15 14:00:00")
    just_before = _message(conn, s, "user", "Committed late", "2026-10-15 14:59:57")
    action = _action(conn, "Send the deck to Iris", "2026-10-15 14:00:00", status="rejected",
                     by="tray", decided_at="2026-10-15T10:30:00")
    found = journal.entries(conn, DAY, since="2026-10-15T15:00:00Z")
    assert _keys(found) == [f"action:{action}", f"said:{just_before}"]  # in order of asking


def test_an_outcome_written_after_a_poll_comes_in_the_next(conn):
    """An action's outcome, a message the outbox stopped trying, and a
    listening note's document were written with no time, so the open page,
    asking only for what changed since its last look, never learned of
    them: a send that failed went on saying "You said yes · under way"
    until the page was reloaded."""
    action = _action(conn, "Send the deck to Iris", "2026-10-15 14:09:00", status="executing",
                     by="tray", decided_at="2026-10-15T09:10:00",
                     executed_at="2026-10-15 14:10:01")
    waiting = _outbox(conn, "job", "The deck went to Iris.", "2026-10-15 14:00:00",
                      remote="signal")
    note = conn.execute(
        "INSERT INTO notes (kind, title, status, started_at, ended_at) VALUES"
        " ('lecture', 'Pricing', 'done', '2026-10-15 13:00:00', '2026-10-15 14:00:00')").lastrowid
    assert journal.entries(conn, DAY, since="2026-10-15T15:00:00Z") == []

    approvals._set(conn, action, status="failed", error="the server refused it")
    outbox.stop_channel(conn, "signal", "Signal was unlinked")
    notes.finish(conn, note, doc_url="https://docs.example.com/pricing")

    later = journal.entries(conn, DAY, since="2026-10-15T15:00:00Z")
    assert set(_keys(later)) == {f"action:{action}", f"outbox:{waiting}", f"listening:{note}"}


def test_new_marks_what_changed_after_the_user_looked_and_never_their_own_words(conn):
    """One moment says what is news. Before the user has ever looked, the
    whole page would be bold, which says nothing."""
    s = _session(conn)
    seen = _message(conn, s, "assistant", "Seen already", "2026-10-15 14:30:00")
    said = _message(conn, s, "user", "Mine", "2026-10-15 15:30:00")
    fresh = _message(conn, s, "assistant", "Fresh", "2026-10-15 15:31:00")
    decided = _action(conn, "Send the deck to Iris", "2026-10-15 14:00:00", status="rejected",
                      by="signal", decided_at="2026-10-15T10:45:00")

    assert not any(entry["new"] for entry in journal.entries(conn, DAY))
    journal.look(conn, datetime(2026, 10, 15, 15, 0, tzinfo=UTC),
                 now=datetime(2026, 10, 15, 16, 0, tzinfo=UTC))
    new = {entry["key"]: entry["new"] for entry in journal.entries(conn, DAY)}
    assert new == {f"reply:{seen}": False, f"said:{said}": False, f"reply:{fresh}": True,
                   f"action:{decided}": True}


def test_looking_never_moves_back_and_never_past_now(conn):
    """Two windows: the one behind says nothing. And a page can't have been
    seen past the moment it was looked at."""
    now = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
    assert journal.look(conn, datetime(2026, 10, 15, 15, 0, tzinfo=UTC), now=now) == (
        "2026-10-15T15:00:00Z")
    assert journal.look(conn, datetime(2026, 10, 15, 14, 0, tzinfo=UTC), now=now) == (
        "2026-10-15T15:00:00Z")
    assert journal.look(conn, datetime(2027, 1, 1, tzinfo=UTC), now=now) == (
        "2026-10-15T16:00:00Z")


# ---------------------------------------------------------------------------
# Folded days
# ---------------------------------------------------------------------------


def test_a_folded_day_reads_its_evening_briefing_or_counts_what_happened(conn):
    """A day folds to one line without a model: that evening's briefing's
    first sentence, else what happened counted, else a quiet day.

    An evening briefing is known by the day it looks ahead to, as
    `briefing.send` records it. The fold looked it up by its own day, so
    each day showed the evening before's look-ahead; this test keyed its
    briefing the way the fold read it, and passed on the fault."""
    for text, sent, about in (
            ("You had three meetings, and the deck went to Iris. Tomorrow starts early.",
             "2026-10-16 02:00:00", date(2026, 10, 16)),
            ("A slow day, and the dentist moved. Tomorrow is clear.",
             "2026-10-14 02:00:00", date(2026, 10, 14))):
        evening = _outbox(conn, "briefing", text, sent)
        conn.execute("INSERT INTO told (message, how, outbox_id, line)"
                     " VALUES (?, 'evening', ?, 'x')", (key_for("evening", about), evening))
    s = _session(conn)
    _message(conn, s, "user", "Hi", "2026-10-14 14:00:00")
    _message(conn, s, "assistant", "Hello", "2026-10-14 14:00:05")
    _job(conn, "Find the cheapest flight to Lisbon", "2026-10-14 15:00:00")

    folded = journal.days(conn, date(2026, 10, 16), 3)

    assert [(d["date"], d["line"]) for d in folded] == [
        ("2026-10-15", "You had three meetings, and the deck went to Iris."),
        ("2026-10-14", "1 exchange, 1 thing done"),
        ("2026-10-13", "A slow day, and the dentist moved.")]
    assert [d["count"] for d in folded] == [1, 3, 1]


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


def _ledger(conn) -> dict[str, int]:
    return {r["kind"]: r["count"] for r in conn.execute("SELECT kind, count FROM issues")}


def test_a_mark_on_a_reply_is_kept_and_counted_once_in_the_ledger(conn):
    """The page draws the mark it was given, and the failure ledger counts an
    opinion once: a second click on the same mark is not a second reply that
    was wrong."""
    s = _session(conn)
    asked = _message(conn, s, "user", "When is the dentist?", "2026-10-15 14:00:00")
    reply = _message(conn, s, "assistant", "Thursday at 3.", "2026-10-15 14:00:05")

    assert journal.react(conn, reply, "right") and journal.react(conn, reply, "right")
    assert _ledger(conn) == {"feedback_up": 1}
    assert journal.react(conn, reply, "not right")
    assert _ledger(conn) == {"feedback_up": 1, "feedback_down": 1}
    issue = conn.execute("SELECT message_id, session_id FROM issues"
                         " WHERE kind = 'feedback_down'").fetchone()
    assert (issue["message_id"], issue["session_id"]) == (reply, s)
    assert journal.entries(conn, DAY)[1]["reaction"] == "not right"

    assert journal.react(conn, reply, None)
    assert journal.entries(conn, DAY)[1]["reaction"] is None
    assert not journal.react(conn, asked, "right")  # the user's own words
    assert not journal.react(conn, reply, "meh")


# ---------------------------------------------------------------------------
# What's next
# ---------------------------------------------------------------------------


class _Agenda:
    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.asked = 0

    def __call__(self, conn, days=1, *, limit=25) -> dict:
        self.asked += 1
        return {"events": self.events}


def test_whats_next_is_the_next_timed_event_and_reminder_within_three_hours(conn, monkeypatch):
    """The sidebar's "next" line: an all-day event isn't next, nor is one that
    started or one hours away. A reminder's time is local wall time."""
    agenda = _Agenda([
        {"summary": "Holiday", "start": "2026-10-15", "end": "2026-10-16", "all_day": True},
        {"summary": "Standup", "start": "2026-10-15T08:00:00-05:00",
         "end": "2026-10-15T08:15:00-05:00"},
        {"summary": "Dentist", "start": "2026-10-15T10:30:00-05:00",
         "end": "2026-10-15T11:00:00-05:00", "location": "Main St"},
        {"summary": "Dinner", "start": "2026-10-15T19:00:00-05:00"},
    ])
    monkeypatch.setattr(calendars, "agenda", agenda)
    for text, at in (("Water the plants", "2026-10-15T11:00:00"),
                     ("Too late", "2026-10-15T13:00:00"), ("Gone", "2026-10-15T08:00:00")):
        conn.execute("INSERT INTO reminders (kind, text, next_fire_at) VALUES ('remind', ?, ?)",
                     (text, at))
    now = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)  # 09:00 local

    nxt = journal.next_up(conn, now)

    assert nxt["event"] == {"summary": "Dentist", "start": "2026-10-15T15:30:00Z",
                            "end": "2026-10-15T16:00:00Z", "location": "Main St"}
    assert nxt["reminder"]["text"] == "Water the plants"
    assert nxt["reminder"]["at"] == "2026-10-15T16:00:00Z"
    journal.next_up(conn, now)
    assert agenda.asked == 1  # every calendar account isn't asked every minute


def test_an_all_day_event_is_never_next_even_given_a_time(conn, monkeypatch):
    """Some calendars give an all-day event its day's midnight as a time: at
    10 p.m. tomorrow's holiday would have been "next, in two hours"."""
    monkeypatch.setattr(calendars, "agenda", _Agenda([
        {"summary": "Holiday", "start": "2026-10-16T00:00:00", "all_day": True}]))
    assert journal.next_up(conn, datetime(2026, 10, 16, 3, 0, tzinfo=UTC))["event"] is None


# ---------------------------------------------------------------------------
# What Steve is doing
# ---------------------------------------------------------------------------


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def test_a_tool_that_looks_is_said_and_one_that_acts_is_not():
    """The working line uses the voice's words for a tool. One that acts is
    reported when it's done: announced, a draft would seem sent."""
    assert journal.doing("calendar_agenda") == "Checking your calendar"
    assert journal.doing("web_search") == "Looking that up"
    assert journal.doing("create_draft") is None


def test_a_turns_working_line_says_each_phrase_once_and_comes_down_at_the_end():
    """Three calendar tools in a row are one "Checking your calendar", and
    the line doesn't outlive the turn."""
    ws = _Socket()

    async def turn() -> None:
        line = journal.TurnLine(ws)
        for tool in ("calendar_agenda", "calendar_free", "create_draft", "web_search"):
            line(tool)
        line.done()
        await asyncio.sleep(0)

    asyncio.run(turn())
    assert [m["text"] for m in ws.sent] == ["Checking your calendar", "Looking that up", None]
    assert {m["of"] for m in ws.sent} == {"turn"}


def test_every_other_open_window_hears_a_turns_working_line_under_that_turns_own_key(
    monkeypatch,
):
    """The desk card says what Steve is doing while the user talks in the
    quick bar: it hears the line, and its end, keyed so two turns at once
    are two lines. The asking window hears it once, as "turn"."""
    from agent import runtime

    asker, card = _Socket(), _Socket()
    monkeypatch.setattr(runtime, "listeners", {asker, card})

    async def turns() -> None:
        first, second = journal.TurnLine(asker), journal.TurnLine(asker)
        first("calendar_agenda")
        second("web_search")
        first.done()
        await asyncio.sleep(0)

    asyncio.run(turns())
    assert [(m["of"], m["text"]) for m in asker.sent] == [
        ("turn", "Checking your calendar"), ("turn", "Looking that up"), ("turn", None)]
    keys = [m["of"] for m in card.sent]
    assert [m["text"] for m in card.sent] == ["Checking your calendar", "Looking that up", None]
    assert keys[0] == keys[2] != keys[1]
    assert all(k.startswith("turn:") for k in keys)


def test_the_chat_socket_says_what_the_turn_is_doing_before_the_reply(monkeypatch):
    """The app's own chat turn, through the worker's socket: the working
    line comes while the model reaches for a tool, and is down by the end."""
    from starlette.routing import WebSocketRoute
    from starlette.testclient import TestClient

    from agent.worker import answering, desk, routes

    class _Reply:
        def as_dict(self) -> dict:
            return {"text": "Two meetings."}

    class _Convo:
        session_id = 7

        async def send(self, text, *, on_tool=None, images=None):
            on_tool("calendar_agenda")
            on_tool("create_event")
            await asyncio.sleep(0)
            return _Reply()

    monkeypatch.setattr(answering, "conversation", lambda _sid, channel="tray": _Convo())
    monkeypatch.setattr(runtime, "voice", None)
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setattr(runtime, "turning", set())
    monkeypatch.setitem(desk._state, "engine", None)
    monkeypatch.setitem(desk._state, "ticker", None)
    app = Starlette(routes=[WebSocketRoute("/ws", routes.ws_session)])

    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "chat", "text": "What's on tomorrow?"})
        assert ws.receive_json()["type"] == "thinking"
        assert ws.receive_json() == {
            "type": "doing", "text": "Checking your calendar", "of": "turn"}
        rest = [ws.receive_json(), ws.receive_json()]

    assert {"type": "doing", "text": None, "of": "turn"} in rest
    assert {"type": "reply", "session_id": 7, "text": "Two meetings."} in rest


def test_a_task_says_what_it_works_on_and_stops_saying_it_however_it_ends(conn, monkeypatch):
    """A task that fails, to be tried again, has no result to announce; the
    line was taken down only on a result, and it stayed up."""
    ws = _Socket()
    monkeypatch.setattr(runtime, "listeners", {ws})

    async def broken(job):
        raise RuntimeError("the network went away")

    async def run() -> None:
        worker = jobs.JobWorker(conn, {"research": broken}, on_start=journal.job_started,
                                on_stop=journal.job_stopped)
        job_id = jobs.enqueue(conn, kind="research", prompt="Find the cheapest flight to Lisbon")
        await worker._run_one(jobs.claim(conn))
        await asyncio.sleep(0)
        assert jobs.get(conn, job_id).status == "queued"  # to be tried again

    asyncio.run(run())
    assert [(m["text"], m["of"]) for m in ws.sent] == [
        ("Working on: Find the cheapest flight to Lisbon", "job:1"), (None, "job:1")]


def test_a_hook_that_fails_does_not_stop_the_job(conn):
    """The queue runs one job after another: a working line that raised out
    of the worker would end every job after it."""

    async def handler(job):
        return "found it"

    def broken(job):
        raise RuntimeError("no window")

    async def run() -> None:
        worker = jobs.JobWorker(conn, {"research": handler}, on_start=broken, on_stop=broken)
        job_id = jobs.enqueue(conn, kind="research", prompt="Find a flight")
        await worker._run_one(jobs.claim(conn))
        assert jobs.get(conn, job_id).status == "done"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _found(answer: dict) -> list[tuple[str, list[str]]]:
    return [(d["date"], [hit["key"] for hit in d["hits"]]) for d in answer["days"]]


def test_a_search_finds_the_entries_holding_every_word_by_page_newest_first(conn):
    """A thing said weeks ago was found by scrolling back through every day
    between. Each word must be in the one entry, wherever in it, so "dentist
    theo" finds the message about both, not every mention of either; and a
    talk at one in the morning is on the page it belongs to."""
    s = _session(conn)
    both = _message(conn, s, "user", "Book the dentist for Theo, any morning",
                    "2026-10-15 14:00:00")
    _message(conn, s, "user", "Theo lands at six", "2026-10-15 15:00:00")
    late = _message(conn, s, "assistant", "The DENTIST can see Theo on Friday",
                    "2026-10-16 06:00:00")  # 1 a.m. in Chicago: the 15th's page
    earlier = _message(conn, s, "user", "Is the dentist open on Saturdays?",
                       "2026-10-14 16:00:00")
    sent = _action(conn, "Email the dentist: Theo needs a cleaning", "2026-10-13 17:00:00")
    # 10 p.m. in Chicago on the 11th: already the 12th in UTC.
    task = _job(conn, "Find a dentist near Theo's school", "2026-10-12 03:00:00")
    conn.commit()

    assert _found(journal.search(conn, "dentist theo")) == [
        ("2026-10-15", [f"said:{both}", f"reply:{late}"]),
        ("2026-10-13", [f"action:{sent}"]),
        ("2026-10-11", [f"task:{task}"])]
    assert _found(journal.search(conn, "dentist saturdays")) == [
        ("2026-10-14", [f"said:{earlier}"])]
    assert journal.search(conn, "orthodontist")["days"] == []
    assert journal.search(conn, "   ")["days"] == []


def test_a_search_takes_its_words_as_written_not_as_wildcards(conn):
    """The pages are chosen by LIKE, whose %, _ and escape character are
    not letters: "Done!" unescaped asks for "Done" and something else, and
    finds nothing."""
    s = _session(conn)
    sure = _message(conn, s, "user", "I'm 100% sure", "2026-10-15 14:00:00")
    _message(conn, s, "user", "Buy 100 apples", "2026-10-15 14:01:00")
    name = _message(conn, s, "user", "Rename it to draft_two", "2026-10-15 14:02:00")
    _message(conn, s, "user", "The draft two is done", "2026-10-15 14:03:00")
    done = _message(conn, s, "assistant", "All done!", "2026-10-15 14:04:00")
    conn.commit()

    assert _found(journal.search(conn, "100%")) == [("2026-10-15", [f"said:{sure}"])]
    assert _found(journal.search(conn, "draft_two")) == [("2026-10-15", [f"said:{name}"])]
    assert _found(journal.search(conn, "done!")) == [("2026-10-15", [f"reply:{done}"])]


def test_a_spoken_exchange_is_found_by_its_summary_or_a_line_and_shows_that_line(conn):
    """A spoken exchange is one entry of many lines: its hit shows the line
    the words are in, not its first."""
    s = _session(conn, "voice")
    first = _message(conn, s, "user", "What's on tomorrow?", "2026-10-15 14:00:00")
    _message(conn, s, "assistant", "Two meetings, then the pharmacy closes at five.",
             "2026-10-15 14:00:10")
    conn.execute("INSERT INTO spoken_summaries (first_id, summary) VALUES (?, ?)",
                 (first, "Maya asked about tomorrow's plans"))
    conn.commit()

    by_line = journal.search(conn, "pharmacy")["days"][0]["hits"]
    assert by_line == [{"key": f"spoken:{first}", "kind": "spoken", "at": "2026-10-15T14:00:00Z",
                        "line": "Two meetings, then the pharmacy closes at five."}]
    assert journal.search(conn, "plans")["days"][0]["hits"][0]["line"] == (
        "Maya asked about tomorrow's plans")


def test_a_long_entrys_line_starts_a_little_before_the_match_and_says_where_it_was_cut(conn):
    """The sidebar shows a line, not a page: a word near the end of a long
    reply must still be in it."""
    s = _session(conn)
    words = " ".join(f"word{i}" for i in range(80))
    _message(conn, s, "assistant", f"{words} the ferry leaves at noon {words}",
             "2026-10-15 14:00:00")
    conn.commit()

    line = journal.search(conn, "ferry")["days"][0]["hits"][0]["line"]
    assert line.startswith("…") and line.endswith("…") and "the ferry leaves" in line
    assert len(line) <= journal.SNIPPET + 2


def test_a_common_word_does_not_crowd_out_the_page_with_the_rare_one(conn):
    """The pages are capped. Chosen by "the", the 20 latest days that say
    it would hide the one older page where the ferry was talked about."""
    s = _session(conn)
    for day in range(1, journal.SEARCH_DAYS + 2):
        _message(conn, s, "user", "What's the weather?", f"2026-10-{day:02d} 15:00:00")
    ferry = _message(conn, s, "user", "When does the ferry leave?", "2026-09-20 15:00:00")
    conn.commit()

    assert _found(journal.search(conn, "the ferry")) == [("2026-09-20", [f"said:{ferry}"])]


def test_a_found_line_runs_a_messages_lines_together_without_their_markdown(conn):
    """The page draws a briefing's list; the sidebar's one line showed its
    dashes and asterisks as typed."""
    s = _session(conn)
    _message(conn, s, "assistant", "Morning, Maya.\n\n- **Dentist** at 15:00\n- Rain after four",
             "2026-10-15 14:00:00")
    conn.commit()

    assert journal.search(conn, "dentist")["days"][0]["hits"][0]["line"] == (
        "Morning, Maya. · Dentist at 15:00 · Rain after four")


def test_a_day_with_many_hits_shows_the_first_and_counts_the_rest(conn):
    s = _session(conn)
    for i in range(journal.SEARCH_HITS + 3):
        _message(conn, s, "user", f"Groceries, list {i}", f"2026-10-15 14:{i:02d}:00")
    conn.commit()

    day = journal.search(conn, "groceries")["days"][0]
    assert len(day["hits"]) == journal.SEARCH_HITS and day["more"] == 3


def test_the_search_route_answers_with_the_words_it_was_asked(conn):
    """The box drops an answer to older words by its `q`: it must come back
    as it was sent."""
    s = _session(conn)
    said = _message(conn, s, "user", "Call the plumber", "2026-10-15 14:00:00")
    conn.commit()

    answer = _call("GET", "/control/search", params={"q": "plumber call"}).json()
    assert answer["ok"] and answer["q"] == "plumber call" and answer["today"]
    assert _found(answer) == [("2026-10-15", [f"said:{said}"])]
    assert _call("GET", "/control/search").json()["days"] == []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _call(method: str, path: str, **kw) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=Starlette(routes=journal.ROUTES))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.request(method, path, **kw)

    return asyncio.run(go())


def test_the_routes_answer_the_page_and_refuse_what_they_cannot_read(conn, monkeypatch):
    """The day page's five calls, and a bad date or id refused with its
    reason rather than an empty page."""
    s = _session(conn)
    reply = _message(conn, s, "assistant", "Thursday at 3.", "2026-10-15 14:00:05")
    conn.commit()
    monkeypatch.setattr(calendars, "agenda", _Agenda([]))

    day = _call("GET", "/control/day?date=2026-10-15").json()
    assert day["date"] == "2026-10-15" and day["turns_at"] == "04:00"
    assert _keys(day["entries"]) == [f"reply:{reply}"] and day["as_of"].endswith("Z")
    since = _call("GET", f"/control/day?date=2026-10-15&since={day['as_of']}").json()
    assert since["entries"] == []
    assert _call("GET", "/control/day?date=Thursday").status_code == 400
    assert _call("GET", "/control/day?since=soon").status_code == 400

    folded = _call("GET", "/control/days?before=2026-10-16&n=2").json()
    assert [d["date"] for d in folded["days"]] == ["2026-10-15", "2026-10-14"]
    assert _call("GET", "/control/days?n=lots").status_code == 400

    assert _call("POST", "/control/react", json={"message_id": reply, "verdict": "right"}
                 ).json() == {"ok": True}
    assert _call("POST", "/control/react", json={"message_id": str(reply)}).status_code == 400
    assert _call("POST", "/control/react", json={"message_id": 999, "verdict": "right"}
                 ).status_code == 400

    looked = _call("POST", "/control/looked", json={"until": "2026-10-15T15:00:00Z"}).json()
    assert looked == {"ok": True, "looked": "2026-10-15T15:00:00Z"}
    assert _call("POST", "/control/looked", json={"until": "later"}).status_code == 400

    assert _call("GET", "/control/next").json() == {"ok": True, "event": None, "reminder": None}


def test_looking_at_the_page_marks_seen_what_it_showed_and_settles_their_lines(
        conn, monkeypatch):
    """The inbox marked a message seen when it drew it: the message joined
    the app's conversation, so a reply typed there found what it answered,
    and its lines in `told` were said, so the next briefing didn't repeat
    them. The day page replaced the inbox; looking at it must do the same,
    for what was written by the moment looked and nothing after."""
    noted: list[str] = []
    monkeypatch.setattr(answering, "note_sent", lambda text, *, channel: noted.append(text))
    briefing = _outbox(conn, "briefing", "Morning. Two meetings today.", "2026-10-15 12:30:00")
    conn.execute("INSERT INTO told (message, how, outbox_id, line) VALUES"
                 " ('briefing:morning:2026-10-15', 'morning', ?, 'Two meetings today.')",
                 (briefing,))
    card = _outbox(conn, "card", "Send the deck to Iris? yes/no", "2026-10-15 13:00:11")
    later = _outbox(conn, "reminder", "Call the landlord", "2026-10-15 17:00:00")
    conn.commit()

    assert _call("POST", "/control/looked", json={"until": "2026-10-15T15:00:00Z"}
                 ).json()["ok"]

    seen = {r["id"]: r["seen_at"] for r in conn.execute("SELECT id, seen_at FROM outbox")}
    assert seen[briefing] and seen[card] and seen[later] is None
    assert noted == ["Morning. Two meetings today.", "Send the deck to Iris? yes/no"]
    assert conn.execute("SELECT said_at FROM told").fetchone()["said_at"]

    # Looked again, the seen stay seen, and none joins the conversation twice.
    _call("POST", "/control/looked", json={"until": "2026-10-15T15:00:00Z"})
    assert len(noted) == 2


def test_a_moment_leaves_for_the_app_as_utc_to_the_second_with_a_z():
    """The journal, the needs list, the places and the activity page each
    wrote this form by hand, and the activity page's kept the microseconds
    the others cut. Text that is not a moment is none, not an error."""
    chicago = ZoneInfo("America/Chicago")
    moment = datetime(2026, 10, 15, 4, 30, 15, 250_000, tzinfo=chicago)
    assert sqltime.iso(moment) == "2026-10-15T09:30:15Z"
    assert sqltime.iso_of("2026-10-15 09:30:15") == "2026-10-15T09:30:15Z"
    assert sqltime.iso_of("2026-10-15 09:30:15.250") == "2026-10-15T09:30:15Z"
    assert sqltime.iso_of(None) is None
    assert sqltime.iso_of("") is None
    assert sqltime.iso_of("soon") is None
