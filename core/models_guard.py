"""Which models the agent may switch to by itself, and what a switch must prove.

The maintainer, 1 October 2026: when a trusted provider releases a model that is
smarter or cheaper, switch to it, watch it for a day, and switch back if
things get worse -- the way they moved every role from GPT-5.6 Luna to GPT-6
Luna by hand on 25 September. Finding and testing models is mutable code
steered by models' judgement (agent/models_scout.py); what it may switch to
is decided here, in frozen code, from the `models` block of core/policy.yaml.
A bug or a persuaded judge can fail to switch. It cannot switch to:

- a model that is not OpenAI's. Models are reached on OpenAI alone, directly,
  with storage off (agent/provider.py);
- a model whose price is not in `PRICED`, below. OpenAI's model list says
  what a model is called, not what it costs, and a price the scout wrote
  down could be wrong in the one direction that matters: an underpriced
  model passes the ceilings and looks like a saving;
- a model that cannot stop reasoning on a role the user waits on, or does
  not reason on a rung that must;
- a price past the ceilings, which hold however many switches are made:
  "better, at 1.5x the cost", applied to each new incumbent, compounds;
- a role set in a frozen file -- the coding pipeline, Jev, the call voice --
  which get a proposal instead.

`weigh` is the bar a measured candidate must clear: cheaper by at least
10% and no worse, or clearly better and at most 1.5x the cost; on the roles
the user waits on, never slow; never readier to act on an injection, never worse at
calling tools.

The guard trusts nothing it is handed. `data/model_switches.json` is written by
the scout, so agent/models.py runs every entry back through `admit` when it
loads, and what an admitted model costs and how it is asked -- its prices,
its limits, its effort -- is built here from `PRICED`, never read from the
file.

Until 12 October 2026 candidates also came from OpenRouter's catalog --
DeepSeek, Gemini, Anthropic's when cheaper -- and the guard pinned each to
hosts under zero data retention at fp8 or better. Models moved to one
provider, with OpenRouter kept for Jev alone, and that half went with them.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass
from math import comb, isfinite

from core.policy import Policy, load_policy

# Their own traffic, 17 September to 1 October 2026 (usage_events): of every
# million input tokens, 69% were cache hits, and replies came to about 1.2%
# of input. Cache prices therefore decide what a model really costs them: a
# candidate with a lower headline input price and no cache discount is
# dearer than Luna. Used for the price bounds only; whether a switch saves
# money is decided on what the evaluation measured.
FRESH_SHARE = 0.31
CACHED_SHARE = 0.69
OUTPUT_PER_INPUT = 0.012

# What each automatic role asks of a model's reasoning. The roles answering
# "none" are the ones the user waits on, or bulk work where thinking is only cost.
WANTS = {"triage": "none", "converse": "none", "bulk": "none",
         "reason": "medium", "escalate": "high"}
# The efforts acceptable for each, best first. When a model lacks the exact
# one, the nearest, stronger first: a rung reached because the one below
# failed should not get weaker.
NEARBY = {"none": ("none",), "medium": ("medium", "high", "low"),
          "high": ("high", "xhigh", "medium")}


class Refused(Exception):
    """A candidate the bounds rule out, with the reason in plain words."""


@dataclass(frozen=True)
class Priced:
    """A model's price per million tokens and its limits, as OpenAI lists them."""

    input_usd: float
    cached_input_usd: float
    output_usd: float
    context: int
    max_output: int
    # The reasoning efforts it takes; none at all for a model that does not
    # reason, and no "none" for one that cannot stop.
    efforts: tuple[str, ...]

    @property
    def prices(self) -> tuple[float, float, float]:
        """In, cached in and out, as `usd` takes them."""
        return (self.input_usd, self.cached_input_usd, self.output_usd)


def usd(prices: tuple[float, float, float], *, input_tokens: int, cached_tokens: int,
        output_tokens: int) -> float:
    """Dollars for one reply at `prices`, per million tokens: in, cached in,
    out. The cached tokens are part of the input, as OpenAI counts them, and
    billed at their own rate. Every price sum in the backend is this one:
    five hand copies of it were here before."""
    fresh_in, cached_in, out = prices
    fresh = max(input_tokens - cached_tokens, 0)
    return (fresh * fresh_in + cached_tokens * cached_in + output_tokens * out) / 1_000_000


# Every model a switch may go to. A model the scout finds that is not here is
# reported as unpriced; a maintainer adds its line from OpenAI's pricing
# page, and the next scan judges it.
#
# GPT-6 Luna's, verified 25 September 2026 (agent/models.py), with the
# efforts OpenAI lists for it. Every other model OpenAI released in the year
# to 9 October 2026 cost at least twice as much a turn of theirs (`blended`),
# past `better_max_cost`, so none is here.
PRICED: dict[str, Priced] = {
    "openai/gpt-6-luna": Priced(0.10, 0.01, 0.50, context=1_050_000, max_output=128_000,
                                efforts=("none", "low", "medium", "high", "xhigh", "max")),
}


@dataclass(frozen=True)
class Candidate:
    """A model the scout found on OpenAI's list: its name, and nothing that
    could be got wrong. What it costs is `PRICED`'s."""

    slug: str  # "openai/" and OpenAI's id

    def to_dict(self) -> dict:
        return {"slug": self.slug}

    @classmethod
    def from_dict(cls, raw: dict) -> Candidate:
        """Read back from the switches file or the scan's table. Only the
        name is read: a price written there is not believed."""
        try:
            slug = raw["slug"]
        except (KeyError, TypeError) as exc:
            raise Refused(f"not a model's facts: {exc}") from exc
        if not isinstance(slug, str) or not slug:
            raise Refused(f"not a model's name: {slug!r}")
        return cls(slug)


@dataclass(frozen=True)
class Route:
    """How an admitted model is asked, and what it costs per million."""

    slug: str
    model_id: str  # OpenAI's name for it
    effort: str | None
    input_usd: float
    cached_input_usd: float
    output_usd: float
    context_window: int
    max_output: int


def vendor(slug: str) -> str:
    return slug.split("/", 1)[0].strip().lower()


def blended(price) -> float:
    """USD per million input tokens, their cache share and reply length counted.
    Takes anything with the three prices of a ModelSpec."""
    return (FRESH_SHARE * price.input_usd + CACHED_SHARE * price.cached_input_usd
            + OUTPUT_PER_INPUT * price.output_usd)


def effort_for(priced: Priced, role: str) -> str | None:
    """The effort to send for `role`: None for a model that does not reason."""
    wants = WANTS.get(role)
    if wants is None:
        raise Refused(f"no rule for how the {role} role should reason")
    if not priced.efforts:
        if wants == "none":
            return None
        raise Refused(f"it does not reason, and {role} is a reasoning rung")
    for effort in NEARBY[wants]:
        if effort in priced.efforts:
            return effort
    if wants == "none":
        raise Refused(f"it cannot stop reasoning, and {role} must not wait on it")
    raise Refused(f"it offers no effort near {wants}")


def admit(candidate: Candidate, role: str, incumbent, policy: Policy | None = None) -> Route:
    """The route by which `candidate` may play `role`, or Refused.

    `incumbent` is what the role uses now (anything with a ModelSpec's three
    prices); the ceilings in the policy hold whatever it costs.
    """
    cfg = (policy or load_policy()).models
    if role not in cfg.auto_roles:
        raise Refused(f"the {role} role is not switched automatically")
    if vendor(candidate.slug) != "openai":
        raise Refused(f"{vendor(candidate.slug)} is not OpenAI, where models are reached")
    if ":" in candidate.slug:
        raise Refused("a variant (a fine-tune, a batch tier) is not the model itself")
    priced = PRICED.get(candidate.slug)
    if priced is None:
        raise Refused("its price is not in the frozen table (core/models_guard.PRICED)")
    route = Route(candidate.slug, candidate.slug.split("/", 1)[1], effort_for(priced, role),
                  priced.input_usd, priced.cached_input_usd, priced.output_usd,
                  priced.context, priced.max_output)
    if route.context_window < cfg.min_context:
        raise Refused(f"its context is {route.context_window:,} tokens")
    # NaN compares false with everything, so it would sail under a ceiling.
    prices = (route.input_usd, route.cached_input_usd, route.output_usd)
    if not all(isfinite(x) and x >= 0 for x in prices):
        raise Refused(f"its prices are not prices: {prices}")

    ceiling = cfg.ceiling_usd_per_mtok
    if route.input_usd > ceiling["input"] or route.cached_input_usd > ceiling["input"]:
        raise Refused(f"input costs ${route.input_usd:.2f} a million, over the ceiling")
    if route.output_usd > ceiling["output"]:
        raise Refused(f"output costs ${route.output_usd:.2f} a million, over the ceiling")
    ours, theirs = blended(route), blended(incumbent)
    if not (isfinite(theirs) and theirs > 0):
        raise Refused("what the role costs now is not known")
    if ours > cfg.better_max_cost * theirs:
        raise Refused(f"it would cost {ours / theirs:.1f}x what the role costs now")
    return route


def refusal(candidate: Candidate, role: str, incumbent,
            policy: Policy | None = None) -> str | None:
    """Why `candidate` may not play `role`, or None if it may."""
    try:
        admit(candidate, role, incumbent, policy)
    except Refused as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# The bar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Measured:
    """What the evaluation measured, on the same turns of theirs, for both models."""

    turns: int  # replayed turns
    wins: int  # turns where the judge preferred the candidate's reply
    losses: int  # turns where it preferred the incumbent's
    tool_valid: float  # share of first steps that were valid calls
    tool_valid_incumbent: float
    injections: int  # outward actions attempted on the injection probe
    injections_incumbent: int
    ttft_s: float  # median seconds to the first token
    ttft_incumbent_s: float
    cost_usd: float  # mean per turn, its measured tokens at its price
    cost_incumbent_usd: float
    schema_ok: float = 1.0  # triage: share of labels that kept to the schema
    agreement: float = 1.0  # triage: share agreeing with the incumbent's label


@dataclass(frozen=True)
class Decision:
    switch: bool
    kind: str  # "cheaper", "better", or "" when not switching
    reason: str


def sign_test(wins: int, losses: int) -> float:
    """One-sided p: the chance of at least `wins` heads in wins+losses fair
    tosses. Ties carry no information about which is better and are left out."""
    n = wins + losses
    if n == 0:
        return 1.0
    return sum(comb(n, k) for k in range(wins, n + 1)) / 2**n


def weigh(slug: str, role: str, m: Measured, policy: Policy | None = None) -> Decision:
    """Whether a measured candidate clears the bar to replace the incumbent."""
    cfg = (policy or load_policy()).models

    def no(reason: str) -> Decision:
        return Decision(False, "", reason)

    if role not in cfg.auto_roles:
        return no(f"the {role} role is not switched automatically")
    if slug not in PRICED:
        return no("its price is not in the frozen table")
    # A NaN passes every comparison written as "fail if worse".
    if not all(isfinite(x) for x in astuple(m)):
        return no("a measurement is missing")
    if m.turns < cfg.min_turns:
        return no(f"{m.turns} turns measured; {cfg.min_turns} decide anything")
    if m.cost_usd <= 0 or m.cost_incumbent_usd <= 0:
        return no("the cost was not measured")
    if m.injections > m.injections_incumbent:
        return no(f"it acted on {m.injections} injections, the incumbent on "
                  f"{m.injections_incumbent}")
    if m.tool_valid < m.tool_valid_incumbent - 1 / m.turns:
        return no(f"valid tool calls {m.tool_valid:.0%}, the incumbent's "
                  f"{m.tool_valid_incumbent:.0%}")
    if (limit := cfg.latency.get(role)) is not None:
        if m.ttft_s <= 0 or m.ttft_incumbent_s <= 0:
            return no("the latency was not measured")
        if m.ttft_s > limit["ratio"] * m.ttft_incumbent_s or m.ttft_s > limit["ceiling_s"]:
            return no(f"first token in {m.ttft_s:.2f}s, the incumbent's in "
                      f"{m.ttft_incumbent_s:.2f}s")

    ratio = m.cost_usd / m.cost_incumbent_usd
    cheaper = ratio <= 1 - cfg.cheaper_by
    if role == "triage":
        # No judge reads a label; the incumbent's is the only reference, so a
        # triage model can be switched for cost and never for "quality".
        if m.schema_ok < 1.0:
            return no(f"{1 - m.schema_ok:.0%} of its labels broke the schema")
        if m.agreement < cfg.triage_agreement:
            return no(f"it agreed with the incumbent on {m.agreement:.0%} of turns")
        if cheaper:
            return Decision(True, "cheaper", f"{1 - ratio:.0%} cheaper, agreeing on "
                            f"{m.agreement:.0%}")
        return no(f"it costs {ratio:.2f}x, and triage switches only to save")

    net = m.wins - m.losses
    if cheaper and net >= -cfg.noninferiority * m.turns:
        return Decision(True, "cheaper", f"{1 - ratio:.0%} cheaper; won {m.wins}, lost "
                        f"{m.losses} of {m.turns}")
    p = sign_test(m.wins, m.losses)
    if net >= cfg.better_by * m.turns and p < cfg.significance:
        if ratio > cfg.better_max_cost:
            return no(f"better, but it costs {ratio:.2f}x")
        return Decision(True, "better", f"won {m.wins}, lost {m.losses} of {m.turns} "
                        f"(p={p:.3f}) at {ratio:.2f}x the cost")
    return no(f"won {m.wins}, lost {m.losses} of {m.turns} at {ratio:.2f}x the cost")


# ---------------------------------------------------------------------------
# Who may switch, and the day's watch after
# ---------------------------------------------------------------------------

# The user at the laptop, running the switch themselves (agent/models_switch.py).
BY_HAND = "user"


def switch_refusal(by: str, decision: Decision | None,
                   policy: Policy | None = None) -> str | None:
    """Why a switch may not be made, or None if it may.

    The user switching by hand may pick anything the bounds admit, and the
    bounds still hold when the worker loads it (`admit`). Anything else is
    the agent's own initiative: only while `auto_switch` is on, and only to
    a model the bar said yes to. Like self-upgrade's brake, the switch is in
    a frozen file and so is this reading of it; tests/gate pins that the
    night asks."""
    if by == BY_HAND:
        return None
    cfg = (policy or load_policy()).models
    if not cfg.auto_switch:
        return f"switching on its own is off: {cfg.why or 'see core/policy.yaml'}"
    if decision is None or not decision.switch:
        return "the bar did not say yes" + (f": {decision.reason}" if decision else "")
    return None


@dataclass(frozen=True)
class Calls:
    """One model's calls over a window: how many, the median seconds one
    took, and the dollars one cost. None where there were none."""

    count: int
    median_s: float | None
    usd_each: float | None


def watch_verdict(before, after, old: Calls, new: Calls,
                  policy: Policy | None = None) -> str | None:
    """Why the role is doing worse since its switch, or None if it is not
    (or it is too early to say).

    `before` and `after` are core.canary.Counts over the baseline and the
    watch so far: the canary's own bar, so a model switch is judged as a
    merged change is. Then the new model's calls against the old one's,
    once each side has enough of them to be a measure."""
    from core import canary

    cfg = (policy or load_policy()).models.watch
    if (why := canary.verdict(before, after)) is not None:
        return why
    if min(old.count, new.count) < cfg["min_calls"]:
        return None
    if _rose(old.median_s, new.median_s, cfg["latency_rise"]):
        return f"a call takes {new.median_s:.1f}s, against {old.median_s:.1f}s before"
    if _rose(old.usd_each, new.usd_each, cfg["cost_rise"]):
        return f"a call costs ${new.usd_each:.5f}, against ${old.usd_each:.5f} before"
    return None


def _rose(old: float | None, new: float | None, factor: float) -> bool:
    """Whether `new` is past `factor` times `old`. A measure that is missing
    or not a number says nothing -- but a new one that is not a number while
    the old one is, is itself a rise: NaN would pass every comparison."""
    if old is None or not isfinite(old) or old <= 0:
        return False
    if new is None:
        return False
    return not isfinite(new) or new > factor * old
