"""The keys: two are needed, and each service asks for its own.

Step 3 moved every model, web search and embeddings onto OpenAI's key, and
left OpenRouter's for Jev, the decision model, alone. Before it, five
vendors' keys were declared and the README asked for three. The keychain
still listed keys for voice and search services no code called any more:
a user setting one up would set up nothing. And a key asked for under a
name not declared reads as never set, deep inside a tool call, with no
`python -m core.secrets set` that will take it.

So the keys declared and the keys asked for are held to each other, and
the two a user is told to set are shown to be enough: with them, each
service finds its key; without OpenRouter's, Jev is not asked and each of
its sites falls back while health says why; without OpenAI's, the models,
memory and search each say so rather than fail on a stand-in.
"""

from __future__ import annotations

import ast
import asyncio
import json
from functools import cache
from pathlib import Path

import pytest

from agent import provider
from agent.attention import fetch
from agent.attention.sources import calendar, health
from core import decide, embed, secrets, spend, websearch
from tests.gate import repo_files
from tests.gate.decisions import install

ROOT = Path(__file__).resolve().parents[2]
CODE = ("agent", "channels", "core", "devtools", "modules", "scripts", "tools", "voice")
CAPTURED_SEARCH = json.loads((ROOT / "tests" / "gate" / "fixtures" / "websearch"
                              / "captured.json").read_text(encoding="utf-8"))
REAL_SEARCH_CALL = websearch._call

# Declared, and read by no code yet, each with what it waits for. Emptied as
# the step it names decides: a key nothing reads is set up for nothing.
WAITING: dict[str, str] = {}

TWO_KEYS = {"OPENAI_API_KEY": "openai-key", "OPENROUTER_API_KEY": "openrouter-key"}


def _keychain(monkeypatch, keys: dict[str, str]) -> None:
    """The keychain holding `keys` and nothing else. In a test every declared
    key otherwise reads as set (tests/gate/fences.py)."""
    monkeypatch.setattr("core.secrets.get", lambda name, default=None: keys.get(name, default))


@cache
def _asked_for() -> dict[str, list[str]]:
    """Each key name the code reads through `secrets.get` or `require`, with
    where: a literal, or a module's constant of one, by name. A name made at
    run time (a loop over a tuple, a parameter) is left to its caller's
    literal, which is read here where it is written."""
    constants: dict[str, set[str]] = {}
    trees: list[tuple[str, ast.Module]] = []
    for rel in repo_files.python(*(f"{folder}/" for folder in CODE)):
        tree = repo_files.parsed(rel)
        trees.append((rel, tree))
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                constants.setdefault(node.targets[0].id, set()).add(node.value.value)
    found: dict[str, list[str]] = {}
    for where, tree in trees:
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("get", "require")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "secrets" and node.args):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names = {arg.value}
            elif isinstance(arg, ast.Name | ast.Attribute):
                names = constants.get(arg.id if isinstance(arg, ast.Name) else arg.attr, set())
            else:
                names = set()
            for name in names:
                found.setdefault(name, []).append(f"{where}:{node.lineno}")
    return found


def test_every_key_the_code_asks_for_is_one_the_user_can_set():
    """`set_secret` refuses an undeclared name, so a key asked for under one
    could never be stored: the feature would read it as never set, forever."""
    asked = _asked_for()

    # The scan reads literals and constants alike, here and across modules.
    assert {"OPENAI_API_KEY", "OPENROUTER_API_KEY", "BRAVE_API_KEY",
            "APPROVAL_TOTP_SECRET"} <= set(asked)
    assert {name: where for name, where in asked.items()
            if name not in secrets.KNOWN_KEYS} == {}


def test_every_key_the_user_can_set_is_one_the_code_asks_for():
    """The dead keys of step 3: three dropped providers' keys stayed in the
    keychain's list after the code stopped calling them, offered to a user
    to set up for nothing."""
    asked = _asked_for()

    assert sorted(set(secrets.KNOWN_KEYS) - set(asked) - set(WAITING)) == []
    assert sorted(set(WAITING) & set(asked)) == [], "read now: take it out of WAITING"
    assert set(WAITING) <= set(secrets.KNOWN_KEYS)


def test_the_two_keys_a_user_is_told_to_set_are_described_as_what_they_pay_for():
    """The README asks for these two; `python -m core.secrets status` shows
    the descriptions beside them."""
    assert secrets.KNOWN_KEYS["OPENAI_API_KEY"] == (
        "OpenAI: every model, web search and embeddings")
    assert secrets.KNOWN_KEYS["OPENROUTER_API_KEY"] == "OpenRouter: Jev, the decision model"


# ---------------------------------------------------------------------------
# The services, on the keys the keychain holds
# ---------------------------------------------------------------------------

def _embedding(conn, monkeypatch) -> list[str]:
    """The keys an embedding was asked on, or why there was none."""
    used: list[str] = []

    def call(request: dict, key: str) -> dict:
        used.append(key)
        return {"model": request["model"], "usage": {"prompt_tokens": 1, "total_tokens": 1},
                "data": [{"index": 0, "embedding": [0.1] * request["dimensions"]}]}

    monkeypatch.setattr(embed, "_call", call)
    try:
        with spend.spending("chat", conn=conn):
            embed.vectors(["a note"], conn=conn)
    except embed.EmbedderUnavailable as exc:
        return [str(exc)]
    return used


def _searching(conn, monkeypatch) -> list[str]:
    """Which service a web search was asked of, or why there was none."""
    asked: list[str] = []

    async def openai_call(request: dict) -> dict:
        asked.append("openai")
        if not secrets.get("OPENAI_API_KEY"):
            # The real call, refused at the keychain before any client is made.
            return await REAL_SEARCH_CALL(request)
        return CAPTURED_SEARCH["answer"]

    async def run():
        with spend.spending("chat", conn=conn):
            return await websearch.search("opening hours of the library", 2)

    monkeypatch.setattr(websearch, "_call", openai_call)
    monkeypatch.setattr(websearch, "_by_brave",
                        lambda *a: pytest.fail("Brave asked with no Brave key"))
    try:
        asyncio.run(run())
    except websearch.SearchUnavailable as exc:
        return asked + [str(exc)]
    return asked


def _credits(monkeypatch) -> list[str]:
    asked: list[str] = []

    def get_json(url, headers=None, **kwargs):
        asked.append(url)
        return {"data": {"total_credits": 10, "total_usage": 1}}

    monkeypatch.setattr(fetch, "get_json", get_json)
    return asked


def test_with_the_two_keys_every_service_finds_its_own(conn, monkeypatch):
    """The README's promise: these two, and nothing else, is a working
    install. Search goes to OpenAI with no Brave key, and health has
    nothing to say about either key."""
    _keychain(monkeypatch, dict(TWO_KEYS))
    jev = install(monkeypatch, q=0.5)
    credits = _credits(monkeypatch)

    decided = decide.ask(conn, "text", [decide.Predicate("q", "?")])

    assert provider.key_missing() is None
    assert _embedding(conn, monkeypatch) == ["openai-key"]
    assert _searching(conn, monkeypatch) == ["openai"]
    assert decided.answers["q"].probability == 0.5 and len(jev.asked) == 1
    assert calendar.can_judge() is True
    assert health._decisions(conn) is None and health._openrouter(conn) is None
    assert credits == [health.openrouter_credits()]


def test_without_openrouters_key_jev_is_not_asked_and_health_says_so(conn, monkeypatch):
    """Each of Jev's sites falls back on its own: the screen to extracts,
    the critic to a card, meetings to no follow-up question. Health is the
    one place that says why; everything on OpenAI's key goes on."""
    _keychain(monkeypatch, {"OPENAI_API_KEY": "openai-key"})
    jev = install(monkeypatch, q=0.5)
    credits = _credits(monkeypatch)

    decided = decide.ask(conn, "text", [decide.Predicate("q", "?")])

    assert decided.answers == {} and decided.error == "no OpenRouter key"
    assert jev.asked == []
    assert calendar.can_judge() is False
    name, title, detail = health._decisions(conn)
    assert (name, title) == ("decisions:nokey", "Jev, the decision model, has no key")
    assert detail.endswith(health.WITHOUT_JEV)
    assert health._openrouter(conn) is None and credits == []
    assert provider.key_missing() is None
    assert _embedding(conn, monkeypatch) == ["openai-key"]
    assert _searching(conn, monkeypatch) == ["openai"]


def test_without_openais_key_models_memory_and_search_each_say_so(conn, monkeypatch):
    """None of them goes out on a stand-in, and each names the missing key
    as the user can act on it. Jev, on its own key, is still asked."""
    _keychain(monkeypatch, {"OPENROUTER_API_KEY": "openrouter-key"})
    jev = install(monkeypatch, q=0.5)

    assert provider.key_missing() == "OPENAI_API_KEY"
    assert _embedding(conn, monkeypatch) == [
        "memory search is unavailable: no OpenAI key is stored"]
    assert _searching(conn, monkeypatch) == ["openai", "no OpenAI key is stored"]
    assert decide.ask(conn, "text", [decide.Predicate("q", "?")]).answers["q"].probability == 0.5
    assert len(jev.asked) == 1
