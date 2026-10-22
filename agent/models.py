"""Every model id, price, and routing decision in one place.

Model deprecation is a recurring event, not an edge case -- so there is exactly
one file to edit when a model is retired or repriced, and nothing elsewhere may
hardcode a slug.

One provider: every role is OpenAI's, reached directly. Until 12 October
2026 there were two gateways. The coding role ran GLM through OpenRouter,
under a provider policy pinning its host and quantisation, and OpenRouter
was also chat's fallback when the OpenAI key was missing -- which sent a
fresh install's turns to a service the user had not chosen for them. The
coding pipeline has its own catalog (core/coder/models.py), so the role went,
and OpenRouter is kept for Jev alone (core/decide.py).

Prices are USD per million tokens, verified 2026-09-25 for GPT-6 Luna. They
exist here so cost accounting is honest without a second lookup; if they
drift, the ledger in usage_events drifts with them, which is the failure
mode to watch.

This file is the lineup as written. The model scout (agent/models_scout.py)
may switch the automatic roles to a newer or cheaper model by writing
`data/model_switches.json`; `load_active` applies it when the worker starts,
after core/models_guard.py has re-checked every entry against the bounds in
core/policy.yaml and priced it from its own frozen table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from core import models_guard, paths, settingsfile


class Role(StrEnum):
    """What a model is being asked to do, not which model it is."""

    TRIAGE = "triage"  # classify an inbound turn; latency-critical
    CONVERSE = "converse"  # the default reply path, including voice
    REASON = "reason"  # harder turns that earn reasoning tokens
    ESCALATE = "escalate"  # REASON failed twice; go sideways to a better model
    BULK = "bulk"  # reflection, summarisation; latency irrelevant


@dataclass(frozen=True)
class ModelSpec:
    model_id: str  # exactly what OpenAI expects
    slug: str  # "maker/model", for display and the ledger
    label: str
    input_usd: float  # per 1M tokens
    cached_input_usd: float
    output_usd: float
    context_window: int
    max_output: int
    effort: str | None  # reasoning_effort to send, or None to omit
    supports_effort: bool
    # OpenAI service tier. "flex" is roughly half price for double the latency,
    # which is free money on anything the user is not waiting for.
    service_tier: str = "default"
    note: str = ""
    # How long one request may go without an answer before it is abandoned
    # and retried. Requests are not streamed, so this is the whole answer's
    # time, not the first token's.
    timeout_s: float = 300.0

    def cost_usd(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> float:
        """Cost of one call. Cached tokens are billed at the cached rate, and
        are assumed to be a *subset* of input_tokens (which is how OpenAI
        reports them)."""
        return models_guard.usd((self.input_usd, self.cached_input_usd, self.output_usd),
                                input_tokens=input_tokens, cached_tokens=cached_tokens,
                                output_tokens=output_tokens)


# ---------------------------------------------------------------------------
# The lineup
#
# `slug` keeps the "maker/model" shape the ledger has always used, so
# usage_events reads the same before and after a model is reached another
# way; `model_id` is the bare name OpenAI takes.
# ---------------------------------------------------------------------------

# GPT-6 Luna for every OpenAI tier since 25 September 2026, the user's call: it
# is smarter than 5.6 Luna and costs half as much, and at high effort it
# replaces 5.6 Terra at the top of the ladder for a sixth of Terra's price.
# Measured that day, warm, effort none: 0.63-0.72s TTFT, 5.6 Luna 0.70-0.71s.
LUNA = "openai/gpt-6-luna"

_LUNA_ID = "gpt-6-luna"
_LUNA_PRICES = dict(zip(("input_usd", "cached_input_usd", "output_usd"),
                        models_guard.PRICED[LUNA].prices, strict=True))
_LUNA_LIMITS = dict(context_window=1_050_000, max_output=128_000)

# The roles the user waits on. One OpenAI request stalled on 27 September 2026 and
# their reply waited out the 300-second default before a retry answered in
# 1.5s. Measured then: an answer with no reasoning comes at about 80 tokens a
# second, and no turn on record fell below 32 even with its tools' time
# counted in, so 90s is room for an answer of several thousand tokens and
# still cuts a stall from five minutes to a minute and a half. The reasoning
# rungs keep the default: high effort can think for minutes before a byte
# comes back.
CHAT_TIMEOUT_S = 90.0

MODELS: dict[Role, ModelSpec] = {
    # Luna at effort=none measures ~0.65s TTFT, which is what keeps the voice
    # path under a second end to end. Anything with reasoning enabled here
    # would be felt immediately.
    Role.TRIAGE: ModelSpec(
        model_id=_LUNA_ID,
        slug=LUNA,
        label="Luna (no reasoning)",
        effort="none",
        supports_effort=True,
        note="latency-critical classification; strict JSON schema",
        timeout_s=CHAT_TIMEOUT_S,
        **_LUNA_PRICES,
        **_LUNA_LIMITS,
    ),
    Role.CONVERSE: ModelSpec(
        model_id=_LUNA_ID,
        slug=LUNA,
        label="Luna (no reasoning)",
        effort="none",
        supports_effort=True,
        note="default reply path; ~0.65s TTFT",
        timeout_s=CHAT_TIMEOUT_S,
        **_LUNA_PRICES,
        **_LUNA_LIMITS,
    ),
    # Effort, not a bigger model, is the ladder now. On 5.6 Luna, medium
    # measured ~1.93s TTFT and high ~8.47s on real turns; 6 Luna's hard-turn
    # timings are not measured yet, so high stays at the top, where a turn
    # has already failed twice and waiting is the lesser cost.
    Role.REASON: ModelSpec(
        model_id=_LUNA_ID,
        slug=LUNA,
        label="Luna (medium reasoning)",
        effort="medium",
        supports_effort=True,
        note="the second rung; reached when CONVERSE escalates",
        **_LUNA_PRICES,
        **_LUNA_LIMITS,
    ),
    Role.ESCALATE: ModelSpec(
        model_id=_LUNA_ID,
        slug=LUNA,
        label="Luna (high reasoning)",
        effort="high",
        supports_effort=True,
        note="the top rung, after REASON fails twice; also the tool reviewer",
        **_LUNA_PRICES,
        **_LUNA_LIMITS,
    ),
    Role.BULK: ModelSpec(
        model_id=_LUNA_ID,
        slug=LUNA,
        label="Luna (no reasoning, flex)",
        effort="none",
        supports_effort=True,
        service_tier="flex",
        # Flex is billed at half the standard rate, so the ledger must use
        # these numbers rather than Luna's default ones.
        input_usd=0.05,
        cached_input_usd=0.005,
        output_usd=0.25,
        note="reflection and summarisation; half price, latency irrelevant",
        **_LUNA_LIMITS,
    ),
}

def spec(role: Role) -> ModelSpec:
    return MODELS[role]


# ---------------------------------------------------------------------------
# The scout's switches
#
# Applied by the worker at startup, not at import: every test and every
# process imports this module, and none of them should quietly run on a
# lineup read from the live data folder. A worker swap applies a new switch.
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


def active_path() -> Path:
    return paths.data_dir() / "model_switches.json"


# Each change of the switches file's shape, oldest first (core/settingsfile.py).
SWITCH_STEPS = (settingsfile.stamped,)


# The lineup as written above. Each switch is checked against this, not
# against whatever was switched to last.
BASE: dict[Role, ModelSpec] = dict(MODELS)


def spec_from_route(route, base: ModelSpec, why: str) -> ModelSpec:
    """A ModelSpec for a model the guard admitted (core.models_guard.Route),
    in place of `base` -- for a switch, or for an evaluation of one."""
    return ModelSpec(
        model_id=route.model_id,
        slug=route.slug,
        label=f"{route.slug} ({route.effort or 'no reasoning'}, {why})",
        input_usd=route.input_usd,
        cached_input_usd=route.cached_input_usd,
        output_usd=route.output_usd,
        context_window=route.context_window,
        max_output=route.max_output,
        effort=route.effort,
        supports_effort=route.effort is not None,
        note=f"in place of {base.slug}; written lineup in agent/models.py",
        timeout_s=base.timeout_s,
    )


def load_active(path: Path | None = None) -> dict[str, str]:
    """Put the scout's switches over the lineup, in place; returns why any
    entry was refused, by role.

    The file is written by mutable code, so nothing in it is taken on trust:
    each entry names a model, which core.models_guard.admit checks against
    the frozen bounds, and the route -- its prices, limits and effort -- is
    what the guard builds from its own frozen table, not what the file says.
    A refused entry leaves its role as written here.
    """
    from core.models_guard import Candidate, Refused, admit

    path = path or active_path()
    MODELS.clear()
    MODELS.update(BASE)
    refused: dict[str, str] = {}
    try:
        raw = settingsfile.load(path, SWITCH_STEPS)
    except (OSError, ValueError) as exc:
        log.warning("model switches unreadable, lineup as written: %s", exc)
        return {"*": f"unreadable: {exc}"}
    if raw is None:
        return refused
    roles = raw.get("roles")
    for name, entry in roles.items() if isinstance(roles, dict) else ():
        try:
            role = Role(name)
            base = BASE[role]
            facts = entry.get("candidate") if isinstance(entry, dict) else None
            route = admit(Candidate.from_dict(facts if isinstance(facts, dict) else {}),
                          role.value, base)
        except (ValueError, Refused) as exc:
            refused[str(name)] = str(exc)
            log.warning("model switch for %s refused: %s", name, exc)
            continue
        MODELS[role] = spec_from_route(route, base, "switched by the scout")
        log.info("model switch: %s now %s", role.value, route.slug)
    return refused


# ---------------------------------------------------------------------------
# The desk's live voice
#
# Not a `ModelSpec`: GPT-Live bills by the second of session, not by the
# token, and does no thinking of its own worth accounting -- that is the
# CONVERSE turn, billed as usual. Used by `voice/desk_call.py`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceSpec:
    model_id: str
    usd_per_minute: float
    note: str = ""

    def cost_usd(self, seconds: float) -> float:
        return max(seconds, 0.0) / 60 * self.usd_per_minute


LIVE_VOICE = VoiceSpec(
    model_id="gpt-live-1",
    usd_per_minute=0.05,
    note="full-duplex voice layer, per second of session; verified 2026-09-24",
)
