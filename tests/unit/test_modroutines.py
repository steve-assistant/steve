"""A module's routines run at their times, once, as the user's do; ask
before every action; and stop the turn the module is off (agent/
modroutines.py; 06-modules.md, section 1).

A routine is a module's words run with no one watching. One that ran at
once when its module was turned on, or twice for two looks, would spend
and speak when nobody said it might; one whose action went through
without a card would let a module's instruction act for the user; and
one that outlived its module's switch would make the switch a lie.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from agent import modroutines, modtools, runtime
from agent.turn import Deps
from agent.worker import background, handlers, links
from core import db, modules, monitor, paths, screen
from steve_sdk.manifest import parse

# A Saturday morning, pinned: weekdays must not depend on when the suite runs.
SAT_10 = datetime(2026, 10, 3, 10, 0)


def _harbour(at="10:30", version="1"):
    return parse({
        "manifest_version": 1, "sdk": 1, "id": "harbour", "version": version,
        "label": "Harbour", "for": "The harbour's tides", "tier": "sandbox",
        "code": "harbour.py", "hosts": ["tides.harbour.example"],
        "tools": [{"name": "tides", "class": "read", "description": "Today's tides",
                   "params": []}],
        "routines": [
            {"title": "Tides", "instruction": "Say today's high tides.", "at": at},
            {"title": "Weekend sails", "instruction": "Say if Saturday suits a sail.",
             "at": "18:00", "days": ["fri"]}]})


DOCK = parse({
    "manifest_version": 1, "sdk": 1, "id": "dock", "version": "1", "label": "Dock",
    "for": "The dock's slips", "tier": "sandbox", "code": "dock.py",
    "tools": [{"name": "slips", "class": "read", "description": "Free slips", "params": []}]})


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    monkeypatch.setattr(modules, "builtins", lambda: {"harbour": _harbour(), "dock": DOCK})
    modules.register(c)
    yield c
    c.close()


@pytest.fixture
def told(monkeypatch):
    out: list[str] = []

    async def notify(text, **_):
        out.append(text)
        return len(out)

    monkeypatch.setattr(links, "notify_user", notify)
    return out


class _Reply:
    def __init__(self, text):
        self.text, self.cost_usd = text, 0.001


@pytest.fixture
def run(monkeypatch):
    """`Conversation` replaced: what a run was built with, and what it says."""
    seen: dict = {}

    class Convo:
        def __init__(self, conn, **kw):
            seen["init"] = kw

        async def send(self, text, **kw):
            seen["send"] = {"text": text, **kw}
            return _Reply(seen["reply"])

        def end(self):
            pass

    monkeypatch.setattr(handlers, "Conversation", Convo)
    return seen


class _Job:
    def __init__(self, prompt):
        self.prompt = prompt


def test_a_routine_first_seen_waits_for_its_time_then_is_queued_once_however_many_look(
        conn):
    """Turned on at ten with a routine at half past, it runs at half past,
    not at once; and two looks after that queue it once."""
    assert asyncio.run(background.routine_tick(SAT_10)) == []
    kept = paths.module_runs_file("harbour")
    assert kept.is_file() and paths.module_state_dir("harbour") not in kept.parents, \
        "core's record of its runs is where the module cannot write"

    later = SAT_10 + timedelta(minutes=31)
    first = asyncio.run(background.routine_tick(later))
    again = asyncio.run(background.routine_tick(later))

    assert len(first) == 1 and again == []
    row = conn.execute("SELECT kind, prompt FROM jobs WHERE id = ?", (first[0],)).fetchone()
    assert (row["kind"], row["prompt"]) == ("routine", "harbour.1")


def test_a_new_version_that_moves_a_routine_takes_its_new_time_without_running_at_once(
        conn, monkeypatch):
    modroutines.due(conn, SAT_10)
    monkeypatch.setattr(modules, "builtins", lambda: {"harbour": _harbour(at="11:00")})
    assert modroutines.due(conn, SAT_10 + timedelta(minutes=45)) == []
    assert [r.id for r in modroutines.due(conn, SAT_10 + timedelta(minutes=61))] == ["harbour.1"]


def test_a_modules_routines_stop_the_turn_it_is_off(conn, told, run):
    modroutines.due(conn, SAT_10)
    modules.set_enabled(conn, "harbour", False)

    assert modroutines.due(conn, SAT_10 + timedelta(hours=1)) == []
    out = json.loads(asyncio.run(handlers.handle_routine(_Job("harbour.1"))))
    assert out["ran"] is False and "init" not in run and told == []
    assert modroutines.next_run(conn) is None


def test_a_run_names_its_module_offers_its_own_tools_and_says_only_what_changed(
        conn, told, run):
    from agent.workers import WORKER_TOOLS

    run["reply"] = "High water at 12:40."
    out = json.loads(asyncio.run(handlers.handle_routine(_Job("harbour.1"))))

    assert run["init"]["channel"] == "routine" and run["init"]["module_routine"] == "harbour.1"
    assert run["init"].get("routine_id") is None, "no envelope of a user's routine"
    assert "the module Harbour set up" in run["init"]["note"]
    assert run["send"]["allow"] == set(WORKER_TOOLS) | {"mod__harbour__tides"}, \
        "its own module's tools, not another's (mod__dock__slips)"
    assert run["send"]["text"] == "Say today's high tides."
    assert out["reported"] and told == ["Harbour, Tides: High water at 12:40."]

    asyncio.run(handlers.handle_routine(_Job("harbour.1")))
    assert len(told) == 1, "the same again says nothing"
    assert "What the user was told last time:\nHigh water at 12:40." in run["init"]["note"]


DRAFT = {"to": ["dana@acme.example"], "subject": "Sail", "body": "Saturday?"}


def test_each_action_of_a_modules_routine_is_a_card_even_one_a_worker_does_alone(conn):
    """A user's routine with no envelope may still draft or note on its
    own, as a worker may; a module's routine is a module's words, so even
    that asks."""
    assert monitor.gate(conn, "draft_email", DRAFT, worker=True).kind == "free"
    assert monitor.module_gate(conn, "draft_email", DRAFT).kind == "card"


async def test_a_run_of_a_modules_routine_asks_the_module_gate(conn):
    from agent import actions
    from agent.turn import Deps

    class Ctx:
        deps = Deps(conn=conn, session_id=1, channel="routine", module_routine="harbour.1")

    assert (await actions._gate(Ctx(), "draft_email", DRAFT)).kind == "card"


def test_the_wake_alarm_knows_a_modules_next_run(conn):
    from agent import wake

    modroutines.due(conn, SAT_10)
    assert wake.next_due(conn, now=SAT_10) == (datetime(2026, 10, 3, 10, 30), "module routine")


class _Run:
    """A tool call's context: its deps, all `modtools.use` reads of it."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps


@pytest.fixture
def tool_calls(conn, monkeypatch):
    """Harbour's tides tool, its sandbox run and its card stood in: what
    reached the module's code, and what was asked."""
    ran, asked = [], []
    monkeypatch.setattr(modtools.run, "call", lambda *a, **k: ran.append(a) or {"high": "12:40"})

    async def shown(conn, source, source_id, origin, text):
        return screen.Shown(text, "text")

    async def act(ctx, tool, args, preview):
        asked.append(tool)
        return {"status": "awaiting_approval"}

    monkeypatch.setattr(modtools.screening, "shown", shown)
    monkeypatch.setattr(modtools.actions, "act", act)
    offer = next(o for o in modtools.offers(conn) if o.name == "mod__harbour__tides")
    return offer, ran, asked


@pytest.mark.parametrize("away", [
    {"session_id": 1, "channel": "routine", "module_routine": "harbour.1"},
    {"session_id": 1, "goal_id": 7},
    {"session_id": 1, "worker": True},
    {"session_id": None},
], ids=["its routine", "a goal", "a worker", "no conversation"])
async def test_a_modules_free_tool_is_a_card_on_every_call_when_no_one_is_in_the_run(
        conn, tool_calls, away):
    """Harbour reads none of the user's accounts and only GETs its host, so
    its card asks no passphrase and its tool is free. But its routine is
    offered read_email beside it: with no one watching, it could read the
    user's mail and pass each subject to its host as an argument, after a
    card that said anything a run would do, it asks first (the review of
    steps 5 and 6, 15 October)."""
    offer, ran, asked = tool_calls
    await modtools.use(_Run(Deps(conn=conn, **away)), offer, {})
    assert ran == [] and asked == [modtools.TOOL]


async def test_a_modules_free_tool_runs_without_a_card_in_the_users_own_conversation(
        conn, tool_calls):
    """The user is there, and sees what each call is given."""
    offer, ran, asked = tool_calls
    said = await modtools.use(_Run(Deps(conn=conn, session_id=1)), offer, {})
    assert len(ran) == 1 and asked == [], said


@pytest.mark.parametrize("gone", [
    lambda conn: modules.set_enabled(conn, "harbour", False),
    lambda conn: modules.remove_module(conn, "harbour"),
], ids=["turned off", "removed"])
async def test_a_card_a_modules_routine_raised_is_withdrawn_when_the_module_goes(conn, gone):
    """A card for one of core's own tools names no module's account, so
    turning Harbour off, or removing it, left the task its routine asked for
    waiting, and a yes added it (the review of steps 5 and 6, 15 October).
    Off means original: what a module's words asked for goes with it, as
    its accounts' cards do. The user's own card is left alone."""
    from agent import actions, approvals
    from core import consent

    class Routine:
        deps = Deps(conn=conn, session_id=None, channel="routine", module_routine="harbour.1")

    said = await actions.act(Routine(), "add_task", {"task": "Check the moorings",
                                                     "due_date": None},
                             "Add a task: Check the moorings")
    assert said.get("status") == "awaiting_approval", said
    mine = await approvals.request(
        conn, tool="add_task", args={"task": "Buy rope", "due_date": None},
        preview="Add a task: Buy rope",
        consent=consent.classify("add_task", {"task": "Buy rope", "due_date": None}))
    gone(conn)
    assert [a.id for a in approvals.pending(conn)] == [mine["action_id"]]
    assert approvals.get(conn, said["action_id"]).status == "withdrawn"


async def test_a_modules_routine_still_running_when_its_module_goes_off_asks_for_nothing(conn):
    """A run already under way when the switch goes off would otherwise ask
    for its next action after the withdrawal, and that card would wait."""
    from agent import actions

    class Routine:
        deps = Deps(conn=conn, session_id=None, channel="routine", module_routine="harbour.1")

    modules.set_enabled(conn, "harbour", False)
    said = await actions.act(Routine(), "add_task", {"task": "Check the moorings",
                                                     "due_date": None},
                             "Add a task: Check the moorings")
    assert said.get("kind") == "refused", said
    assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
