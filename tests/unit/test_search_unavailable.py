"""A search with no answer is said to the model, and the turn goes on.

Until step 3 search was Brave's, and only a missing key was answered for:
anything else Brave raised -- an outage, a rate limit, a timeout -- left the
tool as an exception, and the whole turn broke with it. Search is OpenAI's
now (core/websearch.py), and when it has no answer the tool says that web
search is unavailable, and why, as its result. There is no second search
engine to try.
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import agent.loop as loop
from agent.loop import Conversation
from core.websearch import SearchUnavailable
from tools import web

ANSWER = "I couldn't search just now, so I can't say which bakeries are open."


@pytest.fixture
def store(memories_repo, store):
    return store


@pytest.mark.parametrize("tool", ["web_search", "search_and_read"])
async def test_a_search_with_no_answer_is_said_and_the_turn_goes_on(store, monkeypatch, tool):
    returned: list = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        back = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not back:
            return ModelResponse(parts=[ToolCallPart(tool, {"query": "bakeries open now"})])
        returned.append(back[-1].content)
        return ModelResponse(parts=[TextPart(ANSWER)])

    async def search(query, count=5):
        raise SearchUnavailable("OpenAI could not be reached")

    monkeypatch.setattr(loop, "model_for", lambda role: FunctionModel(respond))
    monkeypatch.setattr(web, "search", search)

    reply = await Conversation(store, channel="signal").send("any bakeries open now?")

    assert reply.text == ANSWER
    assert returned == [{"error": "web search is unavailable: OpenAI could not be reached",
                         "kind": "unavailable"}]
    assert store.execute("SELECT COUNT(*) FROM issues WHERE kind = 'turn_broke'"
                         ).fetchone()[0] == 0
