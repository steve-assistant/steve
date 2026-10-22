"""A turn's writes are done once, however often the turn is run.

`run_agent` (agent/llm.py) runs a turn again from its start when a request
fails past the SDK's own retries, and an escalation replays it on a
stronger tier; both times the model asks again for what it asked before.
`act` keeps an effect once a turn, but not what a tool writes before or
without it: step 10's hostile scenarios found three 500s after
`remind_me` setting the reminder twice, and `create_routine` writes its
routine before its card. `agent/toolset/pace.py` `once` now keeps every
tool's first result for the turn, but a lookup's and the few that mean
something asked twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic_ai import Agent

from agent import reminders, routines, runtime
from agent.toolset import ASKED_FOR_ONLY, pace, register_tools
from agent.turn import Deps
from core import db


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    yield c
    c.close()


@pytest.fixture
def deps(conn):
    session = conn.execute("INSERT INTO sessions (channel) VALUES ('app')").lastrowid
    return Deps(conn=conn, session_id=session)


def _tools() -> dict:
    """Every tool `register_tools` gives, a lead's and the asked-for-only
    ones among them, as the model calls it."""
    agent = Agent("test", deps_type=Deps)
    register_tools(agent, allow=None, lead=True)
    asked_for = Agent("test", deps_type=Deps)
    register_tools(asked_for, allow=set(ASKED_FOR_ONLY), lead=True)
    return dict(agent._function_toolset.tools) | dict(asked_for._function_toolset.tools)


def _tomorrow(hour: int) -> str:
    day = datetime.now() + timedelta(days=1)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def test_every_tool_named_as_run_each_time_or_a_lookup_is_one_and_never_both():
    """A name that is no tool's exempts nothing, and hides a tool renamed
    out of the list, now done once a turn without anyone deciding it."""
    names = set(_tools())
    assert sorted((pace.READ | pace.AGAIN) - names) == []
    assert sorted(pace.READ & pace.AGAIN) == []


async def test_a_reminder_asked_for_again_in_a_turn_is_set_once_and_answered_the_same(deps):
    """As a retried turn asks: the same tool, the same arguments."""
    remind_me = _tools()["remind_me"].function
    asked = {"what": "Repot the fern", "when": _tomorrow(9)}

    first = await remind_me(_Ctx(deps), **asked)
    again = await remind_me(_Ctx(deps), **asked)

    assert first["ok"] and again == first
    assert [r.text for r in reminders.active(deps.conn)] == ["Repot the fern"]
    # Counted as called both times, as `act`'s own check counts it.
    assert deps.tools_used == ["remind_me", "remind_me"]


async def test_a_routine_asked_for_again_in_a_turn_is_made_once(deps):
    """`create_routine` makes its routine before any card, so `act` keeping
    the card once kept nothing: the replay made a second routine."""
    create_routine = _tools()["create_routine"].function
    asked = {"title": "Fern check", "instruction": "Look at the fern.", "at_time": "08:00",
             "days": ["mon"]}

    first = await create_routine(_Ctx(deps), **asked)
    again = await create_routine(_Ctx(deps), **asked)

    assert first["ok"] and again == first
    assert [r.title for r in routines.listed(deps.conn)] == ["Fern check"]


async def test_a_reminder_for_another_time_in_the_same_turn_is_a_second_reminder(deps):
    remind_me = _tools()["remind_me"].function

    await remind_me(_Ctx(deps), what="Repot the fern", when=_tomorrow(9))
    await remind_me(_Ctx(deps), what="Repot the fern", when=_tomorrow(17))

    assert len(reminders.active(deps.conn)) == 2


async def test_a_list_asked_for_again_after_a_write_in_the_same_turn_is_read_again(deps):
    """Kept once, the second list would hide what the turn just did."""
    tools = _tools()
    before = await tools["list_reminders"].function(_Ctx(deps))
    await tools["remind_me"].function(_Ctx(deps), what="Repot the fern", when=_tomorrow(9))

    after = await tools["list_reminders"].function(_Ctx(deps))

    assert "Repot the fern" not in str(before) and "Repot the fern" in str(after)


async def test_a_lookup_that_failed_is_looked_up_again_when_asked_again_in_the_turn(deps):
    """A lookup's answer is kept for the turn by `pace.wrap`, which keeps
    no failure. Kept once like a write, a timed-out search would answer
    every retry the model made in the turn with the same timeout."""
    asked: list[str] = []

    async def weather(ctx, place: str) -> dict:
        asked.append(place)
        return {"error": "timed out"} if len(asked) == 1 else {"ok": True, "forecast": "rain"}

    tool = pace.wrap(pace.once(weather))
    first = await tool(_Ctx(deps), place="Bergen")
    second = await tool(_Ctx(deps), place="Bergen")

    assert first == {"error": "timed out"} and second == {"ok": True, "forecast": "rain"}
    assert asked == ["Bergen", "Bergen"]
