"""A conversation turn can be heard as it is written, and a reply the user cut off is
kept as far as the user heard it (agent/loop.py `streamed`, `cut_short`)."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from agent import loop, runtime, toolset
from agent.loop import Conversation
from agent.turn import Deps
from core import db


async def _events(*events):
    for event in events:
        yield event


async def test_text_and_tools_reach_the_listeners_as_they_happen():
    texts, tools = [], []
    handle = loop.streamed(texts.append, tools.append)

    await handle(None, _events(
        FunctionToolCallEvent(part=ToolCallPart("weather", {})),
        PartStartEvent(index=0, part=TextPart("It's 60")),
        PartDeltaEvent(index=0, delta=TextPartDelta(" and clear.")),
    ))

    assert tools == ["weather"] and texts == ["It's 60", " and clear."]


async def test_a_listener_that_breaks_does_not_break_the_turn():
    def broken(_):
        raise RuntimeError("the speaker is gone")

    await loop.streamed(broken, broken)(None, _events(
        PartStartEvent(index=0, part=TextPart("Hi"))))


@pytest.fixture
def conn(tmp_path, monkeypatch, memories_repo):
    store = db.open_store(tmp_path / "streamed.db")

    async def stream(messages, info: AgentInfo):
        last = messages[-1].parts[-1]
        if getattr(last, "tool_name", None) is None and not any(
                getattr(p, "part_kind", "") == "tool-return" for p in messages[-1].parts):
            yield {0: DeltaToolCall(name="my_commitments", json_args="{}")}
            return
        for piece in ("You have no open ", "commitments right now. ", "Nice and clear."):
            yield piece

    def build_agent(role, **kwargs):
        from pydantic_ai import Agent

        agent = Agent(FunctionModel(stream_function=stream), deps_type=Deps,
                      system_prompt="test")
        toolset.register_tools(agent, allow=kwargs.get("allow"))
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)
    yield store
    store.close()


async def test_a_turn_is_heard_as_it_is_written(conn):
    texts, tools = [], []
    reply = await Conversation(conn, channel="voice").send(
        "what do I owe people?", on_text=texts.append, on_tool=tools.append)

    assert tools == ["my_commitments"]
    assert "".join(texts) == reply.text == "You have no open commitments right now. Nice and clear."


async def test_a_reply_the_user_cut_off_is_kept_as_far_as_the_user_heard_it(conn):
    convo = Conversation(conn, channel="voice")
    await convo.send("what do I owe people?", on_text=lambda _: None)

    convo.cut_short("You have no open commitments right now.")

    said = [p.content for m in convo._history if isinstance(m, ModelResponse)
            for p in m.parts if isinstance(p, TextPart)]
    assert said[-1].startswith("You have no open commitments right now. [the user cut in here")
    stored = conn.execute("SELECT content FROM messages WHERE role = 'assistant'"
                          " ORDER BY id DESC LIMIT 1").fetchone()["content"]
    assert stored == said[-1] and "Nice and clear" not in stored


def _scripted_runs(monkeypatch, *runs):
    """A model that does what `runs` says, one entry per request: a list of
    text pieces to stream and then, optionally, a tool to call ("escalate") or
    an exception to raise."""
    from pydantic_ai import Agent

    seen = {"requests": 0}

    async def stream(messages, info: AgentInfo):
        pieces, then = runs[min(seen["requests"], len(runs) - 1)]
        seen["requests"] += 1
        for piece in pieces:
            yield piece
        if isinstance(then, Exception):
            raise then
        if then == "escalate":
            yield {0: DeltaToolCall(name="escalate", json_args='{"reason": "harder"}')}

    def build_agent(role, **kwargs):
        agent = Agent(FunctionModel(stream_function=stream), deps_type=Deps,
                      system_prompt="test")
        toolset.register_tools(agent, allow=kwargs.get("allow"))
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)


async def test_a_retried_call_tells_the_listener_it_is_starting_over(conn, monkeypatch):
    """8 October 2026 audit, finding 8: `run_agent` retries the whole run, which
    streams again from its first word; a voice that had spoken the first
    attempt's sentences said them twice."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    from agent import llm

    monkeypatch.setattr(llm, "backoff_delay", lambda attempt: 0.0)
    _scripted_runs(monkeypatch,
                   (["The answer is here. "], UnexpectedModelBehavior("truncated")),
                   (["The answer is here. ", "And the rest."], None))
    events: list[str] = []

    reply = await Conversation(conn, channel="voice").send(
        "what is it?", on_text=lambda piece: events.append(f"text:{piece}"),
        on_restart=lambda: events.append("restart"))

    assert events == ["text:The answer is here. ", "restart", "text:The answer is here. ",
                      "text:And the rest."]
    assert reply.text == "The answer is here. And the rest."


async def test_a_turn_replayed_on_a_stronger_model_tells_the_listener(conn, monkeypatch):
    _scripted_runs(monkeypatch,
                   (["Let me think about that properly. "], "escalate"),
                   (["The answer is forty two, as expected."], None))
    events: list[str] = []

    reply = await Conversation(conn, channel="voice").send(
        "what is it?", on_text=lambda piece: events.append(f"text:{piece}"),
        on_restart=lambda: events.append("restart"))

    assert events == ["text:Let me think about that properly. ", "restart",
                      "text:The answer is forty two, as expected."]
    assert reply.escalated_from is not None


async def test_a_listener_that_cannot_take_a_restart_does_not_break_the_turn(conn, monkeypatch):
    def broken():
        raise RuntimeError("the speaker is gone")

    _scripted_runs(monkeypatch, (["Thinking. "], "escalate"), (["Forty two."], None))

    reply = await Conversation(conn, channel="voice").send(
        "what is it?", on_text=lambda piece: None, on_restart=broken)

    assert reply.text == "Forty two."


async def test_the_desks_reply_hands_all_three_stream_hooks_to_the_turn(tmp_path, monkeypatch):
    """`voice_reply` is the desk's whole way into a turn: a hook it drops is
    one the voice never hears."""
    from types import SimpleNamespace

    from agent.worker import answering

    got = {}

    class _Convo:
        session_id = 7

        async def send(self, text, **hooks):
            got.update(hooks)
            return SimpleNamespace(text="ok", role=SimpleNamespace(value="converse"),
                                   cost_usd=0.0, tools_used=[], deferred_jobs=[])

    async def not_an_approval(text, channel, card=None, spoken=False):
        return None

    monkeypatch.setattr(answering, "intercept_approval", not_an_approval)
    monkeypatch.setattr(answering, "recent_session", lambda channel: 7)
    monkeypatch.setattr(answering, "conversation", lambda session, channel="tray": _Convo())
    # The reply is read against the cards waiting (agent/cardwords.py).
    store = db.open_store(tmp_path / "worker.db")
    monkeypatch.setattr(runtime, "conn", store)
    on_text, on_tool, on_restart = (lambda _: None), (lambda _: None), (lambda: None)

    await answering.voice_reply("hi", channel="voice", on_text=on_text, on_tool=on_tool,
                                on_restart=on_restart)
    store.close()

    assert got == {"on_text": on_text, "on_tool": on_tool, "on_restart": on_restart}


def test_the_note_a_cut_off_reply_carries_is_not_a_part_of_any_reply():
    """`Conversation.cut_short` writes it into the history, where a model can
    copy it -- anywhere, not only at the front."""
    from agent import history

    assert history.clean(f"Rain at three. {history.CUT_NOTE}") == "Rain at three."
    assert history.clean(f"{history.CUT_NOTE} Rain at three.") == "Rain at three."
    assert history.clean("Rain at three.") == "Rain at three."


def test_the_live_voice_stays_quiet_for_the_room_and_asks_rather_than_guesses():
    from voice import desk_call

    said = " ".join(desk_call.INSTRUCTIONS.split())
    assert "say nothing" in said and "ask them to say it again" in said
    assert "never hand a request you only half heard" in said
