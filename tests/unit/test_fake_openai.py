"""The fake OpenAI answers as OpenAI does, to the clients Steve really uses.

The end-to-end tests run a whole worker against `tests/fakes/openai.py`.
If the fake's answers drifted from the shapes OpenAI's SDK and pydantic-ai
parse, every scenario would fail in the client, far from the cause, or
worse, pass on a path the real service never takes. So each way Steve
converses is driven here through the client that ships: pydantic-ai's two
models, whole and streamed, with a tool call; a structured answer; web
search, read by `core/websearch.py` itself; and the screen's extraction.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, Field

from core import websearch
from tests.fakes.openai import GOOD, FakeOpenAI, Page, Rule, smallest

WEATHER = [Rule(said="weather", offered="weather", call="weather", arguments={"city": "Oslo"}),
           Rule(result="sunny", say="It is sunny in Oslo.")]


class Verdict(BaseModel):
    ok: bool
    score: int = Field(ge=1, le=5)
    why: str
    kind: str | None = None


class Extraction(BaseModel):
    facts: list[str]
    asks: list[str]


def _model(kind: str, url: str):
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(base_url=url, api_key=GOOD)
    model = OpenAIResponsesModel if kind == "responses" else OpenAIChatModel
    return model("m", provider=provider)


@pytest.mark.parametrize("kind", ["responses", "chat"])
@pytest.mark.parametrize("streamed", [False, True])
def test_both_of_pydantic_ais_models_call_a_scripted_tool_and_read_the_scripted_answer(
        kind, streamed):
    """REASON and ESCALATE go through Responses, the quick roles through
    Chat Completions (agent/provider.py), and a turn streams. Each pair
    must survive a tool call and its result, or no scenario's tool runs."""
    from pydantic_ai import Agent

    fake = FakeOpenAI(WEATHER, otherwise="OK.")
    with fake.running() as url:
        agent = Agent(_model(kind, url))
        ran: list[str] = []

        @agent.tool_plain
        def weather(city: str) -> str:
            """The weather in a city."""
            ran.append(city)
            return f"sunny in {city}"

        async def drain(ctx, stream):
            async for _ in stream:
                pass

        handler = drain if streamed else None
        result = asyncio.run(agent.run("what's the weather?", event_stream_handler=handler))
    assert ran == ["Oslo"]
    assert result.output == "It is sunny in Oslo."


@pytest.mark.parametrize("kind", ["responses", "chat"])
def test_a_role_that_asks_for_a_structured_answer_gets_the_smallest_its_schema_takes(kind):
    """Triage, the mailbox, research and the coder's judges each ask for
    a typed answer. With no rule for one, the fake still gives an answer
    the role can read, so a scenario fails only where it means to."""
    from pydantic_ai import Agent

    with FakeOpenAI([], otherwise="OK.").running() as url:
        result = asyncio.run(Agent(_model(kind, url), output_type=Verdict).run("judge it"))
    assert result.output == Verdict(ok=False, score=1, why="", kind=None)


def test_the_screens_extraction_parses_the_smallest_answer_its_schema_takes():
    from openai import OpenAI

    with FakeOpenAI([], otherwise="OK.").running() as url:
        parsed = OpenAI(base_url=url, api_key=GOOD).responses.parse(
            model="m", input="extract", text_format=Extraction, store=False)
    assert parsed.output_parsed == Extraction(facts=[], asks=[])


def test_a_web_search_finds_the_pages_that_hold_the_querys_words_most_first_and_cites_them():
    """Search is read by `core/websearch.py`'s own `read`, citations and
    all: a page found must come back with the line that cited it, and a
    query that matches nothing must find nothing, not every page."""
    from openai import AsyncOpenAI

    pages = [Page("https://trains.example/", "Trains", "Oslo trains run late."),
             Page("https://ferries.example/oslo", "Oslo ferry timetable",
                  "Ferries leave Oslo hourly."),
             Page("https://cats.example/", "Cats", "Nothing about boats.")]

    async def search(url: str, query: str) -> list[websearch.Found]:
        async with AsyncOpenAI(base_url=url, api_key=GOOD) as client:
            answer = await client.responses.create(**websearch.request_for(query, 5))
        return websearch.read(answer.model_dump(mode="json"), 5)

    with FakeOpenAI([], pages=pages).running() as url:
        found = asyncio.run(search(url, "ferry timetable oslo"))
        nothing = asyncio.run(search(url, "quantum gravity"))
    assert [(f.url, f.description) for f in found] == [
        ("https://ferries.example/oslo", "Oslo ferry timetable: Ferries leave Oslo hourly."),
        ("https://trains.example/", "Trains: Oslo trains run late.")]
    assert nothing == []


def test_the_first_matching_rule_answers_and_a_tools_result_never_matches_what_was_said():
    """A rule for the user's words must not fire again on the tool's result
    that follows, or a scripted tool call would loop until the turn's
    limit."""
    from openai import OpenAI

    rules = [Rule(said="remind", call="set_reminder", arguments={"text": "call"}),
             Rule(said="remind", say="never reached"),
             Rule(result="", endpoint="chat", say="Done.")]
    with FakeOpenAI(rules, otherwise="OK.").running() as url:
        client = OpenAI(base_url=url, api_key=GOOD)
        tools = [{"type": "function", "function": {"name": "set_reminder", "parameters": {}}}]
        first = client.chat.completions.create(
            model="m", tools=tools, messages=[{"role": "user", "content": "Remind me at 5"}])
        call = first.choices[0].message.tool_calls[0]
        after = client.chat.completions.create(model="m", tools=tools, messages=[
            {"role": "user", "content": "Remind me at 5"},
            first.choices[0].message.model_dump(exclude_none=True),
            {"role": "tool", "tool_call_id": call.id, "content": "set for 5 pm; remind"}])
        other = client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hello"}])
    assert (call.function.name, call.function.arguments) == ("set_reminder", '{"text": "call"}')
    assert after.choices[0].message.content == "Done."
    assert other.choices[0].message.content == "OK."


def test_every_request_is_kept_with_its_headers_and_body():
    """The canaries look for a secret in what reached the model: a request
    the fake answered but did not keep would hide one."""
    from openai import OpenAI

    fake = FakeOpenAI([], otherwise="OK.")
    with fake.running() as url:
        client = OpenAI(base_url=url, api_key=GOOD)
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "a1"}])
        client.responses.create(model="m", input="b2", store=False)
        client.embeddings.create(model="e", input="c3")
    kept = [(k.path, k.headers["authorization"], k.body) for k in fake.requests]
    assert [path for path, _, _ in kept] == ["/v1/chat/completions", "/v1/responses",
                                            "/v1/embeddings"]
    assert all(auth == f"Bearer {GOOD}" for _, auth, _ in kept)
    assert ["a1" in str(kept[0][2]), "b2" in str(kept[1][2]), "c3" in str(kept[2][2])] == [
        True, True, True]


@pytest.mark.parametrize("schema,value", [
    ({"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
      "required": ["a"]}, {"a": ""}),
    ({"type": "integer", "minimum": 3, "maximum": 9}, 3),
    ({"type": "array", "items": {"type": "string"}, "minItems": 2}, ["", ""]),
    ({"anyOf": [{"type": "string"}, {"type": "null"}]}, None),
    ({"enum": ["low", "high"]}, "low"),
    ({"$defs": {"P": {"type": "object", "properties": {"n": {"type": "number"}},
                      "required": ["n"]}},
      "type": "object", "properties": {"p": {"$ref": "#/$defs/P"}}, "required": ["p"]},
     {"p": {"n": 0.0}}),
])
def test_the_smallest_answer_takes_every_bound_its_schema_sets(schema, value):
    assert smallest(schema) == value
