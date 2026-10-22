"""Lookups the model asks for together run together -- the ones that share
nothing -- and a turn does not look the same thing up twice. The maintainer, 5 October
2026: "sure we can parallelize some of the searches, not everything needs to
be since some things can work better sequentially"."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.models import Role
from agent.toolset import pace
from agent.turn import DEPTH_ASKED, Deps, escalation_target


class _InFlight:
    """How many calls were running at once, at most. Counted rather than
    timed: these asserted elapsed time (three 0.3 s calls "together" in
    under 0.6 s), and a wall-clock upper bound fails on a loaded laptop or
    in the 6 GB sandbox under -n 3 with the code doing the right thing --
    the mail-search concurrency test failed exactly so on 5 and 6 October
    and helped sink attempt #84."""

    def __init__(self):
        self.now = 0
        self.most = 0


def _named(name: str, calls: list, flight: _InFlight | None = None):
    flight = flight or _InFlight()

    async def tool(ctx, query: str) -> str:
        calls.append(query)
        flight.now += 1
        flight.most = max(flight.most, flight.now)
        try:
            # Long enough that calls let run together overlap: they all
            # start in one turn of the loop, before any timer fires.
            await asyncio.sleep(0.02)
        finally:
            flight.now -= 1
        return f"{name}: {query}"

    tool.__name__ = name
    return pace.wrap(tool)


class _Ctx:
    def __init__(self, cache=None):
        self.deps = type("D", (), {"turn_cache": cache})()


async def _three_at_once(name: str) -> int:
    """The most of three calls, asked for together, that ran at once."""
    calls: list = []
    flight = _InFlight()
    tool = _named(name, calls, flight)
    await asyncio.gather(*(tool(_Ctx(), query=f"q{i}") for i in range(3)))
    assert len(calls) == 3
    return flight.most


async def test_web_lookups_asked_together_run_together():
    assert await _three_at_once("search_and_read") == 3


async def test_calls_sharing_a_client_take_turns():
    """Google's clients are shared between threads, and httplib2 is not
    thread-safe: two Gmail calls at once can corrupt each other."""
    assert await _three_at_once("search_email") == 1


async def test_different_tools_on_one_client_take_turns_too():
    """A search and a read of their mail at once share Gmail's one client."""
    calls: list = []
    flight = _InFlight()
    search, read = _named("search_email", calls, flight), _named("read_email", calls, flight)
    await asyncio.gather(search(_Ctx(), query="dana"), read(_Ctx(), query="m1"))
    assert sorted(calls) == ["dana", "m1"] and flight.most == 1


async def test_a_tool_not_named_anywhere_takes_turns():
    """A new tool is safe before it is fast."""
    assert pace.group_of("some_new_tool") == "tool:some_new_tool"
    assert await _three_at_once("some_new_tool") == 1


async def test_the_same_lookup_twice_in_a_turn_is_answered_from_the_first():
    calls: list = []
    tool = _named("web_search", calls)
    cache: dict = {}
    first = await tool(_Ctx(cache), query="campsites near Pittsburgh")
    again = await tool(_Ctx(cache), query="campsites near Pittsburgh")
    other = await tool(_Ctx(cache), query="something else")
    assert first == again and calls == ["campsites near Pittsburgh", "something else"]
    assert other == "web_search: something else"


async def test_a_failed_lookup_is_asked_again():
    calls: list = []

    async def read_email(ctx, message_id: str) -> dict:
        calls.append(message_id)
        return {"error": "Outlook is not answering"}

    tool = pace.wrap(read_email)
    cache: dict = {}
    await tool(_Ctx(cache), message_id="m1")
    await tool(_Ctx(cache), message_id="m1")
    assert calls == ["m1", "m1"]


async def test_an_effect_is_never_answered_from_a_cache():
    calls: list = []

    async def send_email(ctx, to: str) -> dict:
        calls.append(to)
        return {"queued": True}

    tool = pace.wrap(send_email)
    cache: dict = {}
    await tool(_Ctx(cache), to="dana@example.com")
    await tool(_Ctx(cache), to="dana@example.com")
    assert calls == ["dana@example.com", "dana@example.com"]


@pytest.mark.parametrize("asked, first, second", [
    ("which campsite is closer?", Role.REASON, None),
    ("think hard about which campsite is better", Role.ESCALATE, Role.ESCALATE),
    ("take your time on this one", Role.ESCALATE, Role.ESCALATE),
    ("do a deep dive on her background", Role.ESCALATE, Role.ESCALATE),
])
def test_high_effort_is_for_when_the_user_asks(asked, first, second):
    assert escalation_target(Role.CONVERSE, None, asked) is first
    assert escalation_target(Role.REASON, None, asked) is second


def test_depth_is_not_read_into_ordinary_words():
    for said in ("I think I'll go hiking", "the hard drive is full", "take your bike"):
        assert not DEPTH_ASKED.search(said)


async def test_an_escalated_tier_is_told_what_was_already_looked_up(
        monkeypatch, tmp_path, memories_repo):
    """On 5 October an escalation threw away 35 s of lookups and spent 111 s
    more. The tier taking over is told what was asked, and gets it back at
    once."""
    from agent import loop
    from agent.loop import Conversation
    from core import db

    store = db.open_store(tmp_path / "t.db")

    fetched: list[str] = []
    prompts: list[str] = []

    async def model(messages, info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        returned = [p for p in last.parts if isinstance(p, ToolReturnPart)]
        asked = " ".join(str(p.content) for p in last.parts
                         if not isinstance(p, ToolReturnPart))
        if asked:
            prompts.append(asked)
        if returned and returned[0].tool_name == "web_search":
            if len(prompts) == 1:
                return ModelResponse(parts=[ToolCallPart("escalate", {"reason": "hard"})])
            return ModelResponse(parts=[TextPart("Amtrak 5:10 pm gives a 40 minute buffer.")])
        return ModelResponse(parts=[ToolCallPart("web_search", {"query": "amtrak times"})])

    def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
        agent = Agent(FunctionModel(model), deps_type=Deps, instructions="house")

        async def web_search(ctx, query: str) -> str:
            fetched.append(query)
            return "5:10 pm, 6:40 pm"

        async def escalate(ctx, reason: str) -> str:
            from agent.turn import EscalationRequested

            raise EscalationRequested(Role.REASON, reason)

        agent.tool(pace.wrap(web_search))
        agent.tool(escalate)
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)
    reply = await Conversation(store, channel="signal").send("What Amtrak do I need?")

    assert reply.role is Role.REASON and "5:10" in reply.text
    assert fetched == ["amtrak times"], "the escalated tier looked it up again"
    assert "already" not in prompts[0] and "web_search" in prompts[1]
    store.close()
