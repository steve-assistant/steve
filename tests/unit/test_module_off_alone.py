"""Any one module switched off, the rest on: Steve starts, answers, and
says what is not connected.

`test_off_means_original.py` switches every module off at once and finds
Steve as it was before any. A user switches one off -- a mailbox they no
longer use, a calendar link that broke -- and the others carry on. Each
built-in is switched off alone here, with an account of every module on a
fake: what Steve is given still builds, a turn still answers from the
accounts that are on, the module's accounts are gone from what the model is
told, a class only it provided has no section (the house prompt says what
has none is not connected), and an id of its account is said to be from an
account not connected now.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import connectors, features, loop
from agent.connectors import NoAccount
from agent.models import Role
from agent.turn import Deps
from core import db, modules
from steve_sdk import ids
from steve_sdk.classes import Event, FileItem, Message, Task
from tests.gate import mailfake, slotfake

BUILT_INS = modules.read_builtins()
# The features a class's accounts bring (agent/features.py).
SECTION = {"mail": "mail", "calendar": "calendar", "files": "drive", "tasks": "tasks"}


def _fake(klass: str, n: int, address: str):
    soon = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
    return {
        "mail": lambda: mailfake.Mailbox(address, Message(
            f"m{n}", sender="Dana <dana@example.com>", to=address, subject="Lunch?",
            when=int(dt.datetime.now().timestamp() * 1000), body="Lunch on Friday?")),
        "calendar": lambda: slotfake.Calendar(Event(
            f"e{n}", title=f"Review {n}", start=soon, end=soon + dt.timedelta(hours=1))),
        "files": lambda: slotfake.Drive(FileItem(f"f{n}", name="Q3 plan", modified="t1")),
        "tasks": lambda: slotfake.TaskBoard(Task(f"k{n}", title="Renew the permit")),
    }[klass]()


@pytest.fixture
def every(tmp_path, monkeypatch):
    """An account of each built-in connector, every class it offers, each
    class on a fake of its own: the registry still decides which are live.
    A built-in with no class (a tool's) has no account to add."""
    conn = db.open_store(tmp_path / "agent.db")
    modules.register(conn)
    fakes: dict[tuple[str, str], object] = {}
    keys: dict[str, str] = {}
    for n, (module_id, manifest) in enumerate(sorted(BUILT_INS.items())):
        if not manifest.classes:
            continue
        address = f"maya{n}@example.com"
        preset = manifest.setup.presets[0].id if manifest.setup.presets else None
        keys[module_id] = modules.add_account(
            conn, module_id=module_id, name=f"acct-{module_id}",
            classes=list(manifest.classes), addresses=[address], preset=preset)
        for klass in manifest.classes:
            fakes[(klass, keys[module_id])] = _fake(klass, n, address)
    for engine in list(connectors.ENGINES):
        monkeypatch.setitem(connectors.ENGINES, engine, lambda ctx, spec: fakes[
            (spec.klass, ctx.account.key)])
    yield conn, keys
    conn.close()


def _answers(on) -> FunctionModel:
    """A model that looks at the calendar if it is offered, then says what
    it found, or that there is no calendar."""
    async def respond(messages, info: AgentInfo) -> ModelResponse:
        returned = [p for m in messages for p in getattr(m, "parts", [])
                    if isinstance(p, ToolReturnPart)]
        offered = {t.name for t in info.function_tools}
        if not returned and "calendar_agenda" in offered:
            return ModelResponse(parts=[ToolCallPart("calendar_agenda", {"days": 1})])
        said = str(returned[0].content) if returned else "no calendar is connected"
        return ModelResponse(parts=[TextPart(f"Answered: {said[:200]}")])

    return FunctionModel(respond)


@pytest.mark.parametrize("module_id", sorted(BUILT_INS))
async def test_with_one_module_off_steve_starts_answers_and_says_what_is_not_connected(
        every, module_id):
    conn, keys = every
    off = BUILT_INS[module_id]
    others = [m for m in BUILT_INS.values() if m.id != module_id]
    modules.set_enabled(conn, module_id, False)

    # Starts: what a conversation is given builds, with the module off.
    on = features.enabled(conn)
    prompt = loop.system_prompt(on)
    agent = loop.build_agent(Role.CONVERSE, on=on, lead=True)

    # Answers, from the accounts still on.
    with agent.override(model=_answers(on)):
        result = await agent.run("What's on today?", deps=Deps(conn=conn, session_id=1))
    assert result.output.startswith("Answered:")

    # Says what is not connected: its accounts are not offered or named, a
    # class only it brought has no section, and its ids are said to be from
    # an account not connected now.
    for klass in off.classes:
        assert keys[module_id] not in {c.key for c in connectors.accounts(conn, klass)}
        if any(klass in m.classes for m in others):
            assert SECTION[klass] in on, f"{klass} is still connected through another module"
        else:
            assert SECTION[klass] not in on, f"{klass} came only from {module_id}"
        with pytest.raises(NoAccount, match="not connected now"):
            connectors.by_id(conn, ids.make(klass, keys[module_id], "x1"))
    assert f"acct-{module_id}" not in prompt
    assert all(f"acct-{m.id}" in prompt for m in others if set(m.classes) & {"mail", "calendar"})
    if "calendar" in on:
        assert "Review" in result.output, result.output
