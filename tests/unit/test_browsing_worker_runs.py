"""A background task may start a browsing worker -- one the user asked for, and
since 11 October 2026 one the agent set itself -- and so may the doer's and
the night's research turns; the worker runs with its own tools and its own
browser, and leaves nothing behind.

What a browsing worker may and may not do is pinned in the frozen gate
(tests/gate/test_browsing_workers.py). These pin the plumbing: which tasks
get a crew that may browse, how the worker is run, and that its browser
and its place among the two are given back however it ends.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import core.browser
from agent import runtime, workers
from agent.browser import session as sessions
from agent.turn import Deps
from core import db

PAGE = "https://93.184.216.34/hours"


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    monkeypatch.setattr(core.browser, "enabled", lambda: True)
    monkeypatch.setattr(core.browser, "background", lambda: True)
    monkeypatch.setattr(workers, "_browsing", set())
    yield c
    c.close()


def _session(conn, channel: str) -> int:
    with conn:
        return int(conn.execute("INSERT INTO sessions (channel) VALUES (?)",
                                (channel,)).lastrowid)


@pytest.fixture
def made(monkeypatch):
    """`Conversation` replaced in the handlers: what each task's lead got."""
    from agent.worker import handlers

    seen: list[dict] = []

    class Convo:
        def __init__(self, c, **kw):
            self.session_id = 1
            seen.append({"init": kw})

        async def send(self, text, **kw):
            seen[-1]["crew"] = kw.get("crew")
            return type("R", (), {"text": "done", "cost_usd": 0.0})()

        def end(self):
            pass

    async def no_split(task):
        return workers.Split(offer=False)

    monkeypatch.setattr(handlers, "Conversation", Convo)
    monkeypatch.setattr(workers, "worth_splitting", no_split)
    return seen


async def test_every_background_task_gets_a_crew_that_may_browse_though_it_does_not_split(
        conn, made):
    """Theirs, and since 11 October 2026 one the agent set itself (the user's call). What
    a worker may open differs: their task's crew has seen the pages the user wrote;
    the agent's own has seen nothing, an address in its own prompt included,
    so its workers start from what its searches return."""
    from agent import jobs
    from agent.worker import handlers

    session = _session(conn, "signal")
    with conn:
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)",
                     (session, f"What are the museum's hours? {PAGE}"))
    theirs = jobs.enqueue(conn, kind="research", prompt="Read the museum's hours page.",
                       session_id=session)
    own = jobs.enqueue(conn, kind="research", prompt=f"Research for the night: {PAGE}")

    await handlers.handle_research(jobs.get(conn, theirs))
    await handlers.handle_research(jobs.get(conn, own))

    for task in made:
        crew = task["crew"]
        assert crew is not None and crew.browsing and not crew.split
        assert "start_browsing_worker" in task["init"]["note"]
    assert made[0]["crew"].seen == {PAGE}
    assert made[1]["crew"].seen == set(), "the agent's own words open nothing"


def test_a_lead_is_shown_only_what_its_crew_may_do(conn):
    from agent.toolset import offered

    class Tool:
        def __init__(self, name):
            self.name = name

    crew = workers.Crew(conn, 1, split=False, browsing=True)
    ctx = type("C", (), {"deps": Deps(conn=conn, session_id=1, channel="job", crew=crew),
                         "usage": type("U", (), {"tool_calls": 0})()})()
    names = ["start_worker", "start_browsing_worker", "wait_for_workers", "web_search"]

    shown = asyncio.run(offered(ctx, [Tool(n) for n in names]))

    assert [t.name for t in shown] == ["start_browsing_worker", "wait_for_workers",
                                       "web_search"]


async def test_a_browsing_worker_runs_with_its_own_tools_and_browser_and_gives_both_back(
        conn, monkeypatch):
    from agent import loop, toolset

    given: dict = {}

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        given.setdefault("tools", sorted(t.name for t in info.function_tools))
        if any(isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", [])):
            return ModelResponse(parts=[TextPart("Open Tuesday to Saturday, 10 to 5.")])
        return ModelResponse(parts=[ToolCallPart("browse_open", {"url": PAGE})])

    def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
        from pydantic_ai import Agent

        given["note"] = note
        agent = Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")
        toolset.register_tools(agent, allow=allow)
        return agent

    class Page:
        box = type("B", (), {"session": "s"})()
        steps = 1
        site = ""

        async def ask(self, cmd, **args):
            return {"ok": True, "url": PAGE, "title": "Hours", "text": "", "elements": []}

    opened, closed = [], []

    async def session_for(key, site=None, *, domains=()):
        opened.append((key, tuple(domains)))
        return Page()

    async def close(key):
        closed.append(key)
        return True

    monkeypatch.setattr(loop, "build_agent", build_agent)
    monkeypatch.setattr(sessions, "session_for", session_for)
    monkeypatch.setattr(sessions, "current", lambda key: None)
    monkeypatch.setattr(sessions, "close", close)
    monkeypatch.setattr("core.egress.refusal", lambda url: None)
    crew = workers.Crew(conn, _session(conn, "job"), seen={PAGE}, browsing=True)

    started = await crew.start_browsing("Find Saturday's hours.", [PAGE], name="museum")
    said = await crew.wait(30)

    [worker] = said["workers"]
    assert started["ok"] and worker["status"] == "done" and "10 to 5" in worker["result"]
    assert "browse_open" in given["tools"] and "read_email" not in given["tools"]
    assert "kept to 93.184.216.34" in given["note"]
    key = crew.workers[0].browser_key
    assert key < 0 and opened == [(key, ("93.184.216.34",))] and closed == [key]
    assert workers._browsing == set(), "its place is given back"


# --------------------------------------------------------------------------
# An unattended turn's crew (`workers.send_browsing`): the doer's, the night's
# --------------------------------------------------------------------------


class _Turn:
    """A `Conversation` for one unattended turn: what it was sent with. Its
    crew is charged `spent`, as a worker that ran would charge it."""

    def __init__(self, conn, *, spent: float = 0.0, fails: bool = False):
        self.conn, self.channel = conn, "initiative"
        self.session_id = _session(conn, "initiative")
        self.note, self.mandate = None, "The attention loop's own task, not the user's words: x"
        self.spent, self.fails, self.sent = spent, fails, {}

    async def send(self, text, **kw):
        self.sent = kw
        if crew := kw.get("crew"):
            crew.spent += self.spent
        if self.fails:
            raise RuntimeError("the model is down")
        return type("R", (), {"text": "done", "cost_usd": 0.01})()


async def test_an_unattended_turn_gets_a_crew_that_may_only_browse_and_pays_for_it(conn):
    """The night's budget is its own running total of what its turns cost:
    a worker's spend left out of the reply would never reach it."""
    turn = _Turn(conn, spent=0.04)

    reply = await workers.send_browsing(turn, "Find the hours.", frozenset({"web_search"}))

    crew = turn.sent["crew"]
    assert turn.sent["allow"] == {"web_search"} | workers.LEAD_BROWSING_TOOLS
    assert crew.browsing and not crew.split and crew.closed
    assert crew.seen is turn.sent["seen"] and crew.seen == set()
    assert (crew.session_id, crew.channel, crew.mandate) == (turn.session_id, "initiative",
                                                             turn.mandate)
    assert turn.note == workers.LEAD_BROWSE_NOTE
    assert reply.cost_usd == pytest.approx(0.05)


async def test_with_browsing_workers_off_an_unattended_turn_is_sent_as_it_was(conn,
                                                                              monkeypatch):
    monkeypatch.setattr(core.browser, "background", lambda: False)
    turn = _Turn(conn)

    reply = await workers.send_browsing(turn, "Find the hours.", frozenset({"web_search"}))

    assert turn.sent == {"allow": {"web_search"}} and turn.note is None
    assert reply.cost_usd == 0.01


async def test_a_turn_that_fails_still_stops_its_workers(conn):
    turn = _Turn(conn, fails=True)

    with pytest.raises(RuntimeError):
        await workers.send_browsing(turn, "Find the hours.", frozenset({"web_search"}))

    assert turn.sent["crew"].closed
