"""The one provider, and per-call settings.

The load-bearing invariants: every model call goes to OpenAI and nowhere
else, nothing sent there is stored, and a missing key is named rather than
worked around. Until 12 October 2026 a turn with no OpenAI key went to
OpenRouter instead (audit C6); OpenRouter now carries Jev's questions alone.
"""

from __future__ import annotations

import pytest

from agent import provider
from agent.models import MODELS, Role


@pytest.fixture(autouse=True)
def _clear_client_cache():
    provider.reset_clients()
    yield
    provider.reset_clients()


def _only(*present: str):
    """A keychain holding these keys and no others."""
    return lambda key, default=None: f"sk-{key}" if key in present else default


# --------------------------------------------------------------------------
# Where a call goes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(Role))
def test_every_role_s_model_is_reached_at_openai_and_nowhere_else(role, monkeypatch):
    monkeypatch.setattr(provider.secrets, "get", _only("OPENAI_API_KEY", "OPENROUTER_API_KEY"))
    client = provider.model_for(role).wrapped.client
    assert str(client.base_url) == "https://api.openai.com/v1/"


def test_with_only_jevs_key_a_turn_names_the_openai_key_and_does_not_go_elsewhere(
        monkeypatch):
    """With no OpenAI key, a turn went to OpenRouter on Jev's key, and the
    error that came back named a service the user had not chosen for chat
    (audit C6)."""
    monkeypatch.setattr(provider.secrets, "get", _only("OPENROUTER_API_KEY"))
    assert provider.key_missing() == "OPENAI_API_KEY"
    client = provider.model_for(Role.CONVERSE).wrapped.client
    assert str(client.base_url) == "https://api.openai.com/v1/"
    assert client.api_key == "missing-OPENAI_API_KEY"


def test_a_key_set_after_a_client_was_built_without_it_is_used_without_a_restart(
        monkeypatch):
    """The client built without the key holds a stand-in; once a turn finds
    the key missing it is dropped, and the next one builds with the key."""
    monkeypatch.setattr(provider.secrets, "get", _only())
    stale = provider.model_for(Role.CONVERSE)
    assert provider.key_missing() == "OPENAI_API_KEY"

    monkeypatch.setattr(provider.secrets, "get", _only("OPENAI_API_KEY"))
    assert provider.key_missing() is None
    fresh = provider.model_for(Role.CONVERSE)
    assert fresh is not stale
    assert provider._openai_provider().client.api_key == "sk-OPENAI_API_KEY"


def test_a_client_is_built_without_keys_and_says_which_is_missing(monkeypatch, caplog):
    """The coding sandbox keeps no keys, by design; a gate test that builds
    an agent with its call replaced failed there on MissingSecret, and with
    it two of the night's three attempts (7 October 2026). A request still
    cannot go out: the stand-in is not a key."""
    monkeypatch.setattr(provider.secrets, "get", lambda k, d=None: None)

    with caplog.at_level("WARNING", logger="provider"):
        model = provider.model_for(Role.CONVERSE)

    assert model is not None
    assert "OPENAI_API_KEY is not set" in caplog.text


def test_a_key_that_is_there_is_used(monkeypatch):
    monkeypatch.setattr(provider.secrets, "get", lambda k, d=None: f"sk-{k}")
    assert provider._openai_provider().client.api_key == "sk-OPENAI_API_KEY"


# --------------------------------------------------------------------------
# What gets sent
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(Role))
def test_nothing_sent_to_openai_is_stored_there(role, monkeypatch):
    """The Responses API keeps every response by default, and every reasoning
    turn -- their mail, their calendar -- went through it with the default. The
    public privacy policy says storage is off; this is what makes it true."""
    monkeypatch.setattr(provider.secrets, "get", lambda k, d=None: "sk-test")
    assert provider.settings_for(MODELS[role])["openai_store"] is False


def test_reasoning_effort_is_sent_on_every_rung(monkeypatch):
    """Including "none": Luna reasons unless told not to, and the rungs the
    user waits on must not wait on it."""
    monkeypatch.setattr(provider.secrets, "get", lambda k, d=None: "sk-test")
    assert provider.settings_for(MODELS[Role.CONVERSE])["openai_reasoning_effort"] == "none"
    assert provider.settings_for(MODELS[Role.REASON])["openai_reasoning_effort"] == "medium"


def test_flex_tier_is_only_used_where_nobody_waits(monkeypatch):
    monkeypatch.setattr(provider.secrets, "get", lambda k, d=None: "sk-test")
    assert provider.settings_for(MODELS[Role.BULK])["openai_service_tier"] == "flex"
    assert "openai_service_tier" not in provider.settings_for(MODELS[Role.CONVERSE])


def test_cache_key_is_passed_through(monkeypatch):
    """Without it, successive turns miss the cache and bill at 10x."""
    monkeypatch.setattr(provider.secrets, "get", lambda k, d=None: "sk-test")
    settings = provider.settings_for(MODELS[Role.CONVERSE], cache_key="conv-123")
    assert settings["openai_prompt_cache_key"] == "conv-123"


# --------------------------------------------------------------------------
# Model ids
# --------------------------------------------------------------------------


def test_openai_gets_bare_ids_and_the_ledger_gets_the_maker_s_slug():
    """OpenAI rejects "openai/gpt-6-luna"; the cost ledger and the model
    scout key on the slug, with its maker."""
    for role, spec in MODELS.items():
        assert "/" not in spec.model_id, role
        assert spec.slug == f"openai/{spec.model_id}", role


# --------------------------------------------------------------------------
# Reasoning tiers cannot use chat completions
# --------------------------------------------------------------------------


def test_a_reasoning_tier_goes_through_the_responses_api():
    """The bug this pins broke every escalation and nothing else.

    OpenAI rejects `reasoning_effort` together with function tools on
    /v1/chat/completions with a 400. The agent always attaches tools, so
    both escalation tiers were a guaranteed failure -- `escalate` had never
    once completed a turn. Ordinary turns were fine, because CONVERSE runs
    at effort 'none', so the break only showed on the hard questions.
    """
    from pydantic_ai.models.openai import OpenAIResponsesModel

    from agent.provider import model_for

    for role in (Role.REASON, Role.ESCALATE):
        assert isinstance(model_for(role).wrapped, OpenAIResponsesModel), role


def test_the_cheap_tiers_stay_on_chat_completions():
    """Not a blanket switch. Measured on 12 October 2026, Responses gave
    CONVERSE's first token about a quarter of a second later than chat
    completions (`needs_responses_api`), so the roles the user waits on
    stay here."""
    from pydantic_ai.models.openai import OpenAIChatModel

    from agent.provider import model_for

    for role in (Role.CONVERSE, Role.BULK):
        assert isinstance(model_for(role).wrapped, OpenAIChatModel), role


def test_every_tier_that_sends_an_effort_is_routed_accordingly():
    """Derived from the specs rather than listed, so adding a tier or
    raising an effort cannot quietly reintroduce the 400."""
    from pydantic_ai.models.openai import OpenAIResponsesModel

    from agent.provider import model_for

    for role, spec in MODELS.items():
        sends_effort = bool(spec.supports_effort and spec.effort and spec.effort != "none")
        assert isinstance(model_for(role).wrapped, OpenAIResponsesModel) == sends_effort, role


def test_the_effort_is_actually_sent_on_the_tiers_that_have_one():
    """The settings types are TypedDicts, so there is no class to assert on
    -- at runtime they are both plain dicts and pydantic-ai picks the
    request shape from the *model* class. What is checkable, and what
    matters, is that the effort reaches the request at all: an escalation
    that silently dropped it would make escalation a replay of the same
    turn, at the same effort, for nothing.
    """
    from agent.provider import settings_for

    for role, effort in ((Role.REASON, "medium"), (Role.ESCALATE, "high")):
        assert settings_for(MODELS[role])["openai_reasoning_effort"] == effort, role


# --------------------------------------------------------------------------
# How long a reply waits on one request
# --------------------------------------------------------------------------


def test_a_reply_the_user_is_waiting_for_gives_up_on_a_stalled_request_in_ninety_seconds():
    """27 September 2026: one OpenAI request stalled, and "wait why did you
    cancel gym and runs?" waited out the 300-second default before a retry
    answered in 1.5s. The reasoning rungs keep 300s: high effort can think
    for minutes before a byte comes back."""
    from agent.models import CHAT_TIMEOUT_S
    from agent.provider import settings_for

    assert CHAT_TIMEOUT_S == 90.0
    for role in (Role.CONVERSE, Role.TRIAGE):
        assert settings_for(MODELS[role])["timeout"] == CHAT_TIMEOUT_S, role
    for role in (Role.REASON, Role.ESCALATE, Role.BULK):
        assert settings_for(MODELS[role])["timeout"] == 300.0, role


async def test_the_timeout_reaches_the_request_itself():
    """A setting pydantic-ai ignored would leave the client's 300s in force:
    what counts is the timeout on the HTTP request that goes out."""
    import httpx2
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from agent.provider import settings_for

    seen = []

    def answer(request):
        seen.append(request.extensions.get("timeout"))
        return httpx2.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "gpt-6-luna",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(answer),
                                timeout=httpx2.Timeout(300.0, connect=10.0))
    model = OpenAIChatModel("gpt-6-luna", provider=OpenAIProvider(api_key="x", http_client=client))
    result = await Agent(model, model_settings=settings_for(MODELS[Role.CONVERSE])).run("hello")

    assert result.output == "hi"
    assert seen and seen[0]["read"] == 90.0
