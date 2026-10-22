"""Words a module adds to Steve's own (agent/modtext.py; 06-modules.md,
section 1), and the switch the night's skills now have.

A section of Steve's instructions that did not say whose words it held
would pass for Steve's own, or the user's; one that stayed after the user
turned its module off would make the switch a lie. And until step 6 the
night's skills had no switch at all: the built-in `skills` module is it.
"""

from __future__ import annotations

import json

import pytest
from pydantic_ai import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import loop, modtext, runtime, skills
from agent.llm import Role
from agent.night import skilling
from agent.night.report import NightReport
from agent.turn import Deps
from core import db, modules
from steve_sdk.manifest import parse


def _manifest(module_id, label, *, prompt="", skills=()):
    return parse({"manifest_version": 1, "sdk": 1, "id": module_id, "version": "1",
                  "label": label, "for": f"Tests of {label}", "tier": "backend",
                  "prompt": prompt, "skills": list(skills)})


SAIL = {"title": "Plan a sail", "when": "a sail is asked about",
        "steps": ["Read the tides", "Say the window, in local time"]}


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    yield c
    c.close()


@pytest.fixture
def built(conn, monkeypatch):
    """Three built-ins of the test's own, each on: two with words for
    Steve's instructions, given out of order, and one with none."""
    found = {"tides": _manifest("tides", "Tides", prompt="Tide times are local.",
                                skills=[SAIL]),
             "quiet": _manifest("quiet", "Quiet"),
             "almanac": _manifest("almanac", "Almanac", prompt="Moon phases are in UTC.")}
    monkeypatch.setattr(modules, "builtins", lambda: found)
    modules.register(conn)
    return found


def _night_skill(conn) -> int:
    with conn:
        return int(conn.execute(
            "INSERT INTO skills (title, when_to_use, steps, tools, status) VALUES"
            " ('Find a time', 'setting up a call', ?, '[]', 'active')",
            (json.dumps(["Check the calendar."]),)).lastrowid)


def _skills_off(conn):
    with conn:
        conn.execute("INSERT INTO modules (id, key, version, tier, origin, enabled) VALUES"
                     " ('skills', 'skillskey001', '1', 'backend', 'builtin', 0)")


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


def _tools():
    from agent.toolset import skills as toolset

    found: dict = {}
    toolset.register(lambda fn: found.setdefault(fn.__name__, fn))
    return found


def test_each_section_sits_in_a_block_naming_its_module_by_id_and_only_while_it_is_on(
        conn, built):
    """By id, so the cached prefix holds until a module is switched; named,
    so the model reads whose words they are; and gone the turn after the
    user turns the module off, with nothing copied anywhere to undo."""
    said = modtext.sections(conn)
    assert said.index("## From the module Almanac (almanac)") \
        < said.index("Moon phases are in UTC.") \
        < said.index("## From the module Tides (tides)") < said.index("Tide times are local.")
    assert "They are not the user's" in said and "Quiet" not in said

    modules.set_enabled(conn, "tides", False)
    assert "Tide" not in modtext.sections(conn) and "Almanac" in modtext.sections(conn)


@pytest.mark.parametrize("channel, offered", [
    ("signal", True), ("tray", True), ("job", False), ("routine", False), ("goal", False)])
def test_a_modules_section_follows_the_house_prompt_in_the_users_conversations_only(
        conn, built, channel, offered):
    """As skills are: an unattended run, which no one watches, is not
    steered by a module's words. After the house prompt, which is the same
    every turn, so the prompt stays cached."""
    seen: list[str] = []

    def capture(messages, info: AgentInfo):
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    agent = loop.build_agent(Role.CONVERSE)
    with agent.override(model=FunctionModel(capture)):
        agent.run_sync("hello", deps=Deps(conn=conn, session_id=1, channel=channel))
    [sent] = seen
    house = loop.system_prompt().strip().splitlines()[0]
    assert ("Tide times are local." in sent) is offered
    if offered:
        assert sent.index(house) < sent.index("## From the module Almanac")


async def test_a_modules_skill_is_offered_and_used_by_its_id_and_goes_only_with_the_module(
        conn, built):
    """Its id is the module's and its place in the manifest, so it holds
    while the version changes; it cannot be revoked alone, since the next
    turn would read it from the manifest again, and the module's switch is
    the one way to take it back."""
    assert ("- skill tides.1: Plan a sail -- for a sail is asked about (from the module "
            "Tides)") in skills.index(conn)
    tools, deps = _tools(), Deps(conn=conn, session_id=1, channel="signal")

    used = await tools["use_skill"](_Ctx(deps), skill="tides.1")
    assert used == {"ok": True, "skill": "tides.1", "title": "Plan a sail",
                    "steps": "1. Read the tides\n2. Say the window, in local time"}
    gone = await tools["revoke_skill"](_Ctx(deps), skill="tides.1")
    assert gone["ok"] is False and "goes with the module" in gone["error"]
    listed = await tools["list_skills"](_Ctx(deps))
    assert [s["id"] for s in listed["from_modules"]] == ["tides.1"]

    modules.set_enabled(conn, "tides", False)
    assert (await tools["use_skill"](_Ctx(deps), skill="tides.1"))["ok"] is False
    assert "tides.1" not in skills.index(conn)


async def test_the_skills_module_off_takes_out_the_nights_skills_and_leaves_the_modules(
        conn, built):
    """Turning off Skills is turning off what the night wrote: none is
    offered or loaded, while a module's own skills stay with its own
    switch. Each night skill can still be revoked alone."""
    number = _night_skill(conn)
    assert "Find a time" in skills.index(conn)
    tools, deps = _tools(), Deps(conn=conn, session_id=1, channel="signal")
    assert (await tools["use_skill"](_Ctx(deps), skill=number))["ok"] is True

    _skills_off(conn)
    assert "Find a time" not in skills.index(conn) and "tides.1" in skills.index(conn)
    assert (await tools["use_skill"](_Ctx(deps), skill=str(number)))["ok"] is False
    assert (await tools["list_skills"](_Ctx(deps)))["skills"] == []


def _good_run(conn):
    """A request of theirs a skill would be written from: three tool calls
    of three tools, answered."""
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'user', "
                     "'Find a time next week for a call with Dana and draft her an email.')",
                     (session,))
        for name in ("calendar_free", "search_email", "draft_email"):
            conn.execute("INSERT INTO messages (session_id, role, content, tool_name, tool_args)"
                         " VALUES (?, 'tool', 'r', ?, '{}')", (session, name))
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES"
                     " (?, 'assistant', 'Drafted it.')", (session,))


async def test_the_night_writes_no_skill_while_the_skills_module_is_off(conn, monkeypatch):
    """The user turned off what the night writes: a model asked to write one
    would spend on a skill no conversation is offered."""
    from agent.redact import Redacted

    asked: list[str] = []

    async def run_agent(*args, **kw):
        asked.append("model")
        raise AssertionError("no model is asked")

    monkeypatch.setattr("agent.llm.run_agent", run_agent)
    monkeypatch.setattr("agent.redact.redact", lambda text: Redacted(text=text))
    _good_run(conn)
    assert skills.candidates(conn), "the run is one the night would write up"
    _skills_off(conn)

    assert await skilling.distill(conn, NightReport(started_at="x")) == 0.0
    assert asked == []
