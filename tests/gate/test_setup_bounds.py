"""Setup's choices reach no further than they say.

Two of setup's answers could be widened by a later change without any test
of the pages noticing:

- **The presets** (agent/worker/setup.py) say how much Steve does unasked.
  The maintainer, approving step 9's plan: they move only the attention
  knobs, never an approval, a switch, a cap or self-upgrade. A preset that
  also raised a spending cap, or turned on the browser, would be a choice
  the user never saw made.
- **The services' addresses** (core/endpoints.py). The exit walk and the
  end-to-end tests point a checkout's worker at fakes on this computer
  through `STEVE_OPENAI_URL`, `STEVE_OPENROUTER_URL`, `STEVE_TELEGRAM_URL`
  and `STEVE_BRAVE_URL`. An installed Steve that read one would send the
  user's key, and every conversation, wherever a stray variable pointed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx2
import pytest

from agent import provider
from agent.models import Role
from agent.worker import setup
from core import endpoints, knobs

ROOT = Path(__file__).resolve().parents[2]


def test_every_preset_moves_only_how_often_steve_speaks_up_unasked():
    attention = {name for name, knob in knobs.KNOBS.items() if knob.group == "attention"}
    # Meeting research sends the guests' names to a search engine: a
    # choice of its own, never part of a preset.
    assert set(setup.PRESET_KNOBS) <= attention - {"MEETING_RESEARCH"}
    assert not set(setup.PRESET_KNOBS) & set(knobs.SWITCHES)
    for name, moves in setup.PRESETS.items():
        assert set(moves) == set(setup.PRESET_KNOBS), name
        for knob, value in moves.items():
            if value is not None:
                assert knobs.value(knob, value) == value, (name, knob)
    assert setup.PRESETS["balanced"] == dict.fromkeys(setup.PRESET_KNOBS), "today's Steve"


# Each service a checkout may point at a fake: its variable, the function
# that reads it, and the service's own address. OpenRouter's is named in
# few files on purpose (tests/unit/test_no_vendor_names.py), so the
# addresses are endpoints' own.
SERVICES = [("STEVE_OPENAI_URL", "openai", endpoints.OPENAI),
            ("STEVE_OPENROUTER_URL", "openrouter", endpoints.OPENROUTER),
            ("STEVE_TELEGRAM_URL", "telegram", endpoints.TELEGRAM),
            ("STEVE_BRAVE_URL", "brave", endpoints.BRAVE)]


@pytest.mark.parametrize("variable,function,own", SERVICES)
@pytest.mark.parametrize("given", ["http://127.0.0.1:8999/v1", "https://api.example.net/v1"])
def test_an_installed_steve_reaches_each_service_at_its_own_address_whatever_the_environment(
        monkeypatch, variable, function, own, given):
    """Jev's key, the bot's token and the search key ride on these, as
    the OpenAI key does on OpenAI's: an install that read a stray variable
    would hand them, and what they carry, to whoever it named."""
    monkeypatch.setenv(variable, given)
    monkeypatch.setattr(endpoints.paths, "is_source_tree", lambda: False)
    assert getattr(endpoints, function)() == own


@pytest.mark.parametrize("variable,function,own", SERVICES)
@pytest.mark.parametrize("given", ["https://api.example.net/v1", "http://192.168.1.20:8000/v1",
                                   "http://localhost.example.net/v1", "ftp://127.0.0.1/v1"])
def test_a_checkout_takes_an_address_on_this_computer_and_no_other(
        monkeypatch, variable, function, own, given):
    monkeypatch.setenv(variable, given)
    assert getattr(endpoints, function)() == own


@pytest.mark.parametrize("variable,function,own", SERVICES)
def test_a_checkout_takes_the_fake_on_this_computer_that_it_names(
        monkeypatch, variable, function, own):
    monkeypatch.setenv(variable, "http://127.0.0.1:8999/x/")
    assert getattr(endpoints, function)() == "http://127.0.0.1:8999/x"


def test_jevs_credit_and_the_bot_api_are_read_at_the_fakes_a_checkout_names(monkeypatch):
    """The two addresses built once, rather than at each call: the credit
    check's, and the bot's client's. Jev's call and Brave's search build
    theirs at the call, which the guard below keeps from naming the
    service's own."""
    from agent.attention.sources import health
    from channels import telegram

    monkeypatch.setenv("STEVE_OPENROUTER_URL", "http://127.0.0.1:8999/api")
    monkeypatch.setenv("STEVE_TELEGRAM_URL", "http://127.0.0.1:8998")
    assert health.openrouter_credits() == "http://127.0.0.1:8999/api/v1/credits"
    api = telegram.BotApi("123456:" + "a" * 35,
                          transport=httpx2.MockTransport(lambda request: httpx2.Response(200)))
    assert str(api._client.base_url).startswith("http://127.0.0.1:8998/bot123456:")


def test_a_checkouts_conversations_go_to_the_fake_it_names(monkeypatch):
    """The exit walk's worker must answer from the fake, not from OpenAI."""
    monkeypatch.setenv("STEVE_OPENAI_URL", "http://127.0.0.1:8999/v1/")
    provider._openai_provider.cache_clear()
    try:
        client = provider.model_for(Role.CONVERSE).wrapped.client
        assert str(client.base_url) == "http://127.0.0.1:8999/v1/"
    finally:
        provider._openai_provider.cache_clear()


CLIENTS = {"OpenAI", "AsyncOpenAI", "OpenAIProvider"}
# The other services' addresses, as a URL would hold them; a bare name may
# be said to the user, as health tells them where to top up. OpenRouter's is
# split, since few files may name it (tests/unit/test_no_vendor_names.py).
ADDRESSES = ("api.openai.com", "https://openrouter" + ".ai", "https://api.telegram.org",
             "https://api.search.brave.com")
OWN = {"OPENAI", "OPENROUTER", "TELEGRAM", "BRAVE"}


def _own_addresses(path: Path) -> list[str]:
    """Each OpenAI client made without `base_url`, each service's address
    written out, and each use of endpoints' own address past its function,
    in one file."""
    where = path.relative_to(ROOT).as_posix()
    if where == "core/endpoints.py":
        return []
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        name = isinstance(node, ast.Call) and (getattr(node.func, "attr", None)
                                               or getattr(node.func, "id", None))
        if name in CLIENTS and not any(k.arg == "base_url" for k in node.keywords):
            found.append(f"{where}:{node.lineno} {name} without base_url")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found += [f"{where}:{node.lineno} names {a}" for a in ADDRESSES if a in node.value]
        if (isinstance(node, ast.Attribute) and node.attr in OWN
                and getattr(node.value, "id", None) == "endpoints"):
            found.append(f"{where}:{node.lineno} endpoints.{node.attr}")
    return found


def test_every_service_a_checkout_can_fake_is_reached_at_an_address_from_one_place():
    """Seven clients went straight to api.openai.com, embeddings, the mail
    screen and web search among them. The exit walk's worker, pointed at the
    fake, would have sent the user's name to OpenAI to embed it, where only
    the fences stopped it: the walk would not have tested what ships, and a
    test without the fences would have spent the developer's key. Jev, the
    bot and Brave are held to the same: a call that names its service's
    own address skips the fake the end-to-end tests point the worker at."""
    found = sorted({line for folder in ("agent", "core", "voice", "channels")
                    for path in (ROOT / folder).rglob("*.py")
                    for line in _own_addresses(path)})
    assert found == []


def test_a_socket_to_openai_is_at_the_same_address_as_everything_else(monkeypatch):
    assert endpoints.openai_socket() == "wss://api.openai.com/v1"
    monkeypatch.setenv("STEVE_OPENAI_URL", "http://127.0.0.1:8999/v1")
    assert endpoints.openai_socket() == "ws://127.0.0.1:8999/v1"
