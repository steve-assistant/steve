"""Turning a Role into a configured model client.

Everything that talks to a model goes through here, so what each call must
carry -- storage off, its effort, its tier, its timeout -- cannot be forgotten
at a call site, and every client is asked before it spends and billed after
(`guard`).

One provider: OpenAI, direct. Until 12 October 2026 there was a second. A
turn with no OpenAI key went to OpenRouter on its key instead, so a missing
key came back as an error from a service the user had not chosen for chat
(audit C6), and those calls carried the product's name to OpenRouter in an
attribution header (H3). OpenRouter now carries Jev's questions and nothing
else (core/decide.py).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import lru_cache

from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIChatModelSettings,
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.providers.openai import OpenAIProvider

from agent import cardwords
from agent.llm import plain_http_client
from agent.models import MODELS, ModelSpec, Role
from core import endpoints, secrets, spend

KEY = "OPENAI_API_KEY"


def key_missing() -> str | None:
    """The models' key, if it is not set; None if it is.

    Clients built while it was missing hold a stand-in (`_key`), so they
    are dropped: once the user sets it, the next turn builds them again,
    with no restart."""
    if secrets.get(KEY):
        return None
    reset_clients()
    return KEY


def needs_responses_api(spec: ModelSpec) -> bool:
    """Does this tier have to go through /v1/responses rather than chat?

    OpenAI refuses `reasoning_effort` together with function tools on
    /v1/chat/completions:

        Function tools with reasoning_effort are not supported for
        gpt-5.6-luna in /v1/chat/completions. To use function tools, use
        /v1/responses or set reasoning_effort to 'none'.

    The agent always attaches tools, so this made both escalation tiers a
    guaranteed 400 -- `escalate` had never once completed a turn. It failed
    at the tier boundary rather than at startup, so every ordinary turn
    worked and only the hard ones died, which is the worst place for a
    break to hide.

    Chat completions stays the default for the rest, for speed. It was
    chosen when GLM could be reached no other way, and with GLM gone the
    roles the user waits on were measured on 12 October 2026: twenty warm
    CONVERSE turns on each API, the same instructions, 85 tools and cache,
    interleaved. Responses gave the first token 239 and 273 ms later at the
    median, in two runs (1.22 s against 0.99, 1.15 against 0.87), past the
    100 ms the move was allowed. One API would keep a turn's cache warm
    when it escalates; that is worth less than every reply starting a
    quarter of a second later.
    """
    return bool(spec.supports_effort and spec.effort and spec.effort != "none")


log = logging.getLogger("provider")


def _key(name: str) -> str:
    """The key, or a stand-in the provider refuses at the first request.

    Building a client used to demand the key, so whatever built an agent
    broke where no key is kept -- the coding sandbox, by design. On 7
    October 2026 that rejected two of the night's three upgrade attempts:
    a gate test builds the lecture summary's agent with its model call
    replaced, and the client underneath it asked for a key. Nothing can go
    out without a key either way; the log says which one is missing before
    the request is refused."""
    value = secrets.get(name)
    if value:
        return value
    log.warning("%s is not set: requests through it will be refused "
                "(python -m core.secrets set %s)", name, name)
    return f"missing-{name}"


@lru_cache(maxsize=1)
def _openai_provider() -> OpenAIProvider:
    """One provider, and therefore one connection pool, per process.

    OpenAI reports failures with honest HTTP status codes, which the SDK
    maps to typed exceptions (`agent.llm.is_retryable` reads them).
    """
    return OpenAIProvider(
        base_url=endpoints.openai(),
        api_key=_key(KEY),
        http_client=plain_http_client(),
    )


def settings_for(spec: ModelSpec, *, cache_key: str | None = None):
    """Build per-call settings from a spec.

    `cache_key` is OpenAI's prompt-cache routing hint. A stable value per
    conversation keeps successive turns landing on the same cache, which is the
    single biggest lever on cost: cached input bills at 0.1x.
    """
    # OpenAI's Responses API stores every response unless told not to, and
    # every reasoning turn goes through it: their mail and calendar, kept in
    # OpenAI's dashboard for a month. Nothing here reads a stored response
    # back, and the privacy statement (PRIVACY.md) says storage is off.
    kwargs: dict = {"openai_store": False}

    # Per request, over the client's 300s: see CHAT_TIMEOUT_S in agent/models.py.
    kwargs["timeout"] = spec.timeout_s

    if spec.supports_effort and spec.effort is not None:
        kwargs["openai_reasoning_effort"] = spec.effort

    if spec.service_tier != "default":
        kwargs["openai_service_tier"] = spec.service_tier

    if cache_key:
        kwargs["openai_prompt_cache_key"] = cache_key

    # The settings class has to match the model class or pydantic-ai sends
    # the wrong request shape.
    if needs_responses_api(spec):
        return OpenAIResponsesModelSettings(**kwargs)
    return OpenAIChatModelSettings(**kwargs)


@lru_cache(maxsize=len(Role))
def model_for(role: Role) -> spend.Guarded:
    """The model client for a role. Cached: construction opens a client."""
    return client_for(MODELS[role])


def client_for(spec: ModelSpec) -> spend.Guarded:
    """A model client for any spec, the role's or not -- the model scout's
    candidates and judges (agent/models_eval.py). Uncached; the provider
    underneath is shared."""
    kind = OpenAIResponsesModel if needs_responses_api(spec) else OpenAIChatModel
    return guard(kind(spec.model_id, provider=_openai_provider(),
                      settings=settings_for(spec)), spec)


def guard(model, spec: ModelSpec) -> spend.Guarded:
    """`model`, asked before each request whether it may spend and billed
    after it at `spec`'s prices (`core/spend.Guarded`), and never sent a
    waiting card's word (`Unworded`). Every client this module builds is
    one; a test's fake is wrapped the same way to be billed as a real one
    would be."""
    return Unworded(model, slug=spec.slug, effort=spec.effort,
                    price=(spec.input_usd, spec.cached_input_usd, spec.output_usd))


class Unworded(spend.Guarded):
    """A model client whose requests hold no waiting card's word.

    A yes naming a card's word approves the card, so a model that knew the
    word could write a card of its own around it, and the user's yes to
    that would approve the real one. The word reached the model by every
    road the conversation has: the card itself as sent, the briefing's
    list, the user's own words quoting it, a tool's refusal naming it (step
    11's fourth review). Each road closed would leave the next one open, so
    the words are taken out here, where every request to every model
    passes: instructions, history and tools' results alike
    (agent/cardwords.mask). The model reads "[the card's word]".

    A subclass of the guard rather than a wrapper around it, so that the
    client is still the one model under it (`wrapped`)."""

    async def request(self, messages, model_settings, model_request_parameters):
        messages, model_request_parameters = unworded(messages, model_request_parameters)
        return await super().request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters,
                             run_context=None):
        messages, model_request_parameters = unworded(messages, model_request_parameters)
        async with super().request_stream(messages, model_settings, model_request_parameters,
                                          run_context) as stream:
            yield stream

    async def count_tokens(self, messages, model_settings, model_request_parameters):
        messages, model_request_parameters = unworded(messages, model_request_parameters)
        return await super().count_tokens(messages, model_settings, model_request_parameters)


def unworded(messages: list, parameters):
    """`messages` and the request's instructions with no waiting card's
    word in them; copies, so the conversation as stored is not changed."""
    words = cardwords.words_now()
    if not words:
        return messages, parameters
    if parameters.instruction_parts:
        parameters = replace(parameters, instruction_parts=[
            replace(part, content=cardwords.mask(part.content, words))
            for part in parameters.instruction_parts])
    out = []
    for message in messages:
        fields = {"parts": [_part(part, words) for part in message.parts]}
        if isinstance(message, ModelRequest) and message.instructions:
            fields["instructions"] = cardwords.mask(message.instructions, words)
        out.append(replace(message, **fields))
    return out, parameters


def _part(part, words: list[str]):
    if isinstance(part, ToolReturnPart) and not isinstance(
            part.content, (str, list, tuple, dict, int, float, bool, type(None))):
        # An object a tool returned, as the model would be sent it.
        part = replace(part, content=part.model_response_str())
    if isinstance(getattr(part, "content", None), (str, list, tuple, dict)):
        part = replace(part, content=cardwords.masked(part.content, words))
    if isinstance(getattr(part, "args", None), (str, dict)):
        part = replace(part, args=cardwords.masked(part.args, words))
    return part


def describe(role: Role) -> dict:
    """Diagnostics for the status endpoint -- never includes the API key."""
    spec = MODELS[role]
    return {
        "role": role.value,
        "slug": spec.slug,
        "label": spec.label,
        "effort": spec.effort,
        "service_tier": spec.service_tier,
        "context_window": spec.context_window,
        "usd_per_mtok": {
            "input": spec.input_usd,
            "cached_input": spec.cached_input_usd,
            "output": spec.output_usd,
        },
        "note": spec.note,
    }


def reset_clients() -> None:
    """Drop cached clients. Used by tests and after a credential change."""
    model_for.cache_clear()
    _openai_provider.cache_clear()
