"""The history a turn of their reads, rebuilt from the database each time.

On 8 October 2026 the evening briefing went at 21:00:00 and was stored at
21:05, and their 21:00:33 "Solve is done" sat before the message it
answered. The model wrote a note about Northwind's email format (E2),
and their "why did you say that?" was read as about the briefing (E3). The
day's replay was nine items of the Owen saga and none of the briefing.
These pin what the builder guarantees instead: things in the order they were
said, the last few exchanges whole and the rest settled, what the user is
answering read whole, and nothing from after the moment a turn is built at.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse

from agent import context, sqltime
from core import db

AT = datetime(2026, 10, 9, 1, 0, 33, tzinfo=UTC)          # 21:00:33 in Pittsburgh


@pytest.fixture
def store(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo

    from agent import history

    monkeypatch.setattr(history, "_zone", lambda: ZoneInfo("America/New_York"))
    conn = db.open_store(tmp_path / "context.db")
    yield conn
    conn.close()


def _session(conn) -> int:
    with conn:
        return conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid


def _say(conn, session, role, text, at, *, model=None, tool=None, args=None) -> int:
    with conn:
        return conn.execute(
            "INSERT INTO messages (session_id, role, content, model, tool_name, tool_args,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session, role, text, model, tool, args, sqltime.stamp(at))).lastrowid


def _text(messages) -> str:
    return "\n".join(str(getattr(p, "content", "")) for m in messages for p in m.parts)


def _order(messages, *needles) -> list[int]:
    text = _text(messages)
    return [text.index(n) for n in needles]


def test_what_was_said_is_read_in_the_order_it_was_said_and_nothing_after(store):
    """E2/E3: their 21:00:33 "Solve is done" answered the 21:00 briefing;
    read before it, the briefing seemed to answer them."""
    session = _session(store)
    _say(store, session, "assistant", "Tomorrow: Solve is due.", AT - timedelta(seconds=33),
         model=context.UNPROMPTED)
    asked = _say(store, session, "user", "Solve is done", AT)
    _say(store, session, "assistant", "Noted: Northwind addresses use lastname.firstname.",
         AT + timedelta(minutes=6), model="gpt")

    later = context.history(store, session, at=AT + timedelta(minutes=10))

    briefing, theirs, note = _order(later, "Tomorrow: Solve is due", "Solve is done",
                                 "Noted: Northwind")
    assert briefing < theirs < note
    assert "[sent unprompted, Thursday 08 October 2026, 21:00] Tomorrow" in _text(later)

    then = context.history(store, session, at=AT, exclude_id=asked)
    assert "Tomorrow: Solve is due" in _text(then)
    assert "Noted: Northwind" not in _text(then), "nothing said after the moment"


def test_the_last_exchanges_are_whole_and_the_rest_of_the_day_is_settled(store):
    session = _session(store)
    for n in range(6):
        when = AT - timedelta(hours=8) + timedelta(minutes=10 * n)
        _say(store, session, "user", f"old question {n}", when)
        _say(store, session, "assistant", f"old answer {n}", when, model="gpt")
    for n in range(4):
        when = AT - timedelta(minutes=40 - 10 * n)
        _say(store, session, "user", f"new question {n}", when)
        _say(store, session, "assistant", f"new answer {n}", when, model="gpt")

    built = context.history(store, session, at=AT)

    settled = str(built[0].parts[0].content)
    assert settled.startswith(context.SETTLED_HEAD)
    assert "old question 0" in settled and "new question 0" in settled
    whole = [str(m.parts[0].content) for m in built[1:] if isinstance(m, ModelRequest)]
    assert [w.split("\n")[0] for w in whole] == ["new question 1", "new question 2",
                                                 "new question 3"]
    assert all("[asked " in w for w in whole)


def test_exchanges_from_hours_ago_are_settled_not_replayed_whole(store):
    """At 21:00 the last three of their messages went back to 16:18, two of
    them the Owen draft the user had told it off for."""
    session = _session(store)
    _say(store, session, "user", "look up owen tate on linkedin", AT - timedelta(hours=5))
    _say(store, session, "assistant", "Drafted a thank-you to Owen.", AT - timedelta(hours=5),
         model="gpt")

    built = context.history(store, session, at=AT)

    assert len(built) == 1 and str(built[0].parts[0].content).startswith(context.SETTLED_HEAD)


def test_what_the_last_exchange_did_and_found_rides_on_their_request(store):
    session = _session(store)
    _say(store, session, "user", "find camping sites", AT - timedelta(minutes=5))
    _say(store, session, "tool", "PAGE-MARKER " + "x" * 9_000, AT - timedelta(minutes=5),
         tool="read_page")
    _say(store, session, "tool", '{"ok": true, "draft_id": "d1"}', AT - timedelta(minutes=5),
         tool="draft_email", args='{"to": "owen@northwind.example", "subject": "Thanks"}')
    _say(store, session, "assistant", "Two sites.", AT - timedelta(minutes=5), model="gpt")

    [request, reply] = context.history(store, session, at=AT)

    said = [str(p.content) for p in request.parts]
    assert any(s.startswith("[your tools, for this: draft_email") for s in said)
    found = next(s for s in said if s.startswith("[what your tools found"))
    assert "PAGE-MARKER" in found and len(found) < context.FOUND_TOTAL + 100
    assert isinstance(reply, ModelResponse)


def test_user_message_reads_open_now_after_the_clock():
    text = context.turn_text("Solve is done", clock="Right now it is 21:00",
                             open_block="Open now:\n- [task 5] Solve")

    assert text.index("Solve is done") < text.index("Right now") < text.index("[task 5]")
    assert context.OPEN_HEAD in text
    assert context.turn_text("hi", clock="c") == "hi\n\n[c]", "unchanged when nothing is open"


def test_every_message_says_which_accounts_steve_could_not_check():
    """Asked "anything new?" while a mailbox is signed out, "no" would not be
    true; the model reads which accounts it could not check beside the clock."""
    text = context.turn_text("anything new?", clock="c",
                             unchecked="Work needs you: sign in again.")

    assert text == "anything new?\n\n[c]\n[Accounts Steve could not check: Work needs you: " \
                   "sign in again.]"


def test_in_the_small_hours_tomorrow_is_said_to_mean_the_coming_day(store):
    """E16: at 00:26 on Friday "ropes course tomorrow" meant Friday's, and
    the user was given Saturday's weather."""
    late = context.clock_line(datetime(2026, 10, 9, 4, 26, tzinfo=UTC))    # 00:26 EDT
    day = context.clock_line(datetime(2026, 10, 9, 16, 0, tzinfo=UTC))     # 12:00 EDT

    assert "today, Friday 09 October, not Saturday" in late
    assert "past midnight" not in day


async def test_a_turn_on_signal_is_sent_the_built_history(store, memories_repo, monkeypatch):
    """The live path, not only the function: a conversation of their reads the
    briefing before their reply to it, and Open now after their words."""
    from pydantic_ai import Agent
    from pydantic_ai.messages import TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from agent import loop
    from agent.loop import Conversation
    from agent.turn import Deps

    sent: list = []

    async def model(messages, info: AgentInfo):
        sent.append(list(messages))
        return ModelResponse(parts=[TextPart("Nice work.")])

    monkeypatch.setattr(loop, "build_agent", lambda role, **_: Agent(
        FunctionModel(model), deps_type=Deps, instructions="house"))
    convo = Conversation(store, channel="signal")
    now = datetime.now(UTC)
    _say(store, convo.session_id, "assistant", "Tomorrow: Solve is due.",
         now - timedelta(minutes=1), model=context.UNPROMPTED)
    with store:
        store.execute("INSERT INTO obligations (kind, text, source) VALUES"
                      " ('task', 'Book the dentist', 'conversation')")

    await convo.send("Solve is done")

    text = _text(sent[0])
    assert text.index("Tomorrow: Solve is due") < text.index("Solve is done")
    assert text.index("Solve is done") < text.index("[task 1] Book the dentist")


def test_only_their_own_conversations_read_the_built_history():
    """A background job or the night keeps the history of the run it is in:
    the user's day is not its context."""
    assert context.builds_here("signal") and context.builds_here("tray")
    assert not context.builds_here("night")


def test_their_reply_minutes_after_a_ping_is_said_to_answer_it(store):
    """E11: "yeah that's fine" a minute after a Google security alert, with
    a 7:15 call arranged twenty minutes before in view, cancelled the call."""
    session = _session(store)
    _say(store, session, "user", "call me at 7:15 on tuesday", AT - timedelta(minutes=20))
    _say(store, session, "assistant", "I'll call at 7:15.", AT - timedelta(minutes=20),
         model="gpt")
    _say(store, session, "assistant", "Google security alert -- was this you?",
         AT - timedelta(minutes=1), model=context.UNPROMPTED)
    asked = _say(store, session, "user", "yeah that's fine", AT)

    replying = context.replying_to(store, session, at=AT, exclude_id=asked)

    assert "1 min after" in replying and "Google security alert" in replying
    assert replying in context.turn_text("yeah that's fine", clock="c", replying=replying)
    assert context.replying_to(store, session, at=AT + timedelta(hours=1),
                               exclude_id=asked) == "", "not an hour later"


HURRICANE = "Hurricane Warning in effect near Ridgeview: tropical-storm-force winds"


def _told(conn, line: str, at: datetime, how: str = "ping") -> None:
    from agent.attention import told

    told.record(conn, f"{how}:{sqltime.stamp(at)}", how,
                [told.Line(line, source="weather", place="Ridgeview, WA", reason="safety")],
                said_at=sqltime.stamp(at))


def test_a_ping_says_where_it_came_from_settled_whole_and_when_the_user_answers_it(store):
    """At 00:24 on 9 October the user asked "why do you think I'm in Ridgeview?"
    about a ping sent at 23:03. The attention loop kept its source, place and
    reason (agent/attention/told.py); no turn's context had them."""
    session = _session(store)
    sent_at = AT - timedelta(minutes=81)
    _told(store, HURRICANE, sent_at)
    _say(store, session, "assistant", f"Sorry for the hour -- this one can't wait.\n{HURRICANE}",
         sent_at, model=context.UNPROMPTED)
    asked = _say(store, session, "user", "why do you think I'm in Ridgeview?", AT)
    soon = sent_at + timedelta(minutes=5)

    whence = "from weather, about Ridgeview, WA, judged safety"
    assert whence in _text(context.history(store, session, at=AT, exclude_id=asked)), "settled"
    assert whence in _text(context.history(store, session, at=soon)), "read whole"
    assert whence in context.replying_to(store, session, at=soon)


def test_a_briefing_or_a_ping_the_loop_kept_nothing_for_says_nothing_of_where(store):
    """A briefing is left as it is (a line per item would be most of it), and
    a ping with no record has no provenance to give; a line sent hours
    apart is not this message's."""
    session = _session(store)
    sent_at = AT - timedelta(minutes=10)
    _told(store, HURRICANE, sent_at, how="morning")
    _told(store, "Tornado Watch for Pittsburgh until 9pm", sent_at - timedelta(hours=3))
    _say(store, session, "assistant", "Rain from 3pm", sent_at - timedelta(hours=5),
         model=context.UNPROMPTED)
    _say(store, session, "assistant", f"Good evening.\n{HURRICANE}", sent_at,
         model=context.UNPROMPTED)
    _say(store, session, "assistant", "Tornado Watch for Pittsburgh until 9pm",
         sent_at + timedelta(minutes=2), model=context.UNPROMPTED)

    assert "from weather" not in _text(context.history(store, session, at=AT))


async def test_a_turn_of_their_carries_the_small_hours_note_with_its_clock(
        store, memories_repo, monkeypatch):
    """The note reaches the live turn, not only the replay: E16 was live."""
    from pydantic_ai import Agent
    from pydantic_ai.messages import TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from agent import loop
    from agent.loop import Conversation
    from agent.turn import Deps

    sent: list = []

    async def model(messages, info: AgentInfo):
        sent.append(list(messages))
        return ModelResponse(parts=[TextPart("ok")])

    monkeypatch.setattr(loop, "build_agent", lambda role, **_: Agent(
        FunctionModel(model), deps_type=Deps, instructions="house"))
    monkeypatch.setattr(loop, "_now_line", lambda: "Right now it is 00:26")
    monkeypatch.setattr(context, "small_hours", lambda now: " SMALL-HOURS-NOTE")

    await Conversation(store, channel="signal").send("ropes course tomorrow, weather?")

    assert "[Right now it is 00:26 SMALL-HOURS-NOTE]" in _text(sent[0])
