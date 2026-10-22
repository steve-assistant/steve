"""A turn that fails says so, stays in the conversation, and is answered by the
worker that knows what broke.

27 September 2026, on Signal: "add Game Theory to my calendar". Google's
sign-in had expired the night before, the calendar create was missing its
guard, and the exception took the whole turn down. The user was told "my
laptop is offline", and the question joined no history, so "laptop is awake, try again"
reached a model that had never seen it -- and it retried the request before,
a search for somebody else's coffee chat.
"""

from __future__ import annotations

import pytest
from google.auth.exceptions import RefreshError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import actions, loop, toolset
from agent.loop import Conversation, TurnFailed
from agent.turn import Deps
from agent.worker import answering
from core import db

GAME_THEORY = ("we have game theory class from 10:05 - 11:25 on 10/1 at room 2410,"
               " add to calendar")
ADD = {"summary": "Game Theory", "start": "2026-10-01T10:05", "duration_minutes": 80,
       "location": "Room 2410"}


@pytest.fixture
def talk(tmp_path, monkeypatch, memories_repo):
    """A Signal conversation whose model follows a script, one step a call:
    "add" calls create_event, "boom" raises, anything else is the reply."""
    conn = db.open_store(tmp_path / "turns.db")
    state = {"script": [], "calls": 0, "seen": [], "returned": []}

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        state["calls"] += 1
        state["seen"].append(messages)
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart):
                state["returned"].append(part.content)
                return ModelResponse(parts=[TextPart("I couldn't add it: " + str(part.content))])
        step = state["script"].pop(0)
        if step == "boom":
            raise RuntimeError("the provider gave up")
        if step == "add":
            return ModelResponse(parts=[ToolCallPart("create_event", ADD)])
        return ModelResponse(parts=[TextPart(step)])

    def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    on=None):
        from pydantic_ai import Agent

        agent = Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")
        toolset.register_tools(agent, allow=allow)
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)
    state["conn"] = conn
    state["open"] = lambda: Conversation(conn, channel="signal")
    yield state
    conn.close()


async def test_adding_an_event_after_the_sign_in_expired_is_said_not_fatal(talk, monkeypatch):
    """The calendar the morning after Google expired the sign-in: Google's
    own provider on the slot, its client refusing the refresh token."""
    from modules.google_cloud import calendar as google_calendar
    from modules.google_cloud.provider import GoogleCalendar
    from tests.fakes import google as fake_google
    from tests.gate import slotfake

    def signed_out():
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    created = []
    monkeypatch.setattr(google_calendar, "_calendar", signed_out)
    monkeypatch.setattr(google_calendar, "create",
                        lambda *args, **kwargs: created.append(args) or {"id": "new"})
    slotfake.plug(monkeypatch, "calendar", google=GoogleCalendar(fake_google.Context()))
    talk["script"] = ["add"]

    reply = await talk["open"]().send(GAME_THEORY)

    assert "sign in again" in reply.text and "python -m" not in reply.text
    assert created == [], "a failed duplicate check must stop the create"


async def test_an_executor_that_raises_is_a_failure_the_model_reads(talk, monkeypatch):
    """The contract is "report, never raise"; `act` holds it for the next
    executor that forgets."""
    async def raises(**kwargs):
        raise ValueError("an executor that forgot the rule")

    monkeypatch.setitem(actions.EXECUTORS, "create_event", raises)
    talk["script"] = ["add"]

    reply = await talk["open"]().send(GAME_THEORY)

    assert talk["returned"][0]["ok"] is False
    assert "an executor that forgot the rule" in talk["returned"][0]["error"]
    assert reply.text.startswith("I couldn't add it")


async def test_a_turn_that_breaks_is_still_in_the_conversation(talk):
    convo = talk["open"]()
    talk["script"] = ["boom", "On it: Game Theory, Oct 1, 10:05."]

    with pytest.raises(TurnFailed) as broke:
        await convo.send(GAME_THEORY)
    assert "try again" in broke.value.text

    await convo.send("Laptop is awake try again")

    again = " ".join(str(m) for m in talk["seen"][-1])
    assert "game theory class" in again, "the retry reached a model that never saw the ask"
    stored = [r["content"] for r in talk["conn"].execute(
        "SELECT content FROM messages WHERE role = 'assistant' ORDER BY id")]
    assert stored[0] == broke.value.text


async def test_a_broken_turn_is_answered_with_what_broke_not_an_error(monkeypatch):
    """Raised, it left the worker as an error, and the user was told "my
    laptop is offline" instead of what broke."""
    class _Broken:
        session_id = 7

        async def send(self, text):
            raise TurnFailed("Something broke on my side before I could finish that.")

    async def not_an_approval(text, channel, card=None, spoken=False):
        return None

    monkeypatch.setattr(answering, "intercept_approval", not_an_approval)
    monkeypatch.setattr(answering, "recent_session", lambda channel: 7)
    monkeypatch.setattr(answering, "conversation", lambda session, channel="tray": _Broken())

    text, meta = await answering.voice_reply(GAME_THEORY, channel="signal")

    assert text.startswith("Something broke on my side") and meta["failed"] is True
