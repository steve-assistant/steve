"""A model is never asked to copy a long id back exactly.

On 4 October 2026 the agent's mailbox read a forwarded Contoso mail, the model
passed back its 156-character id with characters missing from the middle,
every read failed, and the canary blamed -- and reverted -- three upgrades
that had nothing to do with it. agent/handles.py swaps long ids for short
handles in everything a tool returns, and back in everything a tool is
given; these pin both directions, that every registered tool gets it, and
that a handle can never quietly name the wrong item.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from agent import handles
from agent.attention import prepare
from agent.attention.store import Observation
from agent.turn import Deps
from core import db

OUTLOOK_ID = "outlook:" + "00000000DF29F64E1A2B3C4D5E6F" * 5 + "0000669016560000"
GMAIL = "18c2f4a9b7d1e3f5"


class _Ctx:
    def __init__(self, conn):
        self.deps = Deps(conn=conn, session_id=1)


@pytest.fixture
def conn():
    c = db.open_store()
    yield c
    c.close()


def test_a_long_id_in_a_result_comes_back_as_a_short_handle_and_a_short_one_as_it_was(conn):
    found = {"messages": [{"id": OUTLOOK_ID, "thread_id": GMAIL, "subject": "Interview"}]}

    shown = handles.shorten(conn, found)

    handle = shown["messages"][0]["id"]
    assert handles.HANDLE.fullmatch(handle) and handle.startswith("outlook#")
    assert len(handle) < 20
    assert shown["messages"][0]["thread_id"] == GMAIL, "a short id is left alone"
    assert found["messages"][0]["id"] == OUTLOOK_ID, "the tool's own result is not changed"
    assert handles.resolve(conn, handle) == OUTLOOK_ID


def test_a_result_with_nothing_to_shorten_is_the_same_object(conn):
    found = {"messages": [{"id": GMAIL, "body": "x" * 500}], "link": "https://" + "a" * 80}
    assert handles.shorten(conn, found) is found


def test_only_values_named_as_ids_are_shortened(conn):
    long_text = "y" * 200
    shown = handles.shorten(conn, {"body": long_text, "link": long_text, "event_ids": [OUTLOOK_ID],
                                   "draft_id": OUTLOOK_ID, "items": [(OUTLOOK_ID,)]})
    assert shown["body"] == long_text and shown["link"] == long_text
    assert shown["items"] == [(OUTLOOK_ID,)], "a tuple of values under no id name is left alone"
    assert handles.HANDLE.fullmatch(shown["event_ids"][0])
    assert handles.HANDLE.fullmatch(shown["draft_id"])


def test_the_same_id_always_gets_the_same_handle(conn):
    first = handles.shorten(conn, {"id": OUTLOOK_ID})["id"]
    again = handles.shorten(conn, {"message_id": OUTLOOK_ID})["message_id"]
    assert first == again, "a conversation replayed after a restart must mean what it meant"
    assert conn.execute("SELECT COUNT(*) FROM handles").fetchone()[0] == 1


def test_a_handle_already_taken_by_another_id_is_never_given_to_this_one(conn):
    """Ten hex digits of a hash can collide. A handle that resolves to the
    wrong draft deletes the wrong draft, so the second id takes a longer
    handle rather than the first one's."""
    digest = hashlib.sha256(OUTLOOK_ID.encode()).hexdigest()
    taken = f"outlook#{digest[:10]}"
    with conn:
        conn.execute("INSERT INTO handles (handle, full) VALUES (?, 'outlook:someone-else')",
                     (taken,))

    handle = handles.shorten(conn, {"id": OUTLOOK_ID})["id"]

    assert handle != taken and handle == f"outlook#{digest[:16]}"
    assert handles.resolve(conn, handle) == OUTLOOK_ID
    assert handles.resolve(conn, taken) == "outlook:someone-else"


def test_an_id_not_shaped_like_a_handle_is_passed_through(conn):
    """One the user pasted, or one read off a link, is used as it is."""
    assert handles.resolve(conn, OUTLOOK_ID) == OUTLOOK_ID
    assert handles.resolve(conn, GMAIL) == GMAIL
    assert handles.resolve(conn, 42) == 42


def test_a_handle_is_found_however_the_model_quoted_it(conn):
    handle = handles.shorten(conn, {"id": OUTLOOK_ID})["id"]
    assert handles.resolve(conn, f" `{handle.upper()}` ") == OUTLOOK_ID
    assert handles.resolve(conn, [handle, GMAIL]) == [OUTLOOK_ID, GMAIL]


def test_an_unknown_handle_is_refused_never_guessed(conn):
    with pytest.raises(handles.UnknownHandle):
        handles.resolve(conn, "outlook#0123456789")


# --- around a tool ---------------------------------------------------------------------


def _tool(seen: list):
    async def read_email(ctx, message_id: str, draft_ids: list[str] | None = None,
                         subject: str = "") -> dict:
        """Read one email."""
        seen.append((message_id, draft_ids, subject))
        return {"id": message_id, "subject": subject}

    return read_email


def test_a_tool_is_given_the_id_behind_a_handle_and_hands_back_handles(conn):
    seen: list = []
    tool = handles.wrap(_tool(seen))
    handle = handles.shorten(conn, {"id": OUTLOOK_ID})["id"]

    result = asyncio.run(tool(_Ctx(conn), message_id=handle, draft_ids=[handle],
                              subject="outlook#0123456789"))

    assert seen == [(OUTLOOK_ID, [OUTLOOK_ID], "outlook#0123456789")], (
        "only arguments named as ids are looked up")
    assert result == {"id": handle, "subject": "outlook#0123456789"}


def test_a_tool_given_an_unknown_handle_is_not_called_and_says_search_again(conn):
    seen: list = []
    tool = handles.wrap(_tool(seen))

    result = asyncio.run(tool(_Ctx(conn), "outlook#0123456789"))

    assert seen == []
    assert result["ok"] is False and "Search again" in result["error"]


def test_a_tool_called_with_no_database_is_called_as_it_was():
    seen: list = []

    class Bare:
        deps = None

    result = asyncio.run(handles.wrap(_tool(seen))(Bare(), OUTLOOK_ID))
    assert seen == [(OUTLOOK_ID, None, "")] and result["id"] == OUTLOOK_ID


def _registered():
    from pydantic_ai import Agent

    from agent.toolset import register_tools

    agent = Agent("test", deps_type=Deps)
    register_tools(agent, lead=True)
    return agent._function_toolset.tools


def test_every_registered_tool_takes_and_gives_handles(conn, monkeypatch):
    """From the caller's side: the real `read_email`, as the model calls it,
    reads the message the handle names, and its answer carries a handle."""
    from steve_sdk.classes import Message
    from tests import persona
    from tests.gate.mailfake import Mailbox, connect

    native = OUTLOOK_ID.removeprefix("outlook:")
    box = Mailbox(persona.WORK_EMAIL, Message(native_id=native, subject="Interview"))
    full = connect(monkeypatch, outlook=box)["outlook"].id(native)
    tools = _registered()
    assert all(hasattr(t.function, "__wrapped__") for t in tools.values()), (
        [n for n, t in tools.items() if not hasattr(t.function, "__wrapped__")])
    handle = handles.shorten(conn, {"id": full})["id"]

    result = asyncio.run(tools["read_email"].function(_Ctx(conn), message_id=handle))

    assert result["subject"] == "Interview", "the message the handle names was read"
    assert result["id"] == handle

def test_wrapping_changes_nothing_the_model_is_shown_about_a_tool(monkeypatch):
    def shown():
        return {name: (json.dumps(t.function_schema.json_schema, sort_keys=True),
                       t.takes_ctx, t.description) for name, t in _registered().items()}

    wrapped = shown()
    monkeypatch.setattr(handles, "wrap", lambda fn: fn)
    assert shown() == wrapped


# --- outside the tools -----------------------------------------------------------------


def test_the_attention_doer_is_shown_a_handle_for_a_long_id(conn):
    obs = Observation(source="mail", key="k", title="Interview invite",
                      ref={"message_id": OUTLOOK_ID, "thread_id": GMAIL}, task="Draft a reply.")

    text = prepare.describe(obs, conn)

    assert OUTLOOK_ID not in text and GMAIL in text
    handle = next(line.split(": ", 1)[1] for line in text.splitlines()
                  if line.startswith("message_id: "))
    assert handles.resolve(conn, handle) == OUTLOOK_ID


def test_a_handle_unused_for_months_is_forgotten(conn, monkeypatch):
    with conn:
        conn.execute("INSERT INTO handles (handle, full, used_at) VALUES"
                     " ('outlook#aaaaaaaaaa', 'old', datetime('now', '-120 days'))")
    monkeypatch.setattr(handles, "_pruned_at", None)

    handles.shorten(conn, {"id": OUTLOOK_ID})

    with pytest.raises(handles.UnknownHandle):
        handles.resolve(conn, "outlook#aaaaaaaaaa")
