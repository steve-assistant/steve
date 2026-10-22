"""The history a turn starts from says who said what, when, and what was done.

Session 238, 7 October 2026: resumed at 11:44, 13:10 and 23:52, each time
with every earlier "tomorrow" undated; six of the thirty resumed rows
unprompted pings, one a 1,414-character upgrade report; and the event it had
created nine hours earlier gone from the history, so it put another in the
same slot and said "You're free then".
"""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from agent import history, loop
from agent.loop import Conversation

NEW_YORK = ZoneInfo("America/New_York")


def test_a_stored_moment_reads_on_their_clock_and_a_garbled_one_as_nothing(monkeypatch):
    """The "[asked ...]" marks, the ledger's cards and the night's judge
    read stored moments through `history.clock`. A stored moment is UTC:
    read as it is, an evening is dated the next day. And one that is
    missing or garbled must read as nothing, not raise in the middle of
    replaying what the user said."""
    monkeypatch.setattr(history, "_zone", lambda: NEW_YORK)

    assert history.clock("2026-10-11 01:30:00") == "Saturday 10 October 2026, 21:30"
    assert history.clock("2026-10-11 01:30:00", "%a %H:%M %Z") == "Sat 21:30 EDT"
    assert [history.clock(x) for x in (None, "", "yesterday", 1760146200)] == [None] * 4


@pytest.fixture
def store(tmp_path, monkeypatch):
    from core import db

    monkeypatch.setattr(history, "_zone", lambda: NEW_YORK)
    # Stamps from a fixed week in October 2026, so the window must reach them.
    monkeypatch.setattr(loop, "HISTORY_WINDOW_HOURS", 24 * 365 * 20)
    conn = db.open_store(tmp_path / "h.db")
    yield conn
    conn.close()


def _row(conn, session, role, content, at, *, model=None, tool=None, args=None):
    with conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, model, tool_name, tool_args,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session, role, content, model, tool, args, at))


def _texts(convo):
    return [p.content for m in convo._history for p in m.parts]


def test_a_resumed_question_says_when_the_user_asked_it(store):
    """"tomorrow" in a question replayed after a restart must say which
    tomorrow: the camping trip "tomorrow" of 1 October is not 7 October's."""
    convo = Conversation(store, channel="signal")
    _row(store, convo.session_id, "user", "is tomorrow light?", "2026-10-02 00:32:00")
    _row(store, convo.session_id, "assistant", "Yes, nothing before noon.", "2026-10-02 00:32:09")

    resumed = Conversation(store, channel="signal", session_id=convo.session_id)

    assert _texts(resumed) == [
        "is tomorrow light?\n\n[asked Thursday 01 October 2026, 20:32]",
        "Yes, nothing before noon.",
    ]


def test_an_unprompted_message_is_marked_timed_and_an_upgrade_report_kept_to_its_headline(store):
    convo = Conversation(store, channel="signal")
    _row(store, convo.session_id, "assistant", "Rain from 3pm; your 4pm is in person.",
         "2026-10-07 17:13:00", model=loop.UNPROMPTED)
    report = "Upgrade #88 did not go in (rejected): Add a capability\n\n" + "pytest " * 300
    _row(store, convo.session_id, "assistant", report, "2026-10-07 18:00:00",
         model=loop.UNPROMPTED)

    rain, upgrade = _texts(Conversation(store, channel="signal", session_id=convo.session_id))

    assert rain == ("[sent unprompted, Wednesday 07 October 2026, 13:13] "
                    "Rain from 3pm; your 4pm is in person.")
    assert "Upgrade #88 did not go in" in upgrade and "pytest" not in upgrade
    assert len(upgrade) < history.SELF_REPORT_CHARS + 120


def test_pings_do_not_push_user_own_words_out_of_the_resumed_window(store, monkeypatch):
    monkeypatch.setattr(loop, "HISTORY_MAX_MESSAGES", 4)
    convo = Conversation(store, channel="signal")
    for i in range(2):
        _row(store, convo.session_id, "user", f"question {i}", f"2026-10-07 1{i}:00:00")
        _row(store, convo.session_id, "assistant", f"answer {i}", f"2026-10-07 1{i}:00:05")
    for i in range(6):
        _row(store, convo.session_id, "assistant", f"ping {i}", f"2026-10-07 13:0{i}:00",
             model=loop.UNPROMPTED)

    said = " ".join(_texts(Conversation(store, channel="signal", session_id=convo.session_id)))

    assert "question 0" in said and "answer 1" in said
    assert said.count("[sent unprompted") == 6


def test_a_resumed_turn_keeps_a_line_for_the_event_it_created(store):
    """The 7 October double booking: with the Fabrikam event's line in the
    history, the clash with a new event at 16:00 was there to see."""
    convo = Conversation(store, channel="signal")
    _row(store, convo.session_id, "user", "add the Fabrikam Denver session", "2026-10-07 17:50:00")
    _row(store, convo.session_id, "tool", json.dumps({"ok": True, "id": "24rkmabc"}),
         "2026-10-07 17:50:04", tool="create_event",
         args=json.dumps({"summary": "Fabrikam Denver Get to Know", "start": "2026-10-15T16:00"}))
    _row(store, convo.session_id, "tool", '{"messages": []}', "2026-10-07 17:50:02",
         tool="search_email", args="{}")
    _row(store, convo.session_id, "assistant", "Added it.", "2026-10-07 17:50:06")

    resumed = Conversation(store, channel="signal", session_id=convo.session_id)

    request = resumed._history[0]
    assert isinstance(request, ModelRequest)
    [line] = [p.content for p in request.parts if history.RECEIPT_HEAD in p.content]
    assert '"Fabrikam Denver Get to Know"' in line and "2026-10-15T16:00" in line
    assert "done, id 24rkmabc" in line
    assert "search_email" not in " ".join(_texts(resumed)), "only actions get a line"


def _turn(asked: str, *, calls=(), answer="Done."):
    out = [ModelRequest(parts=[UserPromptPart(asked)])]
    for i, (tool, args, result) in enumerate(calls):
        out.append(ModelResponse(parts=[ToolCallPart(tool, args, tool_call_id=f"c{i}")]))
        out.append(ModelRequest(parts=[ToolReturnPart(tool, result, tool_call_id=f"c{i}")]))
    out.append(ModelResponse(parts=[TextPart(answer)]))
    return out


def test_an_earlier_turn_keeps_a_line_for_each_action_and_nothing_else_it_read():
    made = _turn("add the Fabrikam Denver session", calls=[
        ("search_email", {"query": "Fabrikam"}, {"messages": ["PAGE-MARKER"]}),
        ("create_event", {"summary": "Fabrikam Denver Get to Know", "start": "2026-10-15T16:00"},
         {"ok": True, "id": "24rkmabc"}),
        ("draft_email", {"to": "asha@fabrikam.example", "subject": "Thank you"},
         {"status": "awaiting_approval", "token": "MAPLE"}),
    ])

    kept = history.compact(made + _turn("and Tailspin at 4?"))
    first = kept[0]
    lines = [p.content for p in first.parts if history.RECEIPT_HEAD in p.content]

    assert len(lines) == 2
    assert "create_event" in lines[0] and "done, id 24rkmabc" in lines[0]
    assert "draft_email" in lines[1] and "waiting for their yes" in lines[1]
    assert "PAGE-MARKER" not in str(kept), "what a tool read is still dropped"
    # And again, a turn later: the line survives being compacted twice.
    again = history.compact(kept + _turn("thanks"))
    assert sum(history.RECEIPT_HEAD in p.content for p in again[0].parts) == 2


def test_a_failed_action_is_said_to_have_failed():
    line = history.receipt("create_event", {"summary": "Coffee"},
                           {"ok": False, "error": "already on the calendar"})
    assert "failed: already on the calendar" in line


def test_a_stored_call_reads_as_what_it_holds_or_as_nothing():
    """Calls are stored as JSON text, but a result cut to fit is no longer
    JSON, nor are arguments stored before `_args_kept` (agent/loop.py), and
    a call not kept has none. The receipts, the skill writer and both evals
    read them through `as_dict`, which must never raise on an old
    conversation."""
    assert history.as_dict('{"summary": "Coffee"}') == {"summary": "Coffee"}
    assert history.as_dict({"summary": "Coffee"}) == {"summary": "Coffee"}
    for nothing in ('{"summary": "Cof', "[1, 2]", "null", "", None):
        assert history.as_dict(nothing) == {}, nothing


def test_something_said_unprompted_is_marked_in_the_live_history_too(store, monkeypatch):
    convo = Conversation(store, channel="signal")
    convo.note_said("How did the Fabrikam Seattle coffee chat go?")

    [said] = _texts(convo)
    assert said.startswith("[sent unprompted, ") and said.endswith("coffee chat go?")
    stored = store.execute("SELECT content FROM messages WHERE session_id = ?",
                           (convo.session_id,)).fetchone()[0]
    assert stored == "How did the Fabrikam Seattle coffee chat go?", "the record keeps her text"


def test_a_stored_moment_reads_where_the_user_is(monkeypatch):
    monkeypatch.setattr(history, "_zone", lambda: NEW_YORK)
    at = history.local("2026-10-07 17:13:00")
    assert at == dt.datetime(2026, 10, 7, 13, 13, tzinfo=NEW_YORK)


@pytest.mark.parametrize("note", [
    "[sent unprompted, Wednesday 07 October 2026, 13:13]",
    "[sent unprompted]",
    "[asked Wednesday 07 October 2026, 13:13]",
])
def test_a_reply_that_copies_a_note_of_the_historys_onto_its_opening_loses_it(note):
    """The notes sit on earlier assistant messages, where the model writing the
    next one can copy them: a reply opening "[sent unprompted, Wednesday ...]"
    would reach their phone with the history's own annotation on it."""
    assert history.clean(f"{note} Yes, at noon.") == "Yes, at noon."
    assert history.clean(f"{note}\n\n{note}\nYes, at noon.") == "Yes, at noon."


def test_a_bracket_that_is_not_one_of_the_historys_notes_stays_where_it_is():
    assert history.clean("[Draft] Hi Rina -- noon works.") == "[Draft] Hi Rina -- noon works."
    assert history.clean("It was [sent unprompted] at noon.") == (
        "It was [sent unprompted] at noon.")
    assert history.clean("") == ""


def test_a_citation_marker_is_still_cut_with_the_note_before_it_gone():
    marked = "[sent unprompted] It is at noonturn0search1 ."
    assert history.clean(marked) == "It is at noon."
