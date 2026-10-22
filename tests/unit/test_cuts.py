"""Nothing is cut silently where a model reasons from it.

A sweep on 4 October 2026 of every place text is cut short found the ones
that could mislead (agent/cut.py): Contoso and own-domain mail bodies cut at 4,000
characters with nothing to say so and no way to read on; a forwarded mail,
a worker's findings, a replayed answer, each cut where a model would read
the cut as the end; a meeting's join link lost below a description's cut;
a long call's arguments stored cut mid-JSON. These pin that each is now
whole, or cut where it says so. What a card shows them -- the same sweep's
other half -- is pinned in tests/gate/test_cards.py.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from agent import mailbox, runtime
from agent.cut import cut, keeping_links
from agent.turn import Deps
from core import db

TEAMS = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0"


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    yield c
    c.close()


def _read_email():
    """`read_email` as the model calls it, registered as `register_tools` does."""
    from pydantic_ai import Agent

    from agent.toolset import register_tools

    agent = Agent("test", deps_type=Deps)
    register_tools(agent)
    return agent._function_toolset.tools["read_email"].function


# --- the cut itself -------------------------------------------------------------------


def test_a_cut_ends_at_a_word_and_says_exactly_how_much_is_missing():
    text = "alpha beta gamma delta " * 40
    out = cut(text, 120)

    assert len(out) <= 120
    head, mark = out.split(" […", 1)
    assert head.endswith(("alpha", "beta", "gamma", "delta"))
    assert int(re.match(r"([\d,]+)", mark).group(1).replace(",", "")) == len(text) - len(head)
    assert cut("short", 120) == "short"


def test_a_cut_at_a_sentence_ends_with_one_and_an_unbroken_run_is_still_cut():
    assert cut("One thing. Another thing here. " * 20, 100, sentence=True,
               mark="").endswith(".")
    assert len(cut("x" * 500, 100)) == 100, "no boundary to find: cut where it is"


def test_a_long_description_keeps_the_join_link_from_past_the_cut():
    """A Teams invitation's join link is at the bottom, under the agenda."""
    from modules.google_cloud import calendar as google_calendar

    described = "Agenda: " + "Discuss the next item in depth. " * 60 + f"\nJoin: {TEAMS}"
    event = {"id": "e1", "summary": "Sync", "description": described,
             "start": {"date": "2026-10-05"}, "end": {"date": "2026-10-06"}}

    shown = google_calendar._format_event(event, dt.UTC)["description"]

    assert TEAMS in shown and len(shown) < len(described)
    assert keeping_links("short " + TEAMS, 1000) == "short " + TEAMS


# --- what a model reads ---------------------------------------------------------------


async def test_a_long_outlook_email_says_it_goes_on_and_reads_on_from_where_it_stopped(
        conn, monkeypatch):
    from steve_sdk.classes import Message
    from tests import persona
    from tests.gate.mailfake import Mailbox, connect

    body = "".join(f"Paragraph {i}. " + "Detail. " * 20 + "\n" for i in range(80))
    box = Mailbox(persona.WORK_EMAIL,
                  Message(native_id="00000000ABCDEF", subject="Offer", body=body))
    on_outlook = connect(monkeypatch, outlook=box)["outlook"].id("00000000ABCDEF")
    ctx = _Ctx(Deps(conn=conn, session_id=1))

    first = await _read_email()(ctx, message_id=on_outlook)
    assert first["body_truncated"] and first["body"] == body[:6000]
    upto = int(re.search(r"from_char=(\d+)", first["more"]).group(1))

    rest = await _read_email()(ctx, message_id=on_outlook, from_char=upto)
    assert first["body"] + rest["body"] == body[:upto + 6000]


async def test_a_long_forward_shows_where_it_is_cut_and_where_to_read_on():
    note = "Can you summarise this and tell me the deadline?"
    forwarded = "---------- Forwarded message ---------\nFrom: HR\n\n" + "Benefit detail. " * 2000
    body = f"{note}\n\n{forwarded}"
    from steve_sdk.classes import Message

    # Read from her Sent copy: that copy is where the rest is too.
    intake = mailbox.Intake(id=1, message_id="mail-k1-inbox-7", thread_id="t", sender="me",
                            subject="Fwd: benefits", kind="instruction", why="dmarc=pass",
                            read_from="mail-k1-sent-7")

    said = await mailbox.prompt(None, intake, Message(native_id="sent-7", body=body))

    mark = re.search(r"read_email with message_id (\S+) and from_char (\d+)", said)
    assert mark and mark.group(1) == intake.source == "mail-k1-sent-7"
    shown = said.split("<material>\n", 1)[1].split("\n[…", 1)[0]
    assert body[int(mark.group(2)) - len(shown):int(mark.group(2))] == shown


def test_a_long_calls_arguments_are_stored_as_json(conn):
    """Cut mid-string, a long email's call was no longer JSON, and the
    model scout's replay of it had no arguments at all."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    from agent.loop import MAX_TOOL_ARGS_CHARS, Conversation

    args = {"to": "ann@x.example.org", "body": "A long email. " * 1000}

    class Result:
        def new_messages(self):
            return [ModelResponse(parts=[ToolCallPart("send_email", args, tool_call_id="c1")]),
                    ModelRequest(parts=[ToolReturnPart("send_email", {"status": "queued"},
                                                       tool_call_id="c1")])]

    convo = Conversation(conn, channel="signal")
    convo._store_tool_results(Result())

    kept = conn.execute("SELECT tool_args FROM messages WHERE role = 'tool'").fetchone()[0]
    assert len(kept) <= MAX_TOOL_ARGS_CHARS
    assert json.loads(kept)["to"] == "ann@x.example.org", "still JSON, so the replay has it"


def test_a_workers_long_findings_reach_the_lead_marked_as_cut():
    from agent.workers import RESULT_CHARS, Worker

    worker = Worker(id=1, name="w", task="t")
    worker.status, worker.result = "done", "Found a thing. " * 1000

    said = worker.report()["result"]
    assert len(said) <= RESULT_CHARS and "more characters not shown" in said


def test_a_long_answer_replayed_after_a_restart_says_it_was_cut(conn):
    from agent.loop import Conversation

    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', ?)",
                     (session, "Here is the draft. " * 600))

    convo = Conversation(conn, channel="signal", session_id=session)

    assert "not replayed" in convo._history[0].parts[0].content
