"""What a turn did is the list of tools that ran in it (`Deps.tools_used`).

The answer check reads it before letting "Added it to your calendar" stand
(agent/verify.py), and the turn's record and the attention loop read it as
what was done. Until 11 October 2026 each tool appended its own name as its
first line; now `_recorded` in agent/toolset notes every tool where it is
registered. These pin that from the model's side: a tool that runs is noted
once, and one stopped before it ran is not.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import Agent

from agent import handles
from agent.toolset import register_tools
from agent.turn import Deps
from core import db

OUTLOOK_ID = "outlook:" + "00000000DF29F64E1A2B3C4D5E6F" * 5 + "0000669016560000"


class _Ctx:
    def __init__(self, conn):
        self.deps = Deps(conn=conn, session_id=1)


@pytest.fixture
def conn():
    c = db.open_store()
    yield c
    c.close()


def _tools():
    agent = Agent("test", deps_type=Deps)
    register_tools(agent)
    return agent._function_toolset.tools


def test_a_tool_the_model_calls_is_noted_once_in_what_the_turn_did(conn, monkeypatch):
    """No tool notes itself any more, so a tool left off the list by the
    registration would do things the answer check could not see."""
    from modules.outlook_classic import outlook

    monkeypatch.setattr(outlook, "read", lambda message_id, body_chars=4_000: {
        "id": message_id, "subject": "Interview", "internal_date": 0})
    ctx = _Ctx(conn)
    handle = handles.shorten(conn, {"id": OUTLOOK_ID})["id"]

    asyncio.run(_tools()["read_email"].function(ctx, message_id=handle))

    assert ctx.deps.tools_used == ["read_email"]


def test_a_change_stopped_before_it_ran_is_not_noted_as_done(conn):
    """A handle that names nothing stops the call before the tool runs
    (agent/handles.py). Noted anyway, `update_event` would back "Moved it to
    three" in the reply although nothing was moved."""
    ctx = _Ctx(conn)

    result = asyncio.run(_tools()["update_event"].function(
        ctx, event_id="google#0123456789", start="2026-10-12T15:00"))

    assert result["ok"] is False and "Search again" in result["error"]
    assert ctx.deps.tools_used == []
