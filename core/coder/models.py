"""The models the coding pipeline may use, what they cost, and their clients.

Its own clients rather than `agent/provider.py`'s: that module is outside the
frozen core, and a harness whose model calls go through code the agent can
change is a harness whose judge the agent can quietly swap for a cheaper
one, or whose prompts it can route somewhere else. The same rule holds
here as there: every request goes to OpenAI with storage off.

Until 12 October 2026 the catalog also held GLM and DeepSeek through
OpenRouter. The coder and the test builder moved to GPT-6 Luna on 28
September, and models moved to one provider, with OpenRouter kept for Jev.

Prices are dollars per million tokens (in, cached in, out), from OpenAI's
list as verified on 25 September 2026. Which model plays which role is set
in the `coder:` section of core/policy.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass

from core import models_guard


@dataclass(frozen=True)
class CoderModel:
    name: str
    model_id: str
    price_in: float
    price_cached: float
    price_out: float
    effort: str | None = None

    def cost(self, *, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
        return models_guard.usd((self.price_in, self.price_cached, self.price_out),
                                input_tokens=input_tokens, cached_tokens=cached_tokens,
                                output_tokens=output_tokens)


_LUNA = models_guard.PRICED["openai/gpt-6-luna"].prices
CATALOG: dict[str, CoderModel] = {m.name: m for m in (
    CoderModel("gpt-6-luna", "gpt-6-luna", *_LUNA, effort="medium"),
    CoderModel("gpt-6-luna-high", "gpt-6-luna", *_LUNA, effort="high"),
    CoderModel("gpt-6-sol", "gpt-6-sol", 2.00, 0.20, 10.00, effort="medium"),
)}

# How long one model request may take before it is abandoned and retried.
# Unset, it was the SDK's 600s: in the third live run a coder's revision
# made two tool calls in twenty minutes and billed $0.0003 -- stalled
# requests, not work -- and ran out its clock. Measured in that run, a role's
# turns averaged 13 to 50 seconds each, tool time taken out.
REQUEST_TIMEOUT_S = 240.0


def client(model: CoderModel):
    """(pydantic-ai model, settings) for one model."""
    from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings

    from core import spend

    # Asked before each request whether it may spend, and billed after it,
    # like every client Steve builds (core/spend.py `Guarded`).
    def guarded(llm):
        return spend.Guarded(llm, slug=model.name, effort=model.effort,
                             price=(model.price_in, model.price_cached, model.price_out))

    return (guarded(OpenAIResponsesModel(model.model_id, provider=_openai())),
            OpenAIResponsesModelSettings(openai_store=False,
                                         openai_reasoning_effort=model.effort or "medium",
                                         timeout=REQUEST_TIMEOUT_S))


def _openai():
    from pydantic_ai.providers.openai import OpenAIProvider

    from core import endpoints, secrets

    return OpenAIProvider(api_key=secrets.require("OPENAI_API_KEY"), base_url=endpoints.openai())


def spent(model: CoderModel, usage) -> float:
    """Dollars for a pydantic-ai usage object."""
    return model.cost(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        cached_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )
