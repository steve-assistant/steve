"""The obligations ledger: what is owed, and what closed it, kept by code.

Seven of the 23 incidents audited on 10 October 2026 were an old or closed
request read as open: "Solve is done" closed nothing and the next morning
still said it was due; a thank-you was drafted twice; "both complete"
cancelled six reminders it did not name. These tests pin what closes an item
(their words naming one item, their answer to a card, a tick), what never
does (words that name nothing, or two things), and that what has gone quiet
is asked about rather than guessed at.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import approvals, cardwords, closing, loop, reminders, runtime
from agent.loop import Conversation
from agent.turn import Deps
from agent.worker import background
from tools import commitments, obligations


@pytest.fixture
def laptop(store, monkeypatch):
    monkeypatch.setattr(runtime, "conn", store)
    monkeypatch.setattr(runtime, "conversations", {})
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setattr(runtime, "signal", None)
    monkeypatch.setattr(runtime, "outbox_wake", None)
    return store


def _task(conn, text, *, due=None, source_id=None) -> int:
    return commitments.add(conn, text=text, quote=text, due_date=due, source="conversation",
                           source_id=source_id or f"said:{text}")


def _their_week(conn) -> dict[str, int]:
    """What was open on 8 October, give or take."""
    return {
        "solve": _task(conn, "Complete the Tailspin Solve assessment", due="2026-10-09"),
        "zoe": commitments.add(conn, text="Send Zoe the doctor's note",
                                  quote="I'll get you the doctor's note this week.",
                                  source_id="m-zoe"),
        "journal": _task(conn, "Submit the DES 231 journal", due="2026-10-10"),
    }


# --------------------------------------------------------------------------
# The store, and the commitments it took over
# --------------------------------------------------------------------------


def test_the_same_thing_kept_twice_is_one_item(store):
    """The night reads overlapping windows of sent mail and rewords each
    time."""
    first = commitments.add(store, text="Send the deck", quote="I'll send the deck Friday.",
                            source_id="m1")
    again = commitments.add(store, text="Send Clara the deck",
                            quote="I'll send the deck Friday.", source_id="m1")

    assert first is not None and again is None


def test_closing_says_what_closed_it_and_closes_only_what_is_open(store):
    item = _task(store, "Book the dentist")

    closed = obligations.close(store, item, by="her words", outcome="she said so")

    assert (closed.status, closed.closed_by, closed.outcome) == ("done", "her words",
                                                                 "she said so")
    assert obligations.close(store, item, by="tasks tick") is None, "closed once"
    assert [i.id for i in obligations.closed_since(store, 1)] == [item]


# --------------------------------------------------------------------------
# Their words
# --------------------------------------------------------------------------


def test_solve_is_done_closes_the_solve_task_and_nothing_else(store):
    """E2: 21:00 on 8 October. The 07:40 handover still said Solve was due
    today, and the model answering "Solve is done" wrote a note about Northwind's
    email format instead."""
    week = _their_week(store)

    [closed] = closing.from_words(store, "Solve is done")

    assert closed.id == week["solve"] and closed.closed_by == "user words"
    assert "Solve is done" in closed.outcome
    assert {c.id for c in commitments.open_ones(store)} == {week["zoe"], week["journal"]}
    assert all("Solve" not in c.text for c in commitments.due_soon(store, days=30))


@pytest.mark.parametrize("words", [
    "both complete",             # 27 Sep: named nothing, and six reminders went
    "yeah that's fine",
    "Is Solve done?",
    "Solve isn't done yet",
    "Solve not done",            # ends in "done": only the "not" says otherwise
    "Solve almost done",
    "not finished with the journal",
    "done",
    "I finished the Solve practice test, now I need to do the real one, which is due "
    "tomorrow, and after that the journal and then the deck for Zoe, which I have "
    "not started at all",
])
def test_words_that_name_nothing_or_do_not_say_done_close_nothing(store, words):
    _their_week(store)

    assert closing.from_words(store, words) == []
    assert len(commitments.open_ones(store)) == 3


def test_words_that_could_mean_two_items_close_neither(store):
    _task(store, "Submit the DES 231 journal")
    _task(store, "Read the DES 231 case")

    assert closing.from_words(store, "DES 231 is done") == []
    assert len(commitments.open_ones(store)) == 2


def test_no_thank_you_note_needed_drops_the_one_it_names(store):
    """N7: "no thank you note needed" -- dropped, not done, and only Owen's."""
    owen = _task(store, "draft a thank-you note to Owen Tate")
    iris = _task(store, "draft a thank-you note to Iris Novak")

    [closed] = closing.from_words(store, "no thank you note to Owen needed")

    assert (closed.id, closed.status) == (owen, "dropped")
    assert obligations.get(store, iris).is_open


def _answering_model(store, monkeypatch, memories_repo):
    async def respond(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Nice work.")])

    def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
        from pydantic_ai import Agent

        return Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")

    monkeypatch.setattr(loop, "build_agent", build_agent)


@pytest.mark.parametrize(("channel", "closes"), [("signal", True), ("tray", True),
                                                 ("reminder", False)])
async def test_a_turn_closes_what_user_words_name_before_the_model_reads_it(
        store, monkeypatch, memories_repo, channel, closes):
    """On their channels only: a reminder's text is the agent talking to
    itself, and "send the invoice is done" in one is not
    them saying so."""
    week = _their_week(store)
    _answering_model(store, monkeypatch, memories_repo)

    await Conversation(store, channel=channel).send("Solve is done")

    assert obligations.get(store, week["solve"]).is_open is not closes


# --------------------------------------------------------------------------
# Their phone, and their reminders
# --------------------------------------------------------------------------


def test_an_item_closed_here_is_ticked_on_user_phone_by_the_sync(store, monkeypatch):
    """`commitment_done` closed the row and left the task showing until the user
    ticked it themselves."""
    from agent import taskmirror
    from steve_sdk.classes import Task
    from tests.gate import slotfake

    board = slotfake.TaskBoard(Task(native_id="task-1", title="Book the dentist"))
    found = slotfake.plug(monkeypatch, "tasks", google=board)["google"]
    item = _task(store, "Book the dentist")
    obligations.set_task(store, item, found.id("task-1"))
    commitments.resolve(store, item)

    result = taskmirror.reconcile(store)

    assert board.completed == ["task-1"] and result["ticked"] == 1
    assert taskmirror.reconcile(store)["ticked"] == 0, "ticked once"


class _Signal:
    """Stands in for the Signal bridge: `channel.send` says whether Signal
    took the message."""

    def __init__(self, delivered: bool):
        self.delivered = delivered
        self.sent: list[str] = []
        self.channel = self

    async def send(self, message):
        if self.delivered:
            self.sent.append(message.text)
        return self.delivered


async def test_a_reminder_signal_could_not_carry_still_reaches_them_when_it_is_back(
        laptop, monkeypatch):
    """It went to `done` before it was sent, so one Signal could not carry
    was recorded as gone off and never arrived. Then it waited in a dict in
    memory, tried every minute. Now it goes off into the outbox, which the
    app shows at once and which keeps trying the phone."""
    from datetime import datetime

    from agent import outbox
    from agent.worker import links

    now = datetime.now()
    booked = reminders.schedule(laptop, kind="remind", text="Mop",
                                when=(now + timedelta(minutes=1)).isoformat(timespec="seconds"),
                                now=now)
    signal = _Signal(delivered=False)
    monkeypatch.setattr(runtime, "signal", signal)

    await background._fire_reminder(booked)
    await links.send_due()

    assert reminders.get(laptop, booked.id).status == "done"
    [message] = outbox.inbox(laptop)
    assert message.text == "Mop" and message.sent_at is None and message.next_try_at

    signal.delivered = True
    await links.send_due(datetime.now() + timedelta(minutes=2))
    assert signal.sent == ["Mop"]


# --------------------------------------------------------------------------
# What a turn reads
# --------------------------------------------------------------------------


def test_open_now_says_what_is_open_and_what_closed_and_how(store):
    week = _their_week(store)
    closing.from_words(store, "Solve is done")
    obligations.add(store, kind="task", text="email Iris the deck", source="conversation")

    text = closing.open_now(store).render()

    opened, _, shut = text.partition("Closed in the last day")
    assert f"[task {week['journal']}]" in opened and "would reach someone" in opened
    assert f"[task {week['solve']}]" in shut and "user words" in shut
    assert "Solve is done" in shut and "Solve" not in opened


# --------------------------------------------------------------------------
# Asking them (REDESIGN part 5)
# --------------------------------------------------------------------------


def _aged(conn, item_id, days):
    with conn:
        conn.execute("UPDATE obligations SET created_at = datetime('now', ?) WHERE id = ?",
                     (f"-{days} days", item_id))


def test_what_is_worth_asking_about_is_what_has_gone_quiet(store):
    """Past its date, an ask that waited, two weeks undated -- not what is
    due today, new, or already being asked about."""
    from datetime import date

    yesterday2 = (date.today() - timedelta(days=2)).isoformat()
    late = _task(store, "Submit the DES 231 journal", due=yesterday2)
    today = _task(store, "Read the case", due=date.today().isoformat())
    old = _task(store, "Stop by if Tomas is still there")
    _aged(store, old, 20)
    new = _task(store, "Book the dentist")
    asked = _task(store, "Email the club", due=yesterday2)
    with store:
        store.execute("UPDATE obligations SET asked_at = datetime('now', '-1 day') WHERE id = ?",
                      (asked,))

    ids = [item.id for item, _ in closing.worth_asking(store)]

    assert ids == [late, old]
    assert today not in ids and new not in ids and asked not in ids


def test_a_few_questions_a_day_and_the_same_one_is_not_asked_twice(store, monkeypatch):
    from agent.attention import settings

    monkeypatch.setattr(settings, "MAX_QUESTIONS_PER_DAY", 2)
    for n in range(4):
        _task(store, f"Overdue thing {n}", due=f"2026-01-0{n + 1}")

    first = closing.briefing_questions(store)
    again = closing.briefing_questions(store)

    assert len(first) == 2 and all("YES " in line and "NO " in line for line in first)
    assert again == first, "a rewritten briefing carries the same cards, and no more"
    assert len(approvals.pending(store)) == 2


@pytest.mark.parametrize("breaks", ["\r", " ", "\n"])
def test_a_question_keeps_its_word_whatever_breaks_the_items_text(store, breaks):
    """The question and its card's first line are one line. With a line
    break in the item's text the card's first line stopped at it and the
    question did not, so the question lost its word on the way to the
    phone (agent/cardwords.py; step 11's fourth review). These cards are
    made quietly: the briefing is the only place their word shows. Adding
    an item folds its spaces, so the break is written as an older version
    or another writer could have left it."""
    item = _task(store, "Send the form", due="2026-01-01")
    with store:
        store.execute("UPDATE obligations SET text = ? WHERE id = ?",
                      (f"Send the form{breaks}to the office", item))

    [line] = closing.briefing_questions(store)

    assert "Send the form to the office" in line
    assert cardwords.redact(line, cardwords.waiting(store)) == line

async def test_their_yes_keeps_an_item_and_their_no_drops_it(store):
    kept = _task(store, "Send the deck", due="2026-01-01")
    dropped = _task(store, "Call the bank", due="2026-01-02")
    closing.briefing_questions(store)
    cards = {obligations.get(store, i).action_id: i for i in (kept, dropped)}

    said = await closing.keep(store, kept)
    closing.declined(store, next(a for a, i in cards.items() if i == dropped))

    assert said["say"] == "Kept: Send the deck." and obligations.get(store, kept).is_open
    assert obligations.get(store, dropped).status == "dropped"
