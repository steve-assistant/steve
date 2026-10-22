"""A fresh install, with nothing set up, answers rather than breaking.

The audit of steps 1 and 2 (12 October 2026, docs/plan/reports/02-audit.md,
area C) found a fresh install's turns broken three ways: memory had no
repository of its own and no embedder, a tool missing its key or add-on
raised through the turn, and with no model key the first message went to
OpenRouter with a stand-in key. Each now says what is missing.
"""

from __future__ import annotations

import subprocess

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import loop, toolset
from agent.loop import Conversation
from agent.turn import Deps
from core import db, identity, paths, secrets
from tools import memory


def _git(root, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True).stdout.strip()


def test_the_first_memory_makes_a_repository_of_its_own_and_commits_with_no_git_identity(
        store, tmp_path, monkeypatch):
    """Nothing made the memories repository but a test fixture. On a fresh
    install git looked upward from the plain folder -- in a source tree, to
    the checkout itself -- and the first `remember` broke the turn. A
    user's git may have no identity configured, either."""
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
    root = checkout / "data" / "memories"
    monkeypatch.setenv("STEVE_MEMORIES_DIR", str(root))
    nobody = tmp_path / "gitconfig"
    nobody.write_text("", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(nobody))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(memory, "embed",
                        lambda texts: [[0.1] * db.EMBEDDING_DIM for _ in texts])

    memory.remember(store, kind="user", summary="Prefers mornings",
                    body="Meetings before noon, where there is a choice.")

    assert (root / ".git").is_dir()
    name, email = identity.git_identity("memory")
    assert _git(root, "log", "--format=%an <%ae>") == f"{name} <{email}>"
    assert _git(checkout, "rev-list", "--all") == "", "nothing went into the checkout"


def _no_openai_key(monkeypatch) -> None:
    real = secrets.get
    monkeypatch.setattr("core.secrets.get",
                        lambda key, default=None: None if key == "OPENAI_API_KEY"
                        else real(key, default))


def test_an_embedder_with_no_key_is_said_to_be_missing_not_a_fault(monkeypatch):
    """The embedder needed the voice add-on's onnxruntime, so on a fresh
    install it could not load. It is OpenAI's now, and what it can lack is
    the key: a setting the model can tell the user about."""
    _no_openai_key(monkeypatch)

    with pytest.raises(memory.EmbedderUnavailable) as raised:
        memory.embed(["anything"])
    assert toolset.not_set_up(raised.value) == (
        "memory search is unavailable: no OpenAI key is stored")


def _turn_calling(monkeypatch, tool: str, args: dict, allow: set[str]) -> list:
    """Replace the agent with one that calls `tool` once and then says what
    came back; returns what came back."""
    came_back: list = []

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        returned = [p for p in messages[-1].parts if isinstance(p, ToolReturnPart)]
        if returned:
            came_back.append(returned[0].content)
            return ModelResponse(parts=[TextPart(f"It said: {returned[0].content}")])
        return ModelResponse(parts=[ToolCallPart(tool, args)])

    def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
        agent = Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")
        toolset.register_tools(agent, allow=allow_these)
        return agent

    allow_these = allow
    monkeypatch.setattr(loop, "build_agent", build_agent)
    return came_back


async def test_a_tool_missing_what_it_needs_answers_and_the_turn_goes_on(store, monkeypatch):
    """A tool that raised for a missing key, account or add-on broke the
    whole turn: "Something broke on my side", and nothing the model could
    tell the user."""
    import httpx
    import openai

    # The embedder unreachable: the turn has the key it shares, so this is
    # what a note can lack that the turn does not.
    def unreachable(request, key):
        raise openai.APIConnectionError(
            request=httpx.Request("POST", "https://api.openai.com/v1/embeddings"))

    monkeypatch.setattr("core.embed._call", unreachable)
    # A note, not a fact: a fact is kept without the embedder, found by name.
    came_back = _turn_calling(monkeypatch, "remember",
                              {"kind": "project", "summary": "Kitchen renovation",
                               "body": "Quotes are due on Friday."},
                              {"remember"})

    reply = await Conversation(store, channel="tray").send("note the kitchen renovation")

    assert came_back[0]["kind"] == "not_set_up"
    assert ("memory search is unavailable: OpenAI could not be reached"
            in came_back[0]["error"])
    assert "not set up" in reply.text
    assert store.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    assert store.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert not list(paths.memories_dir().rglob("*.md")), "nothing half-written"


async def test_a_fault_in_a_tool_still_breaks_the_turn(monkeypatch):
    """Only what is not set up becomes an answer: a fault is not a setting."""
    async def faulty(ctx):
        raise ValueError("a bug")

    async def keyless(ctx):
        raise secrets.MissingSecret("BRAVE_API_KEY")

    with pytest.raises(ValueError):
        await toolset._set_up(faulty)(None)
    said = await toolset._set_up(keyless)(None)
    assert said["error"].startswith("not set up: BRAVE_API_KEY is not set")
    assert "python -m core.secrets set BRAVE_API_KEY" in said["error"]


async def test_a_turn_with_no_model_key_says_which_to_set_and_reaches_no_model(
        store, monkeypatch):
    """With no key at all it went to OpenRouter with a stand-in, and came
    back as "Something broke" with an authentication error in it."""
    monkeypatch.setattr(secrets, "get", lambda key, default=None: default)
    monkeypatch.setattr(loop, "build_agent",
                        lambda *a, **k: pytest.fail("no model is reached"))

    with pytest.raises(loop.TurnFailed) as failed:
        await Conversation(store, channel="tray").send("hello")

    said = str(failed.value)
    assert "no OPENAI_API_KEY" in said
    assert "python -m core.secrets set OPENAI_API_KEY" in said
    assert store.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
