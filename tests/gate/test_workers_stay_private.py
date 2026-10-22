"""A background task's workers can do only what is private and reversible,
and cannot start workers.

The maintainer, 30 September 2026, asking for them: "allow the top-level agent
to spawn and despawn agents as needed for tasks but not any workers
spawning workers", and "Workers shouldn't be able to do any
irreversible/public impact stuff but drafting an email or putting stuff on
the calendar is fine."

In the gate for the reason the rest of it is: the night's self-upgrades
can merge anything outside the frozen paths, and `agent/workers.py` is
outside them. A change that widened what a worker may do, or let one start
another, has to fail here -- where it cannot edit the test -- rather than
be noticed after it ran. See agent/workers.py for the design.
"""

from __future__ import annotations

import asyncio

import pytest

import agent.toolset  # noqa: F401 - registers every executor before the fakes replace some
from agent import actions, approvals, modtools, workers
from agent.toolset import register_tools
from agent.toolset import workers as worker_tools
from agent.turn import Deps
from core.consent import load_rules

# What no worker may ever be handed, whatever else changes: each sends,
# deletes, spends, changes the agent, or starts more work.
NEVER = {
    "send_email", "send_draft", "delete_event", "delete_draft", "trash_file",
    "purchase", "build_module",
    "defer_to_background", "do_when_laptop_wakes", "keep_checking",
    "start_worker", "wait_for_workers", "stop_worker", "escalate",
    "update_spreadsheet",
}


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


class _Agent:
    """Collects what `register_tools` attaches."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def tool(self, fn):
        self.names.add(fn.__name__)
        return fn

    tool_plain = tool

    def toolset(self, **kwargs):
        # Module tools arrive at each run's start, filtered by the same
        # `allow` (tests/unit/test_modtools.py): none to collect here.
        return lambda fn: fn


def test_every_worker_tool_is_free_and_none_is_one_that_acts_outward():
    rules = load_rules()

    assert not workers.WORKER_TOOLS & NEVER
    free = set(rules.free) | modtools.free_builtins()
    assert set(workers.WORKER_TOOLS) <= free, "a gated tool in a worker's hands"


def test_a_workers_toolset_is_its_list_and_has_nothing_that_starts_a_worker():
    agent = _Agent()

    register_tools(agent, allow=set(workers.WORKER_TOOLS))

    assert agent.names <= workers.WORKER_TOOLS
    assert not agent.names & NEVER


def test_only_a_lead_is_given_the_tools_that_start_workers():
    """A conversation the user is waiting on has the whole toolset but these: a
    worker is a background task's, and only the job's lead manages them."""
    everything, lead = _Agent(), _Agent()

    register_tools(everything)
    register_tools(lead, lead=True)

    assert "start_worker" not in everything.names and "send_email" in everything.names
    assert {"start_worker", "wait_for_workers", "stop_worker"} <= lead.names


async def test_a_worker_runs_as_a_worker_with_its_list_and_no_crew(store, monkeypatch):
    """What `Crew._run` builds, observed rather than read: the agent it asks
    for and the `Deps` its tools will see."""
    import agent.llm
    import agent.loop

    built: dict = {}

    def build_agent(role, **kwargs):
        built.update(kwargs)
        return object()

    async def run_agent(agent_, prompt, role, *, deps, **kwargs):
        built["deps"] = deps
        raise RuntimeError("no model in the gate")

    monkeypatch.setattr(agent.loop, "build_agent", build_agent)
    monkeypatch.setattr(agent.llm, "run_agent", run_agent)
    crew = workers.Crew(store, session_id=1, job_id=1)

    crew.start("look up one person")
    await crew.wait(5)

    assert built["allow"] == set(workers.WORKER_TOOLS)
    assert not built.get("lead"), "a worker was built as a lead"
    assert built["deps"].worker is True and built["deps"].crew is None


async def test_a_worker_cannot_start_workers_even_holding_the_tool(store):
    """Belt and braces: were the tool ever registered for a worker, it refuses."""
    tools: dict = {}
    worker_tools.register(lambda fn: tools.setdefault(fn.__name__, fn))

    class _Crew:
        started = False

        def start(self, *a, **k):
            _Crew.started = True
            return {"ok": True}

    deps = Deps(conn=store, session_id=1, channel="job", worker=True, crew=_Crew())

    said = await tools["start_worker"](_Ctx(deps), "a sub-task")

    assert said["ok"] is False and not _Crew.started


@pytest.fixture
def effects(monkeypatch):
    """Executors that only count, and an approval queue that only counts."""
    ran: list[str] = []
    asked: list[str] = []

    async def perform(tool, args):
        ran.append(tool)
        return {"ok": True}

    async def request(conn, *, tool, **kwargs):
        asked.append(tool)
        return {"ok": False, "status": "asked"}

    monkeypatch.setattr(actions, "perform", perform)
    monkeypatch.setattr(approvals, "record_free", lambda *a, **k: None)
    monkeypatch.setattr(approvals, "request", request)
    return ran, asked


@pytest.mark.parametrize("tool, args", [
    ("create_event", {"summary": "Coffee", "start": "2026-10-01T10:00",
                      "attendees": ["hugo@example.com"]}),
    ("send_email", {"to": "hugo@example.com", "subject": "Thanks", "body": "Hi"}),
    ("send_draft", {"draft_id": "r123"}),
    ("delete_event", {"event_id": "e1"}),
    ("purchase", {"item": "a book", "amount_usd": 20}),
    # Free and private -- no range, so not gated -- but written cells have
    # no undo: refused for being irreversible alone.
    ("update_spreadsheet", {"spreadsheet_id": "s1", "values": [[1]]}),
])
async def test_a_worker_is_refused_anything_public_or_irreversible_and_asks_them_nothing(
        store, effects, tool, args):
    """Refused, not carded: a worker asking the user to approve a send would be
    the public impact by another door."""
    ran, asked = effects
    ctx = _Ctx(Deps(conn=store, session_id=1, channel="job", worker=True))

    said = await actions.act(ctx, tool, args, "preview")

    assert said["ok"] is False and said["kind"] == "not_for_workers"
    assert ran == [] and asked == []


@pytest.mark.parametrize("tool, args", [
    ("draft_email", {"to": "hugo@example.com", "subject": "Thanks", "body": "Hi"}),
    ("create_event", {"summary": "Run", "start": "2026-10-01T07:15"}),
])
async def test_a_worker_may_draft_and_put_things_on_their_calendar(store, effects, tool, args):
    """What the user said was fine: a draft sends nothing, and an event with no
    guests reaches nobody."""
    ran, asked = effects
    ctx = _Ctx(Deps(conn=store, session_id=1, channel="job", worker=True))

    said = await actions.act(ctx, tool, args, "preview")

    assert said == {"ok": True} and ran == [tool] and asked == []


async def test_the_lead_itself_still_asks_them_as_ever(store, effects):
    """The rule is the workers'. The lead's gated calls are cards, as any
    conversation's are."""
    ran, asked = effects
    ctx = _Ctx(Deps(conn=store, session_id=1, channel="job"))

    await actions.act(ctx, "send_email", {"to": "hugo@example.com", "subject": "s",
                                          "body": "b"}, "preview")

    assert asked == ["send_email"] and ran == []


def test_a_crew_is_bounded():
    assert workers.MAX_LIVE <= 4 and workers.MAX_WORKERS <= 8
    assert workers.CREW_BUDGET_USD <= 0.50 and workers.WORKER_TIMEOUT_S <= 600
    assert asyncio.iscoroutinefunction(workers.Crew.close)
