"""A turn nobody is waiting on starts with the model thinking first.

Every turn used to start at CONVERSE, no reasoning: right for a reply the user is
watching arrive, wrong for the doer, a routine, a goal or the night, which
have minutes and act unasked.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import loop, toolset
from agent.loop import UNATTENDED, Conversation
from agent.models import Role
from agent.turn import Deps

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def conn(memories_repo, store):
    return store


@pytest.fixture
def roles(monkeypatch):
    built: list[Role] = []

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("done")])

    def build_agent(role, **kwargs):
        from pydantic_ai import Agent

        built.append(role)
        agent = Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")
        toolset.register_tools(agent, allow=kwargs.get("allow"))
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)
    return built


async def test_a_conversation_the_user_is_watching_starts_without_reasoning(conn, roles):
    await Conversation(conn, channel="signal").send("what's on today?")
    assert roles == [Role.CONVERSE]


async def test_a_background_turn_starts_on_the_reasoning_tier(conn, roles):
    convo = Conversation(conn, channel="routine")
    convo.start = UNATTENDED

    await convo.send("run the routine")

    assert roles == [Role.REASON] and UNATTENDED is Role.REASON


@pytest.mark.parametrize("path", [
    "agent/attention/prepare.py", "agent/night/reading.py", "agent/worker/handlers.py",
])
def test_every_background_conversation_is_started_unattended(path):
    """Each `Conversation(...)` in the modules that run work nobody waits on
    is given `.start = UNATTENDED` -- the next one added included."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    made = sum(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Conversation"
               for n in ast.walk(tree))
    started = sum(isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Attribute) and t.attr == "start" for t in n.targets)
                  and getattr(n.value, "id", None) == "UNATTENDED"
                  for n in ast.walk(tree))
    assert made and made == started
