"""A run that spends its tool calls answers with what it found, a lead that
could not ask Jev is not told the task splits, and a site that refused twice
is not asked again.

30 September 2026: two research jobs about who leads Northwind's New York office.
northwind.example answered 403 to every page and LinkedIn 999 to every profile, and
all four Northwind workers reworded one search until they reached their 16 calls,
when each was stopped mid-run and reported only "went round in tool calls
and was stopped" -- what their searches had turned up never reached the
lead. In the second job Jev's address would not resolve, and the lead, told
the task "splits into independent parts", split one question three ways.

Each model here is a FunctionModel behind the real `build_agent`, so the
tools it is offered are the ones a live run would be.
"""

from __future__ import annotations

from collections import Counter

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import agent.loop as loop
from agent import workers
from agent.loop import Conversation
from tools import web

WRITE_UP = ("Kai Morrow appears in Northwind New York partner listings; who leads the "
            "office is still unverified, and northwind.example refused every page.")


@pytest.fixture
def store(monkeypatch, memories_repo, store):
    return store


@pytest.fixture
def searcher(monkeypatch):
    """A model that searches as long as it is offered a tool -- two at a
    time, as the workers did -- and writes up only once it is offered none."""
    state = {"offered": [], "searches": 0}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        state["offered"].append(len(info.function_tools))
        if info.function_tools:
            return ModelResponse(parts=[
                ToolCallPart("web_search", {"query": f"Northwind New York office leader {i}"})
                for i in range(2)])
        return ModelResponse(parts=[TextPart(WRITE_UP)])

    async def search(query, count=5, **kwargs):
        state["searches"] += 1
        return [web.SearchResult(title="Kai Morrow - Northwind",
                                 url="https://www.linkedin.com/in/x",
                                 description="Managing Director & Partner, New York")]

    monkeypatch.setattr(loop, "model_for", lambda role: FunctionModel(respond))
    monkeypatch.setattr(web, "search", search)
    return state


async def test_a_worker_out_of_tool_calls_writes_up_what_it_found(store, searcher):
    """Stopped at its limit, it handed the lead nothing; now its tools are
    taken away and it answers, marked as cut short."""
    crew = workers.Crew(store, session_id=1, job_id=328)

    crew.start("Find who leads Northwind's New York office.", name="Northwind NYC")
    said = await crew.wait(timeout_s=30)

    [worker] = said["workers"]
    assert worker["status"] == "done", worker
    assert WRITE_UP in worker["result"]
    assert worker["result"].startswith("(It used all its tool calls")
    assert searcher["searches"] >= workers.WORKER_TOOL_CALLS
    assert searcher["offered"][-1] == 0, "the last request was offered no tools"


async def test_a_conversation_out_of_tool_calls_answers_and_the_ledger_hears_of_it(
        store, searcher):
    """The same for their own turns: an answer, not "I got stuck repeating
    myself" -- and still a ledger row, since needing that many is news."""
    reply = await Conversation(store, channel="signal").send("who leads Northwind's NYC office?")

    assert reply.text == WRITE_UP
    row = store.execute("SELECT summary FROM issues WHERE kind = 'turn_loop'").fetchone()
    assert row is not None and f"used all {loop.HOP_TOOL_CALLS} tool calls" in row["summary"]


async def test_a_worker_gets_the_budget_and_its_leads_count_of_refusing_sites(
        store, monkeypatch):
    import agent.llm

    given = []

    async def run_agent(agent_, prompt, role, *, deps, **kwargs):
        given.append((deps, kwargs["usage_limits"]))
        return type("R", (), {"output": "ok"})(), None

    monkeypatch.setattr(loop, "build_agent", lambda role, **kwargs: object())
    monkeypatch.setattr(agent.llm, "run_agent", run_agent)
    crew = workers.Crew(store, session_id=1, job_id=7)

    crew.start("one part")
    await crew.wait(timeout_s=5)

    [(deps, limits)] = given
    assert deps.tool_budget == workers.WORKER_TOOL_CALLS and deps.refused is crew.refused
    assert limits.tool_calls_limit > workers.WORKER_TOOL_CALLS, "the hard stop sits above"


async def test_a_lead_skips_a_site_its_workers_found_refusing(store, monkeypatch):
    """One count for the lead and every worker: a site that turned two of
    them away is not asked again by the lead. The fetch itself is fenced
    by the conftest, so reaching it would fail this test."""
    returned = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart):
                returned.append(part.content)
                return ModelResponse(parts=[TextPart("northwind.example would not let me in.")])
        return ModelResponse(parts=[
            ToolCallPart("read_page",
                         {"url": "https://www.northwind.example/offices/new-york/default"})])

    monkeypatch.setattr(loop, "model_for", lambda role: FunctionModel(respond))
    crew = workers.Crew(store, session_id=1, job_id=328)
    crew.refused.update({"northwind.example": 2})

    await Conversation(store, channel="signal").send("try Northwind's own page", crew=crew)

    [page] = returned
    assert "northwind.example has refused 2 fetches" in page["error"]


async def test_a_turn_without_a_crew_counts_refusals_of_its_own(store, monkeypatch):
    """No crew, no sharing -- but the count still runs for the turn.

    The user names the pages: a refused fetch marks the conversation too,
    since its redirect's address and its content type are the site's words,
    and a marked one opens only addresses it was shown. Until step 11 this
    test let the model make up the second address after the first was
    refused, which only an unmarked conversation allows."""
    returned = []
    urls = iter(["https://www.northwind.example/a", "https://www.northwind.example/b",
                 "https://www.northwind.example/c"])

    def respond(messages, info: AgentInfo) -> ModelResponse:
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart):
                returned.append(part.content)
        url = next(urls, None)
        if url is None:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("read_page", {"url": url})])

    fetched = []

    async def fetch(url, **kwargs):
        fetched.append(url)
        return web.PageContent(url=url, title=None, text="", error="HTTP 403")

    monkeypatch.setattr(loop, "model_for", lambda role: FunctionModel(respond))
    monkeypatch.setattr(web, "fetch", fetch)

    await Conversation(store, channel="signal").send(
        "read https://www.northwind.example/a, https://www.northwind.example/b and "
        "https://www.northwind.example/c")

    assert fetched == ["https://www.northwind.example/a", "https://www.northwind.example/b"]
    assert "refused 2 fetches" in returned[-1]["error"]


def test_the_refusal_count_is_per_turn_not_kept():
    """A site may let us in tomorrow: nothing outlives the turn's Counter."""
    from agent.turn import Deps

    assert Deps(conn=None, session_id=1).refused == Counter()
    assert Deps(conn=None, session_id=1).refused is not Deps(conn=None, session_id=1).refused
